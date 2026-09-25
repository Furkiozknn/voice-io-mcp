"""check_provider_health: Groq liveness probes + local-dependency availability
checks, all failure-isolated from each other."""
from unittest.mock import AsyncMock

import httpx
import pytest

import voice_io


def _groq_models(monkeypatch, handler):
    """Route the quota-free probe's HTTP call to `handler` instead of the network."""
    seen = []

    def _record(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(voice_io, "_http_transport", httpx.MockTransport(_record))
    return seen


def _listing(*ids):
    return lambda request: httpx.Response(200, json={"object": "list", "data": [{"id": i} for i in ids]})


@pytest.mark.asyncio
async def test_default_check_spends_no_audio_quota(groq_key, monkeypatch):
    """The default check must not generate speech or upload audio - both
    count against the free tier every time the tool is called."""
    aspeech, atranscription = AsyncMock(), AsyncMock()
    monkeypatch.setattr(voice_io.litellm, "aspeech", aspeech)
    monkeypatch.setattr(voice_io.litellm, "atranscription", atranscription)
    seen = _groq_models(monkeypatch, _listing("canopylabs/orpheus-v1-english", "whisper-large-v3-turbo"))

    report = await voice_io.check_provider_health()

    aspeech.assert_not_awaited()
    atranscription.assert_not_awaited()
    assert len(seen) == 1 and seen[0].method == "GET" and seen[0].url.path.endswith("/models")
    assert seen[0].headers["authorization"] == f"Bearer {groq_key}"
    assert "OK  groq/canopylabs/orpheus-v1-english - listed" in report
    assert "OK  groq/whisper-large-v3-turbo - listed" in report


@pytest.mark.asyncio
async def test_default_check_reports_a_drifted_model_name(groq_key, monkeypatch):
    _groq_models(monkeypatch, _listing("whisper-large-v3-turbo", "playai-tts"))

    report = await voice_io.check_provider_health()

    assert "FAIL groq/canopylabs/orpheus-v1-english - not in Groq's model list" in report
    assert "OK  groq/whisper-large-v3-turbo - listed" in report


@pytest.mark.asyncio
async def test_default_check_reports_a_rejected_key(groq_key, monkeypatch):
    _groq_models(monkeypatch, lambda request: httpx.Response(401, json={"error": {"message": "Invalid API Key"}}))

    report = await voice_io.check_provider_health()

    assert "FAIL groq/canopylabs/orpheus-v1-english - key rejected (HTTP 401)" in report
    assert "FAIL groq/whisper-large-v3-turbo - key rejected (HTTP 401)" in report


@pytest.mark.asyncio
async def test_default_check_redacts_the_key_from_transport_errors(groq_key, monkeypatch):
    def boom(request):
        raise httpx.ConnectError(f"proxy refused request carrying {groq_key}")

    _groq_models(monkeypatch, boom)

    report = await voice_io.check_provider_health()

    assert groq_key not in report
    assert "proxy refused request carrying ***" in report


@pytest.mark.asyncio
async def test_reports_not_configured_when_no_groq_key(no_groq_key):
    report = await voice_io.check_provider_health()

    assert "FAIL groq/canopylabs/orpheus-v1-english - not configured" in report
    assert "FAIL groq/whisper-large-v3-turbo - not configured" in report


@pytest.mark.asyncio
async def test_reports_ok_when_groq_probes_succeed(groq_key, fake_aspeech, fake_atranscription):
    fake_aspeech()
    fake_atranscription()

    report = await voice_io.check_provider_health(live=True)

    assert "OK  groq/canopylabs/orpheus-v1-english - ok" in report or "OK groq/canopylabs/orpheus-v1-english - ok" in report
    assert "OK  groq/whisper-large-v3-turbo - ok" in report or "OK groq/whisper-large-v3-turbo - ok" in report


@pytest.mark.asyncio
async def test_isolates_tts_probe_failure_from_stt_probe(groq_key, fake_aspeech, fake_atranscription):
    fake_aspeech(side_effect=RuntimeError("tts down"))
    fake_atranscription()

    report = await voice_io.check_provider_health(live=True)

    assert "FAIL groq/canopylabs/orpheus-v1-english - error: tts down" in report
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
