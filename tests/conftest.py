"""Shared pytest fixtures for the voice-io-mcp test suite.

No test makes a real network call or needs GROQ_API_KEY - every litellm call
is mocked, and the local-fallback tests exercise the real (unmocked) code
path against an environment where kokoro/faster-whisper are deliberately not
installed (they're optional extras), confirming the fail-closed behavior.
"""
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def groq_key(monkeypatch):
    """Make the module believe GROQ_API_KEY is set, without touching .env."""
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    return "test-groq-key"


@pytest.fixture
def no_groq_key(monkeypatch):
    """Make the module believe GROQ_API_KEY is NOT set."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    return None


class FakeSpeechResponse:
    """Minimal stand-in for litellm.aspeech's return value."""

    def __init__(self, written_files: list, raise_on_write: Exception | None = None):
        self._written_files = written_files
        self._raise_on_write = raise_on_write

    def stream_to_file(self, path: str) -> None:
        if self._raise_on_write:
            raise self._raise_on_write
        self._written_files.append(path)
        with open(path, "wb") as f:
            f.write(b"fake-audio-bytes")


class FakeTranscriptionResponse:
    def __init__(self, text: str):
        self.text = text


@pytest.fixture
def fake_aspeech(monkeypatch):
    """Patch voice_io.litellm.aspeech. Returns a factory: call it with the
    desired side_effect/return_value before the code under test runs."""
    import voice_io

    def _factory(side_effect=None, written_files=None):
        mock = AsyncMock(side_effect=side_effect)
        if side_effect is None:
            mock.return_value = FakeSpeechResponse(written_files if written_files is not None else [])
        monkeypatch.setattr(voice_io.litellm, "aspeech", mock)
        return mock

    return _factory


@pytest.fixture
def fake_atranscription(monkeypatch):
    """Patch voice_io.litellm.atranscription. Returns a factory: call it
    with the desired side_effect/return_value."""
    import voice_io

    def _factory(side_effect=None, text="transcribed text"):
        mock = AsyncMock(side_effect=side_effect)
        if side_effect is None:
            mock.return_value = FakeTranscriptionResponse(text)
        monkeypatch.setattr(voice_io.litellm, "atranscription", mock)
        return mock

    return _factory
