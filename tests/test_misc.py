"""list_voices and the _redact() defense-in-depth helper."""
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
