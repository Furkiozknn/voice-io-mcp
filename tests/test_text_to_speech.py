"""text_to_speech: Groq's hosted playai-tts first, local Kokoro fallback if
Groq fails or GROQ_API_KEY isn't set."""
import pytest

import voice_io


@pytest.mark.asyncio
async def test_rejects_empty_text():
    with pytest.raises(ValueError, match="empty"):
        await voice_io.text_to_speech(text="   ")


@pytest.mark.asyncio
async def test_rejects_invalid_output_format():
    with pytest.raises(ValueError, match="output_format"):
        await voice_io.text_to_speech(text="hello", output_format="ogg")


@pytest.mark.asyncio
async def test_succeeds_via_groq(groq_key, fake_aspeech, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    written = []
    fake_aspeech(written_files=written)

    result = await voice_io.text_to_speech(text="hello world")

    assert "model: groq/playai-tts" in result
    assert len(written) == 1
    assert written[0].endswith(".mp3")


@pytest.mark.asyncio
async def test_falls_back_to_local_when_groq_fails(groq_key, fake_aspeech, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    fake_aspeech(side_effect=RuntimeError("Groq overloaded"))
    monkeypatch.setattr(voice_io, "_local_text_to_speech", lambda text, filepath, voice="af_heart": (
        filepath.write_bytes(b"fake-wav-bytes") or True
    ))

    result = await voice_io.text_to_speech(text="hello world")

    assert "model: local:kokoro-82m" in result
    assert "requested format ignored" in result  # default output_format is mp3
    saved = list(tmp_path.glob("speech_*.wav"))
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_falls_back_to_local_without_format_note_when_wav_requested(
    groq_key, fake_aspeech, tmp_path, monkeypatch
):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    fake_aspeech(side_effect=RuntimeError("Groq overloaded"))
    monkeypatch.setattr(
        voice_io, "_local_text_to_speech", lambda text, filepath, voice="af_heart": filepath.write_bytes(b"x") or True
    )

    result = await voice_io.text_to_speech(text="hello", output_format="wav")

    assert "requested format ignored" not in result


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

    result = await voice_io.text_to_speech(text="hello world")

    assert "Text-to-speech failed" in result
    assert "Groq overloaded" in result
    assert "local-tts" in result


@pytest.mark.asyncio
async def test_reports_clear_error_when_no_key_and_no_local_fallback_installed(no_groq_key, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)

    result = await voice_io.text_to_speech(text="hello world")

    assert "GROQ_API_KEY not set" in result
    assert "local-tts" in result


@pytest.mark.asyncio
async def test_rapid_calls_do_not_collide_on_the_same_filename(groq_key, fake_aspeech, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "OUTPUT_DIR", tmp_path)
    written = []
    fake_aspeech(written_files=written)

    result_a = await voice_io.text_to_speech(text="first call")
    result_b = await voice_io.text_to_speech(text="second call")

    # Two calls landing in the same wall-clock second must not overwrite
    # each other's file - microsecond precision in the timestamp guarantees this.
    assert result_a != result_b
    saved = list(tmp_path.glob("speech_*.mp3"))
    assert len(saved) == 2


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

    leftover_mp3s = list(tmp_path.glob("speech_*.mp3"))
    assert leftover_mp3s == []  # the partially-written mp3 must be cleaned up, not orphaned


def test_local_text_to_speech_returns_false_when_dependency_missing(tmp_path):
    """The real function, unmocked: if kokoro truly isn't installed, it must
    fail closed (return False) rather than raise."""
    voice_io._local_tts_pipeline = None

    result = voice_io._local_text_to_speech("hello", tmp_path / "out.wav")

    assert result is False
