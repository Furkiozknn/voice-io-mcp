"""speech_to_text: Groq's hosted whisper-large-v3-turbo first, local
faster-whisper fallback if Groq fails or GROQ_API_KEY isn't set."""
import pytest

import voice_io


@pytest.fixture
def sample_audio(tmp_path):
    path = tmp_path / "sample.wav"
    path.write_bytes(b"RIFF....WAVEfake-audio-bytes")
    return str(path)


@pytest.mark.asyncio
async def test_reports_file_not_found():
    result = await voice_io.speech_to_text(audio_path="/nonexistent/file.wav")
    assert result == "File not found: /nonexistent/file.wav"


@pytest.mark.asyncio
async def test_rejects_unrecognized_file_extension(tmp_path):
    # speech_to_text reads whatever local path it's given and uploads it to
    # Groq - refusing anything that isn't a known audio extension stops it
    # from being used to read and exfiltrate an arbitrary file (e.g. a
    # crafted path pointing at .env or a config file).
    suspicious = tmp_path / "not-audio.env"
    suspicious.write_text("GROQ_API_KEY=super-secret\n")

    result = await voice_io.speech_to_text(audio_path=str(suspicious))

    assert "Rejected" in result
    assert "not a recognized audio format" in result


@pytest.mark.asyncio
async def test_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "MAX_AUDIO_BYTES", 10)  # tiny cap for the test
    big_file = tmp_path / "big.wav"
    big_file.write_bytes(b"x" * 100)

    result = await voice_io.speech_to_text(audio_path=str(big_file))

    assert "Rejected" in result
    assert "exceeds" in result


@pytest.mark.asyncio
async def test_succeeds_via_groq(groq_key, fake_atranscription, sample_audio):
    fake_atranscription(text="hello from groq")

    result = await voice_io.speech_to_text(audio_path=sample_audio)

    assert "hello from groq" in result
    assert "model: groq/whisper-large-v3-turbo" in result


@pytest.mark.asyncio
async def test_falls_back_to_local_when_groq_fails(groq_key, fake_atranscription, sample_audio, monkeypatch):
    fake_atranscription(side_effect=RuntimeError("Groq overloaded"))
    monkeypatch.setattr(voice_io, "_local_speech_to_text", lambda path: "hello from local whisper")

    result = await voice_io.speech_to_text(audio_path=sample_audio)

    assert "hello from local whisper" in result
    assert "model: local:faster-whisper" in result


@pytest.mark.asyncio
async def test_uses_local_fallback_directly_when_no_groq_key_configured(no_groq_key, sample_audio, monkeypatch):
    called = {}

    def fake_local(path):
        called["path"] = path
        return "transcribed locally"

    monkeypatch.setattr(voice_io, "_local_speech_to_text", fake_local)

    result = await voice_io.speech_to_text(audio_path=sample_audio)

    assert "transcribed locally" in result
    assert called["path"] == sample_audio


@pytest.mark.asyncio
async def test_reports_clear_error_when_groq_fails_and_no_local_fallback_installed(
    groq_key, fake_atranscription, sample_audio
):
    # faster-whisper is deliberately not installed in the base test env
    # (optional extra) - exercises the real ImportError path.
    fake_atranscription(side_effect=RuntimeError("Groq overloaded"))

    result = await voice_io.speech_to_text(audio_path=sample_audio)

    assert "Speech-to-text failed" in result
    assert "Groq overloaded" in result
    assert "local-stt" in result


@pytest.mark.asyncio
async def test_reports_clear_error_when_no_key_and_no_local_fallback_installed(no_groq_key, sample_audio):
    result = await voice_io.speech_to_text(audio_path=sample_audio)

    assert "GROQ_API_KEY not set" in result
    assert "local-stt" in result


def test_local_speech_to_text_returns_none_when_dependency_missing(sample_audio):
    """The real function, unmocked: if faster-whisper truly isn't installed,
    it must fail closed (return None) rather than raise."""
    voice_io._local_stt_model = None

    result = voice_io._local_speech_to_text(sample_audio)

    assert result is None


@pytest.mark.asyncio
async def test_hosted_stt_call_is_bounded_by_a_timeout(groq_key, fake_atranscription, tmp_path):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake-mp3")
    mock = fake_atranscription(text="hi")

    await voice_io.speech_to_text(str(audio))

    assert mock.await_args.kwargs["timeout"] == voice_io.HOSTED_CALL_TIMEOUT
