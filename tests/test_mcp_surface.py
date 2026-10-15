"""What an MCP client sees: behaviour hints, output schemas, structuredContent
and the resource_link to the generated audio. All provider calls are mocked."""
import wave
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import voice_io


async def _tools():
    return {t.name: t for t in await voice_io.mcp.list_tools()}


@pytest.mark.asyncio
async def test_the_four_tools_keep_their_names():
    assert set(await _tools()) == {"text_to_speech", "speech_to_text", "list_voices", "check_provider_health"}


@pytest.mark.asyncio
@pytest.mark.parametrize("name,read_only,destructive,idempotent,open_world", [
    ("text_to_speech", False, False, False, True),
    ("speech_to_text", True, None, False, True),
    ("list_voices", True, None, True, False),
    ("check_provider_health", True, None, False, True),
])
async def test_annotations(name, read_only, destructive, idempotent, open_world):
    tool = (await _tools())[name]
    a = tool.annotations
    assert (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint) == (
        read_only, destructive, idempotent, open_world)
    assert tool.title


@pytest.mark.asyncio
async def test_descriptions_are_the_original_docstrings():
    tools = await _tools()
    assert "Tries Groq's Orpheus first" in tools["text_to_speech"].description
    assert "Transcribe a local audio file" in tools["speech_to_text"].description
    assert "Costs free-tier quota" in tools["check_provider_health"].description


@pytest.mark.asyncio
async def test_audio_tools_publish_output_schemas():
    tools = await _tools()
    assert set(tools["text_to_speech"].output_schema["properties"]) >= {
        "path", "uri", "format", "model", "tier", "requests", "format_substituted"}
    assert set(tools["speech_to_text"].output_schema["properties"]) >= {"text", "model", "tier", "language_hint"}


@pytest.mark.asyncio
async def test_tts_returns_text_structured_content_and_a_resource_link(groq_key, fake_aspeech, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    fake_aspeech(written_files=[])

    result = await voice_io.mcp.call_tool("text_to_speech", {"text": "hello world"})

    assert not result.is_error
    text, link = result.content
    assert text.text.startswith("Audio saved to ") and "model: groq/canopylabs/orpheus-v1-english" in text.text
    data = result.structured_content
    assert data["tier"] == "hosted" and data["requests"] == 1 and data["format"] == "wav"
    assert data["format_substituted"] is False
    assert Path(data["path"]).is_file() and Path(data["path"]).parent == tmp_path.resolve()
    assert link.type == "resource_link" and link.mime_type == "audio/wav"
    assert link.uri == data["uri"] and data["uri"].startswith("file://")
    assert link.name == Path(data["path"]).name


@pytest.mark.asyncio
async def test_tts_local_tier_and_mp3_substitution_are_reported(no_groq_key, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(voice_io, "_local_text_to_speech",
                        lambda text, filepath, voice="af_heart": filepath.write_bytes(b"x") or True)

    result = await voice_io.mcp.call_tool("text_to_speech", {"text": "hello", "output_format": "mp3"})

    data = result.structured_content
    assert data["tier"] == "local" and data["model"] == "local:kokoro-82m"
    assert data["format_substituted"] is True and data["path"].endswith(".wav")
    assert "requested format ignored" in result.content[0].text


@pytest.mark.asyncio
async def test_tts_counts_hosted_requests_for_long_text(groq_key, fake_aspeech, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    fake_aspeech(written_files=[])
    monkeypatch.setattr(voice_io, "_join_wavs", lambda parts, target: target.write_bytes(b"joined"))

    result = await voice_io.mcp.call_tool("text_to_speech", {"text": "word " * 100})

    assert result.structured_content["requests"] == len(voice_io._tts_chunks("word " * 100)) > 1


@pytest.mark.asyncio
async def test_tts_error_still_raises_tool_error(no_groq_key):
    # MCPServer.call_tool raises ToolError; the protocol layer turns it into an
    # isError result, exactly as before this change.
    with pytest.raises(ToolError, match="empty"):
        await voice_io.mcp.call_tool("text_to_speech", {"text": "   "})


def _wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 160)
    return path


@pytest.mark.asyncio
async def test_stt_structured_hosted(groq_key, fake_atranscription, tmp_path):
    fake_atranscription(text="merhaba dünya")
    audio = _wav(tmp_path / "memo.wav")

    result = await voice_io.mcp.call_tool("speech_to_text", {"audio_path": str(audio), "language": "tr"})

    assert result.structured_content == {
        "text": "merhaba dünya", "model": voice_io.STT_MODEL, "tier": "hosted", "language_hint": "tr"}
    assert result.content[0].text == f"merhaba dünya\n\n(model: {voice_io.STT_MODEL})"


@pytest.mark.asyncio
async def test_stt_structured_local(no_groq_key, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "_local_speech_to_text", lambda audio: "local words")
    audio = _wav(tmp_path / "memo.wav")

    result = await voice_io.mcp.call_tool("speech_to_text", {"audio_path": str(audio)})

    data = result.structured_content
    assert data["tier"] == "local" and data["text"] == "local words" and data["language_hint"] is None


@pytest.mark.asyncio
async def test_stt_refusal_still_raises_before_any_upload(groq_key, fake_atranscription, tmp_path):
    mock = fake_atranscription()
    secret = tmp_path / ".env"
    secret.write_text("KEY=1")
    with pytest.raises(ToolError, match="Rejected"):
        await voice_io.mcp.call_tool("speech_to_text", {"audio_path": str(secret)})
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_text_functions_still_return_the_same_text(groq_key, fake_aspeech, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    fake_aspeech(written_files=[])
    assert (await voice_io.text_to_speech(text="hi")).startswith("Audio saved to ")
