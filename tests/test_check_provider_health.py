"""check_provider_health: Groq liveness probes + local-dependency availability
checks, all failure-isolated from each other."""
import pytest

import voice_io


@pytest.mark.asyncio
async def test_reports_not_configured_when_no_groq_key(no_groq_key):
    report = await voice_io.check_provider_health()

    assert "FAIL groq/playai-tts - not configured" in report
    assert "FAIL groq/whisper-large-v3-turbo - not configured" in report


@pytest.mark.asyncio
async def test_reports_ok_when_groq_probes_succeed(groq_key, fake_aspeech, fake_atranscription):
    fake_aspeech()
    fake_atranscription()

    report = await voice_io.check_provider_health()

    assert "OK  groq/playai-tts - ok" in report or "OK groq/playai-tts - ok" in report
    assert "OK  groq/whisper-large-v3-turbo - ok" in report or "OK groq/whisper-large-v3-turbo - ok" in report


@pytest.mark.asyncio
async def test_isolates_tts_probe_failure_from_stt_probe(groq_key, fake_aspeech, fake_atranscription):
    fake_aspeech(side_effect=RuntimeError("tts down"))
    fake_atranscription()

    report = await voice_io.check_provider_health()

    assert "FAIL groq/playai-tts - error: tts down" in report
    assert "OK  groq/whisper-large-v3-turbo - ok" in report or "OK groq/whisper-large-v3-turbo - ok" in report


def test_reports_local_dependencies_not_installed_in_base_test_env():
    # kokoro and faster-whisper are optional extras, deliberately not
    # installed in this repo's base dev dependencies.
    ok, detail = voice_io._probe_local_dependency("kokoro")
    assert ok is False
    assert "not installed" in detail

    ok, detail = voice_io._probe_local_dependency("faster_whisper")
    assert ok is False
    assert "not installed" in detail


def test_tiny_silent_wav_is_a_valid_wav_file():
    import wave

    buf = voice_io._tiny_silent_wav()

    with wave.open(buf, "rb") as w:
        assert w.getnchannels() == 1
        assert w.getframerate() == 16000
        assert w.getnframes() == 1600


def test_probe_local_dependency_recognizes_an_actually_installed_module():
    # pytest itself is always installed in the test env - a sanity check
    # that the "installed" branch of _probe_local_dependency actually works,
    # not just the "not installed" branch.
    ok, detail = voice_io._probe_local_dependency("pytest")
    assert ok is True
    assert detail == "installed"
