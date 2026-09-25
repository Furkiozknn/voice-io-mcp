"""speech_to_text: Groq's hosted whisper-large-v3-turbo first, local
faster-whisper fallback if Groq fails or GROQ_API_KEY isn't set."""
import os
import sys

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import voice_io


@pytest.fixture
def sample_audio(tmp_path):
    path = tmp_path / "sample.wav"
    path.write_bytes(b"RIFF....WAVEfake-audio-bytes")
    return str(path)


# Bad input raises ToolError, exactly as text_to_speech already does for its
# own invalid arguments - the two tools must report a caller mistake the same
# way. (A tier that merely failed still returns a plain string; that is a
# result, not a caller mistake.)

@pytest.mark.asyncio
async def test_reports_file_not_found():
    with pytest.raises(ToolError, match="File not found: /nonexistent/file.wav"):
        await voice_io.speech_to_text(audio_path="/nonexistent/file.wav")


@pytest.mark.asyncio
async def test_rejects_unrecognized_file_extension(tmp_path):
    # speech_to_text reads whatever local path it's given and uploads it to
    # Groq - refusing anything that isn't a known audio extension stops it
    # from being used to read and exfiltrate an arbitrary file (e.g. a
    # crafted path pointing at .env or a config file).
    suspicious = tmp_path / "not-audio.env"
    suspicious.write_text("GROQ_API_KEY=super-secret\n")

    with pytest.raises(ToolError, match="not a recognized audio format") as excinfo:
        await voice_io.speech_to_text(audio_path=str(suspicious))

    assert "Rejected" in str(excinfo.value)


@pytest.mark.asyncio
async def test_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_io, "MAX_AUDIO_BYTES", 10)  # tiny cap for the test
    big_file = tmp_path / "big.wav"
    big_file.write_bytes(b"x" * 100)

    with pytest.raises(ToolError, match="exceeds") as excinfo:
        await voice_io.speech_to_text(audio_path=str(big_file))

    assert "Rejected" in str(excinfo.value)


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

    def fake_local(audio):
        called["bytes"] = audio.read()
        return "transcribed locally"

    monkeypatch.setattr(voice_io, "_local_speech_to_text", fake_local)

    result = await voice_io.speech_to_text(audio_path=sample_audio)

    assert "transcribed locally" in result
    # The local tier gets the bytes that were validated, not the path to
    # reopen - so the file cannot be swapped between check and read.
    assert called["bytes"] == b"RIFF....WAVEfake-audio-bytes"


@pytest.mark.asyncio
async def test_reports_clear_error_when_groq_fails_and_no_local_fallback_installed(
    groq_key, fake_atranscription, sample_audio
):
    # faster-whisper is deliberately not installed in the base test env
    # (optional extra) - exercises the real ImportError path.
    fake_atranscription(side_effect=RuntimeError("Groq overloaded"))

    with pytest.raises(ToolError) as excinfo:
        await voice_io.speech_to_text(audio_path=sample_audio)

    message = str(excinfo.value)
    assert "Speech-to-text failed" in message
    assert "Groq overloaded" in message
    assert "local-stt" in message


@pytest.mark.asyncio
async def test_reports_clear_error_when_no_key_and_no_local_fallback_installed(no_groq_key, sample_audio):
    with pytest.raises(ToolError) as excinfo:
        await voice_io.speech_to_text(audio_path=sample_audio)

    assert "GROQ_API_KEY not set" in str(excinfo.value)
    assert "local-stt" in str(excinfo.value)


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
    assert mock.await_args.kwargs["max_retries"] == voice_io.HOSTED_MAX_RETRIES


# --- path safety: the file is uploaded to a third party, so every way of
# --- making an innocent-looking path read something else must be refused.


@pytest.fixture
def secret_file(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("GROQ_API_KEY=super-secret\n")
    return secret


def _symlink_or_skip(link, target):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available here")


@pytest.mark.asyncio
async def test_rejects_an_audio_named_symlink_to_a_secret(groq_key, fake_atranscription, tmp_path, secret_file):
    mock = fake_atranscription(text="should never be called")
    link = tmp_path / "memo.wav"
    _symlink_or_skip(link, secret_file)

    with pytest.raises(ToolError, match="link target '.env'"):
        await voice_io.speech_to_text(audio_path=str(link))

    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_follows_a_symlink_whose_target_is_audio(groq_key, fake_atranscription, tmp_path, sample_audio):
    mock = fake_atranscription(text="linked audio")
    link = tmp_path / "alias.mp3"
    _symlink_or_skip(link, sample_audio)

    result = await voice_io.speech_to_text(audio_path=str(link))

    assert "linked audio" in result
    assert mock.await_args.kwargs["file"].read() == b"RIFF....WAVEfake-audio-bytes"


@pytest.mark.asyncio
async def test_rejects_a_directory_with_an_audio_name(tmp_path):
    folder = tmp_path / "album.wav"
    folder.mkdir()

    with pytest.raises(ToolError, match="not a regular file"):
        await voice_io.speech_to_text(audio_path=str(folder))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
@pytest.mark.asyncio
async def test_rejects_a_fifo_without_hanging(tmp_path):
    fifo = tmp_path / "stream.wav"
    os.mkfifo(fifo)

    with pytest.raises(ToolError, match="not a regular file"):
        await voice_io.speech_to_text(audio_path=str(fifo))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/dev/zero is a Linux device")
@pytest.mark.asyncio
async def test_rejects_a_device_behind_an_audio_named_symlink(tmp_path):
    link = tmp_path / "endless.wav"
    _symlink_or_skip(link, "/dev/zero")

    with pytest.raises(ToolError, match="Rejected"):
        await voice_io.speech_to_text(audio_path=str(link))


@pytest.mark.asyncio
async def test_extension_check_is_case_insensitive(groq_key, fake_atranscription, tmp_path):
    fake_atranscription(text="upper case ok")
    loud = tmp_path / "MEMO.WAV"
    loud.write_bytes(b"RIFF")

    assert "upper case ok" in await voice_io.speech_to_text(audio_path=str(loud))


@pytest.mark.asyncio
async def test_a_nonexistent_non_audio_path_is_refused_without_probing_it():
    # The extension is checked before the filesystem is touched, so the
    # answer does not reveal whether some private file exists.
    with pytest.raises(ToolError, match="not a recognized audio format"):
        await voice_io.speech_to_text(audio_path="/nonexistent/private.key")


@pytest.mark.asyncio
async def test_a_nul_byte_in_the_path_is_refused_with_a_designed_message():
    # Without the explicit check Path.resolve raises ValueError, which the
    # SDK masks as a bare "Error executing tool speech_to_text".
    with pytest.raises(ToolError, match="NUL byte"):
        await voice_io.speech_to_text(audio_path="/tmp/memo\x00.wav")


@pytest.mark.asyncio
async def test_a_hard_link_is_indistinguishable_from_the_file_it_names(
    groq_key, fake_atranscription, tmp_path, secret_file
):
    """Documents the limit of the path checks rather than a guarantee: a
    hard link *is* a second name for the same file, so `notes.wav` hard-
    linked to a secret is read like any other audio-named regular file.
    The README says so under Known limitations; if this ever starts
    failing, that text is out of date."""
    mock = fake_atranscription(text="uploaded")
    alias = tmp_path / "notes.wav"
    try:
        os.link(secret_file, alias)
    except (OSError, NotImplementedError):
        pytest.skip("hard links not available here")

    await voice_io.speech_to_text(audio_path=str(alias))

    assert mock.await_args.kwargs["file"].read() == secret_file.read_bytes()
