"""text_to_speech: Groq's hosted Orpheus first, local Kokoro fallback if
Groq fails or GROQ_API_KEY isn't set."""
import pytest
from mcp.server.mcpserver.exceptions import ToolError

import voice_io


@pytest.mark.asyncio
async def test_rejects_empty_text():
    with pytest.raises(ToolError, match="empty"):
        await voice_io.text_to_speech(text="   ")


@pytest.mark.asyncio
async def test_rejects_invalid_output_format():
    with pytest.raises(ToolError, match="output_format"):
        await voice_io.text_to_speech(text="hello", output_format="ogg")


@pytest.mark.asyncio
async def test_succeeds_via_groq(groq_key, fake_aspeech, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    written = []
    fake_aspeech(written_files=written)

    result = await voice_io.text_to_speech(text="hello world")

    assert "model: groq/canopylabs/orpheus-v1-english" in result
    assert "requested format ignored" not in result
    assert len(written) == 1
    assert written[0].endswith(".wav")


@pytest.mark.asyncio
async def test_falls_back_to_local_when_groq_fails(groq_key, fake_aspeech, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    fake_aspeech(side_effect=RuntimeError("Groq overloaded"))
    monkeypatch.setattr(voice_io, "_local_text_to_speech", lambda text, filepath, voice="af_heart": (
        filepath.write_bytes(b"fake-wav-bytes") or True
    ))

    result = await voice_io.text_to_speech(text="hello world")

    assert "model: local:kokoro-82m" in result
    assert "requested format ignored" not in result  # default output_format is wav
    saved = list(tmp_path.glob("speech_*.wav"))
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_mp3_request_is_answered_with_wav_and_says_so(
    groq_key, fake_aspeech, tmp_path, monkeypatch
):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    fake_aspeech(side_effect=RuntimeError("Groq overloaded"))
    monkeypatch.setattr(
        voice_io, "_local_text_to_speech", lambda text, filepath, voice="af_heart": filepath.write_bytes(b"x") or True
    )

    result = await voice_io.text_to_speech(text="hello", output_format="mp3")

    assert "requested format ignored" in result
    assert list(tmp_path.glob("speech_*.mp3")) == []


@pytest.mark.asyncio
async def test_uses_local_fallback_directly_when_no_groq_key_configured(
    no_groq_key, tmp_path, monkeypatch
):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    called = {}

    def fake_local(text, filepath, voice="af_heart"):
        called["text"] = text
        filepath.write_bytes(b"x")
        return True

    monkeypatch.setattr(voice_io, "_local_text_to_speech", fake_local)

    result = await voice_io.text_to_speech(text="hello world")

    assert "model: local:kokoro-82m" in result
    assert called["text"] == "hello world"


@pytest.mark.asyncio
async def test_reports_clear_error_when_groq_fails_and_no_local_fallback_installed(
    groq_key, fake_aspeech, tmp_path, monkeypatch
):
    # The base test environment deliberately does not install the optional
    # local-tts extra (kokoro) - this exercises the real, unmocked
    # _local_text_to_speech ImportError path, not a mocked one.
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    fake_aspeech(side_effect=RuntimeError("Groq overloaded"))

    # Raised, not returned: no audio came out, so the MCP result must carry
    # isError=true - as a plain string it read as a success to clients.
    with pytest.raises(ToolError) as excinfo:
        await voice_io.text_to_speech(text="hello world")

    message = str(excinfo.value)
    assert "Text-to-speech failed" in message
    assert "Groq overloaded" in message
    assert "local-tts" in message


@pytest.mark.asyncio
async def test_reports_clear_error_when_no_key_and_no_local_fallback_installed(no_groq_key, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)

    with pytest.raises(ToolError) as excinfo:
        await voice_io.text_to_speech(text="hello world")

    assert "GROQ_API_KEY not set" in str(excinfo.value)
    assert "local-tts" in str(excinfo.value)


@pytest.mark.asyncio
async def test_rapid_calls_do_not_collide_on_the_same_filename(groq_key, fake_aspeech, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    written = []
    fake_aspeech(written_files=written)

    result_a = await voice_io.text_to_speech(text="first call")
    result_b = await voice_io.text_to_speech(text="second call")

    # Two calls landing in the same wall-clock second must not overwrite
    # each other's file.
    assert result_a != result_b
    assert len(set(written)) == 2


@pytest.mark.asyncio
async def test_partial_file_is_cleaned_up_when_groq_write_fails_after_a_successful_call(
    groq_key, tmp_path, monkeypatch
):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)

    # Simulate: the hosted call itself succeeds, but writing the response to
    # disk fails partway (disk full, permission error, ...) - the orphaned
    # file must not be left behind once the tool falls back to local.
    class FailingWriteResponse:
        def stream_to_file(self, path):
            open(path, "wb").write(b"partial")  # a real partial file gets created...
            raise OSError("disk full")

    from unittest.mock import AsyncMock

    monkeypatch.setattr(voice_io.litellm, "aspeech", AsyncMock(return_value=FailingWriteResponse()))
    monkeypatch.setattr(
        voice_io, "_local_text_to_speech", lambda text, filepath, voice="af_heart": filepath.write_bytes(b"x") or True
    )

    await voice_io.text_to_speech(text="hello world")

    # Only the local fallback's own file may remain - the partial hosted
    # write must be cleaned up, not orphaned or mistaken for the result.
    assert [p.read_bytes() for p in tmp_path.glob("speech_*.wav")] == [b"x"]


def test_local_text_to_speech_returns_false_when_dependency_missing(tmp_path):
    """The real function, unmocked: if kokoro truly isn't installed, it must
    fail closed (return False) rather than raise."""
    voice_io._local_tts_pipeline = None

    result = voice_io._local_text_to_speech("hello", tmp_path / "out.wav")

    assert result is False


@pytest.mark.asyncio
async def test_hosted_tts_call_is_bounded_by_a_timeout(groq_key, fake_aspeech, tmp_path, monkeypatch):
    """litellm's own default is 600s; every real hosted call must pass an
    explicit bound so a wedged provider can't hold the tool for 10 minutes."""
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    mock = fake_aspeech(written_files=[])

    await voice_io.text_to_speech(text="hello world")

    assert mock.await_args.kwargs["timeout"] == voice_io.HOSTED_CALL_TIMEOUT
    assert mock.await_args.kwargs["max_retries"] == voice_io.HOSTED_MAX_RETRIES


def _silent_wav_bytes(frames: int) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


def test_short_text_is_one_chunk():
    assert voice_io._tts_chunks("  hello   world ") == ["hello world"]


def test_long_text_splits_at_sentence_ends_within_the_limit():
    sentence = "This sentence is exactly fifty characters long ok. "
    chunks = voice_io._tts_chunks(sentence * 9)
    assert all(len(c) <= voice_io.TTS_MAX_CHARS for c in chunks)
    assert all(c.endswith(".") for c in chunks)
    assert " ".join(chunks) == (sentence * 9).strip()


def test_a_word_longer_than_the_limit_is_cut():
    chunks = voice_io._tts_chunks("x" * 450)
    assert [len(c) for c in chunks] == [200, 200, 50]


@pytest.mark.asyncio
async def test_long_text_is_sent_in_pieces_and_joined(groq_key, tmp_path, monkeypatch):
    """Orpheus takes at most 200 characters per request: longer text must
    become several requests whose WAVs are joined into one file, with the
    part files removed afterwards."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)

    class WavResponse:
        def stream_to_file(self, path):
            open(path, "wb").write(_silent_wav_bytes(100))

    mock = AsyncMock(return_value=WavResponse())
    monkeypatch.setattr(voice_io.litellm, "aspeech", mock)

    result = await voice_io.text_to_speech(text="One more sentence here. " * 20)

    inputs = [c.kwargs["input"] for c in mock.await_args_list]
    assert len(inputs) > 1
    assert all(len(i) <= voice_io.TTS_MAX_CHARS for i in inputs)
    assert all(c.kwargs["response_format"] == "wav" for c in mock.await_args_list)
    assert f"{len(inputs)} requests" in result

    import wave

    (saved,) = tmp_path.glob("speech_*.wav")
    with wave.open(str(saved), "rb") as w:
        assert w.getnframes() == 100 * len(inputs)


def test_join_refuses_parts_with_different_formats(tmp_path):
    import wave

    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    a.write_bytes(_silent_wav_bytes(10))
    with wave.open(str(b), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00" * 40)
    with pytest.raises(ValueError, match="disagree"):
        voice_io._join_wavs([a, b], tmp_path / "out.wav")


def test_stamps_stay_unique_even_when_the_clock_does_not_move(monkeypatch):
    """Windows' wall clock advances in coarse steps, so two calls can read
    the very same microsecond - the stem must still differ."""
    from datetime import datetime as real_datetime

    class FrozenClock:
        @staticmethod
        def now():
            return real_datetime(2026, 9, 25, 12, 0, 0, 0)

    monkeypatch.setattr(voice_io, "datetime", FrozenClock)

    assert len({voice_io._stamp() for _ in range(50)}) == 50


@pytest.mark.asyncio
async def test_rejects_text_over_the_length_cap_before_any_request(groq_key, fake_aspeech, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    mock = fake_aspeech()

    with pytest.raises(ToolError, match="4000-character limit"):
        await voice_io.text_to_speech(text="x" * (voice_io.MAX_TTS_TEXT_CHARS + 1))

    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_unwritable_output_dir_is_a_clear_tool_error(monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file where the output directory should be")
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", blocker / "output")

    with pytest.raises(ToolError, match="Cannot create output directory .*VOICE_IO_OUTPUT_DIR"):
        await voice_io.text_to_speech(text="hello")


@pytest.mark.asyncio
async def test_output_format_schema_is_an_enum():
    tools = {t.name: t for t in await voice_io.mcp.list_tools()}
    schema = tools["text_to_speech"].input_schema["properties"]["output_format"]
    assert schema.get("enum") == ["wav", "mp3"]
