"""list_voices, the _redact() defense-in-depth helper, and OUTPUT_DIR's
environment-driven default."""
import importlib
from pathlib import Path

import pytest

import voice_io


@pytest.mark.asyncio
async def test_list_voices_includes_the_default_voice():
    result = await voice_io.list_voices()
    assert voice_io.DEFAULT_VOICE in result.splitlines()


@pytest.mark.asyncio
async def test_list_voices_returns_every_known_voice_one_per_line():
    result = await voice_io.list_voices()
    assert result.splitlines() == voice_io.KNOWN_VOICES


def test_redact_replaces_the_secret():
    assert voice_io._redact("error: bad key gsk_abc123 rejected", "gsk_abc123") == "error: bad key *** rejected"


def test_redact_is_a_noop_when_secret_is_none_or_empty():
    assert voice_io._redact("some error", None) == "some error"
    assert voice_io._redact("some error", "") == "some error"


def test_redact_is_a_noop_when_secret_not_present():
    assert voice_io._redact("some error", "unrelated-key") == "some error"


def test_output_dir_honors_the_environment_override(monkeypatch, tmp_path):
    """OUTPUT_DIR is resolved at import time, so this reloads the module -
    reload mutates the existing module object in place, leaving every other
    test's `import voice_io` reference valid."""
    custom = tmp_path / "custom-audio-out"
    monkeypatch.setenv("VOICE_IO_OUTPUT_DIR", str(custom))
    try:
        importlib.reload(voice_io)
        assert voice_io.OUTPUT_DIR == custom
    finally:
        monkeypatch.delenv("VOICE_IO_OUTPUT_DIR", raising=False)
        importlib.reload(voice_io)


def test_output_dir_defaults_under_the_cwd_not_the_installed_package(monkeypatch):
    """The original bug: writing into Path(__file__).parent puts generated
    audio inside site-packages for a pip-installed user."""
    monkeypatch.delenv("VOICE_IO_OUTPUT_DIR", raising=False)
    importlib.reload(voice_io)
    assert voice_io.OUTPUT_DIR == Path.cwd() / "output"


@pytest.mark.asyncio
async def test_a_failing_hosted_call_writes_nothing_to_stdout(monkeypatch, tmp_path, capfd):
    """stdout is the MCP JSON-RPC channel. A real (unmocked) litellm call
    against a closed local port must fail without printing into it -
    litellm's default is a multi-line "Give Feedback" banner."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_never_sent_anywhere")
    monkeypatch.setenv("GROQ_API_BASE", "http://localhost:9")  # discard port: nothing listens here
    monkeypatch.setattr(voice_io, "HOSTED_MAX_RETRIES", 0)
    audio = tmp_path / "clip.wav"
    audio.write_bytes(voice_io._tiny_silent_wav().read())

    result = await voice_io.speech_to_text(str(audio))

    assert "Speech-to-text failed" in result
    assert "gsk_test_key_never_sent_anywhere" not in result
    assert capfd.readouterr().out == ""
