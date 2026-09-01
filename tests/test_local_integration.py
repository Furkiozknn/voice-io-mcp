"""Real (unmocked) integration tests against the actual Kokoro / faster-
whisper API surface - skipped automatically when the relevant optional
extra isn't installed (the base test env / default CI run deliberately
doesn't install either, to stay fast and dependency-light).

Every other test file in this repo mocks the local fallback functions
directly, which means the real `KPipeline(...)`/`WhisperModel(...)` API
usage in voice_io.py is otherwise never actually executed by any test -
these two tests close that gap for anyone working on the local-fallback
code specifically: `uv sync --extra local-tts --extra local-stt && uv run
pytest tests/test_local_integration.py -v`.
"""
import importlib.util

import pytest

import voice_io

_KOKORO_INSTALLED = importlib.util.find_spec("kokoro") is not None
_FASTER_WHISPER_INSTALLED = importlib.util.find_spec("faster_whisper") is not None


@pytest.mark.skipif(not _KOKORO_INSTALLED, reason="local-tts extra not installed (uv sync --extra local-tts)")
def test_real_kokoro_synthesis_produces_a_nonempty_wav_file(tmp_path):
    voice_io._local_tts_pipeline = None
    out_path = tmp_path / "real_output.wav"

    ok = voice_io._local_text_to_speech("Hello, this is a real local synthesis test.", out_path)

    assert ok is True
    assert out_path.exists()
    assert out_path.stat().st_size > 0


@pytest.mark.skipif(
    not _FASTER_WHISPER_INSTALLED, reason="local-stt extra not installed (uv sync --extra local-stt)"
)
def test_real_faster_whisper_transcribes_without_crashing(tmp_path):
    voice_io._local_stt_model = None
    # A silent WAV won't produce a meaningful transcript, but this exercises
    # the real WhisperModel(...).transcribe(...) call end to end - the thing
    # most likely to break on a faster-whisper API change, independent of
    # what the (empty, for silence) transcript actually says.
    audio_path = tmp_path / "silence.wav"
    audio_path.write_bytes(voice_io._tiny_silent_wav().read())

    result = voice_io._local_speech_to_text(str(audio_path))

    assert result is not None
    assert isinstance(result, str)
