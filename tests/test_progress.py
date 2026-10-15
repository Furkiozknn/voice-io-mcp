"""Progress notifications: one per hosted TTS request (plus the join), and a
start/done pair around the slow local models and the STT upload."""
import wave
from pathlib import Path

import pytest

import voice_io


class FakeContext:
    """Stands in for mcp's Context: records report_progress calls."""

    def __init__(self, fail: bool = False):
        self.calls = []
        self.fail = fail

    async def report_progress(self, progress, total=None, message=None):
        self.calls.append((progress, total, message))
        if self.fail:
            raise RuntimeError("client went away")


@pytest.mark.asyncio
async def test_ctx_is_not_part_of_the_input_schema():
    tools = {t.name: t for t in await voice_io.mcp.list_tools()}
    for name in ("text_to_speech", "speech_to_text"):
        assert "ctx" not in tools[name].input_schema["properties"]


@pytest.mark.asyncio
async def test_one_step_per_hosted_request_and_one_for_the_join(groq_key, fake_aspeech, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    fake_aspeech(written_files=[])
    monkeypatch.setattr(voice_io, "_join_wavs", lambda parts, target: target.write_bytes(b"joined"))
    text = "word " * 100
    n = len(voice_io._tts_chunks(text))
    ctx = FakeContext()

    await voice_io.text_to_speech_tool(text=text, ctx=ctx)

    assert [c[0] for c in ctx.calls] == list(range(1, n + 2))
    assert all(c[1] == n + 1 for c in ctx.calls)
    assert ctx.calls[0][2] == f"Groq: part 1 of {n} synthesized"
    assert ctx.calls[-1][2] == f"joined {n} parts"


@pytest.mark.asyncio
async def test_single_request_reports_one_step(groq_key, fake_aspeech, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    fake_aspeech(written_files=[])
    ctx = FakeContext()
    await voice_io.text_to_speech_tool(text="hello", ctx=ctx)
    assert ctx.calls == [(1, 1, "Groq: part 1 of 1 synthesized")]


@pytest.mark.asyncio
async def test_local_tts_reports_start_and_done(no_groq_key, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(voice_io, "_local_text_to_speech",
                        lambda text, filepath, voice="af_heart": filepath.write_bytes(b"x") or True)
    ctx = FakeContext()
    await voice_io.text_to_speech_tool(text="hello", ctx=ctx)
    assert ctx.calls == [(0, 1, "local:kokoro-82m: synthesizing"), (1, 1, "local:kokoro-82m: done")]


@pytest.mark.asyncio
async def test_a_failing_progress_channel_does_not_trigger_the_fallback(groq_key, fake_aspeech, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    fake_aspeech(written_files=[])
    called = []
    monkeypatch.setattr(voice_io, "_local_text_to_speech", lambda *a, **k: called.append(1) or True)

    result = await voice_io.text_to_speech_tool(text="hello", ctx=FakeContext(fail=True))

    assert result.structured_content["tier"] == "hosted"
    assert called == [], "progress errors must never look like a Groq failure"


@pytest.mark.asyncio
async def test_without_a_context_nothing_is_sent(groq_key, fake_aspeech, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    fake_aspeech(written_files=[])
    result = await voice_io.text_to_speech_tool(text="hello")
    assert result.structured_content["tier"] == "hosted"


def _wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 160)
    return path


@pytest.mark.asyncio
async def test_stt_hosted_reports_upload_and_done(groq_key, fake_atranscription, tmp_path):
    fake_atranscription(text="ok")
    ctx = FakeContext()
    await voice_io.speech_to_text_tool(audio_path=str(_wav(tmp_path / "a.wav")), ctx=ctx)
    assert [c[0] for c in ctx.calls] == [0, 1]
    assert ctx.calls[0][2].startswith("Groq: uploading 0.0MB")


@pytest.mark.asyncio
async def test_stt_local_reports_start_and_done(no_groq_key, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "_local_speech_to_text", lambda audio: "words")
    ctx = FakeContext()
    await voice_io.speech_to_text_tool(audio_path=str(_wav(tmp_path / "a.wav")), ctx=ctx)
    assert ctx.calls == [(0, 1, "local:faster-whisper: transcribing"), (1, 1, "local:faster-whisper: done")]


@pytest.mark.asyncio
async def test_a_refused_file_sends_no_progress(groq_key, tmp_path):
    from mcp.server.mcpserver.exceptions import ToolError

    secret = tmp_path / "notes.txt"
    secret.write_text("x")
    ctx = FakeContext()
    with pytest.raises(ToolError):
        await voice_io.speech_to_text_tool(audio_path=str(secret), ctx=ctx)
    assert ctx.calls == []
