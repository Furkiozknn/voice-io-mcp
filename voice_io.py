"""voice-io-mcp: text-to-speech and speech-to-text as MCP tools.

Groq's OpenAI-compatible audio endpoints first (playai-tts / whisper-large-
v3-turbo - both genuinely free-tier, no credit card, per Groq's own rate-
limit docs as of 2026-09), falling back to a fully local, keyless model if
Groq is unreachable or GROQ_API_KEY isn't set at all: Kokoro-82M (Apache-2.0)
for speech, faster-whisper (MIT) for transcription. Both local fallbacks are
optional extras (`uv sync --extra local-tts` / `--extra local-stt`) so the
base install stays light - this tool works with zero API keys configured if
you enable them, unlike a hosted-only wrapper.

Model/voice names below follow Groq's public API documentation but were not
live-verified with a real key while building this (no key was available in
the build environment) - run check_provider_health once GROQ_API_KEY is set
to confirm, the same "don't trust a name from memory" discipline
nvidia-nim-mcp documents for its own model list.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import logging
import os
import threading
import wave
from datetime import datetime
from pathlib import Path

import litellm
from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

load_dotenv()

logger = logging.getLogger(__name__)

mcp = MCPServer("voice-io")

OUTPUT_DIR = Path(__file__).parent / "output"

GROQ_API_KEY_ENV = "GROQ_API_KEY"

TTS_MODEL = "groq/playai-tts"
DEFAULT_VOICE = "Fritz-PlayAI"
STT_MODEL = "groq/whisper-large-v3-turbo"

# Groq's PlayAI voice names, transcribed from Groq's public API documentation
# - like the model names above, not live-verified with a real key while
# building this. Run check_provider_health (or list_voices, which just
# returns this list) and expect it to drift over time.
KNOWN_VOICES = [
    "Arista-PlayAI", "Atlas-PlayAI", "Basil-PlayAI", "Briggs-PlayAI",
    "Calum-PlayAI", "Celeste-PlayAI", "Cheyenne-PlayAI", "Chip-PlayAI",
    "Cillian-PlayAI", "Deedee-PlayAI", "Fritz-PlayAI", "Gail-PlayAI",
    "Indigo-PlayAI", "Mamaw-PlayAI", "Mason-PlayAI", "Mikail-PlayAI",
    "Mitch-PlayAI", "Quinn-PlayAI", "Thunder-PlayAI",
]

# speech_to_text reads a caller-supplied local file and uploads its bytes to
# Groq (a third party) - both limits below exist so an untrusted or
# accidental path (anything from a wrong extension to a multi-GB file, or a
# deliberately-crafted "transcribe this" prompt pointing at a sensitive
# file) fails fast and locally instead of silently exfiltrating whatever is
# at that path. 25MB matches Groq/OpenAI's own documented Whisper upload
# ceiling.
ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".webm", ".mp4", ".mpeg", ".mpga"}
MAX_AUDIO_BYTES = 25 * 1024 * 1024

# Short timeout for health probes specifically - these are meant to be a
# quick "is it alive" check, not a real generation, so failing fast is correct.
HEALTH_PROBE_TIMEOUT = 8.0

# Passed to the real hosted TTS/STT calls. litellm's default is 600 seconds;
# a wedged provider would otherwise hold the tool for ten minutes before the
# local fallback even got a chance. Two minutes comfortably covers a 25MB
# upload plus transcription on the free tier.
HOSTED_CALL_TIMEOUT = 120.0

_LOCAL_TTS_MODEL_NAME = "kokoro-82m"
_KOKORO_LANG_CODE = "a"  # American English - must match the voice prefix (af_/am_)
_local_tts_pipeline = None  # lazy singleton
_local_tts_lock = threading.Lock()

_LOCAL_STT_MODEL_NAME = "faster-whisper"
_LOCAL_STT_MODEL_SIZE = "base"
_local_stt_model = None  # lazy singleton
_local_stt_lock = threading.Lock()


def _local_text_to_speech(text: str, filepath: Path, voice: str = "af_heart") -> bool:
    """Fully local, keyless TTS fallback via Kokoro-82M. Weights auto-download
    from Hugging Face Hub on first use (~300MB) - only used if Groq's hosted
    endpoint fails or GROQ_API_KEY isn't set. Returns False (never raises) if
    the optional dependency is missing or synthesis fails, so the caller can
    report a clean error instead of crashing.

    Always writes a .wav file regardless of the caller's requested format -
    Kokoro outputs raw 24kHz PCM natively, and encoding straight to mp3 via
    soundfile depends on the local libsndfile build, which isn't guaranteed
    across platforms. The caller is responsible for noting the format
    substitution to the user.
    """
    global _local_tts_pipeline
    try:
        import numpy as np
        import soundfile as sf

        if _local_tts_pipeline is None:
            with _local_tts_lock:
                if _local_tts_pipeline is None:
                    from kokoro import KPipeline

                    _local_tts_pipeline = KPipeline(lang_code=_KOKORO_LANG_CODE)

        segments = []
        for result in _local_tts_pipeline(text, voice=voice, speed=1.0, split_pattern=r"\n+"):
            audio = getattr(result, "audio", None)
            if audio is None and isinstance(result, tuple):
                audio = result[-1]
            if audio is None:
                continue
            if hasattr(audio, "numpy"):
                audio = audio.numpy()
            segments.append(audio)

        if not segments:
            return False

        combined = np.concatenate(segments)
        sf.write(str(filepath), combined, 24000)
        return True
    except Exception as e:
        logger.warning("local TTS fallback unavailable: %s", e)
        return False


def _local_speech_to_text(audio_path: str) -> str | None:
    """Fully local, keyless STT fallback via faster-whisper. Weights
    auto-download from Hugging Face Hub on first use - only used if Groq's
    hosted endpoint fails or GROQ_API_KEY isn't set. Returns None (never
    raises) if the optional dependency is missing or transcription fails."""
    global _local_stt_model
    try:
        if _local_stt_model is None:
            with _local_stt_lock:
                if _local_stt_model is None:
                    from faster_whisper import WhisperModel

                    _local_stt_model = WhisperModel(_LOCAL_STT_MODEL_SIZE, device="cpu", compute_type="int8")
        segments, _info = _local_stt_model.transcribe(audio_path)
        return " ".join(segment.text.strip() for segment in segments)
    except Exception as e:
        logger.warning("local STT fallback unavailable: %s", e)
        return None


def _stamp() -> str:
    """Microsecond-precision timestamp for output filenames - plain
    second-precision let two rapid tool calls collide and silently
    overwrite each other's audio file."""
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _read_audio_file(path: Path) -> io.BytesIO:
    """Read an audio file into an in-memory buffer with a `.name` attribute
    (some upload APIs use the filename for content-type sniffing)."""
    buf = io.BytesIO(path.read_bytes())
    buf.name = path.name
    return buf


def _redact(text: str, secret: str | None) -> str:
    """Scrub a known secret value out of an error/log string before it's
    returned to the caller or written to logs. Defense-in-depth: no known
    code path here should embed the raw API key in an exception's str(), but
    an underlying HTTP client doing so in some failure mode isn't fully
    ruled out, and this costs nothing to guard against unconditionally."""
    if not secret:
        return text
    return text.replace(secret, "***")


def _format_unavailable_message(action: str, hosted_error: str | None, local_extra: str) -> str:
    """The identical 'both tiers failed' message shape both tools return -
    factored out once so text_to_speech and speech_to_text can't drift
    apart in wording as this contract evolves."""
    detail = hosted_error or f"{GROQ_API_KEY_ENV} not set in .env"
    return (
        f"{action} failed (Groq: {detail}) and no local fallback available "
        f"(run `uv sync --extra {local_extra}` to enable one)."
    )


def _tiny_silent_wav() -> io.BytesIO:
    """A ~0.1s silent mono WAV, built with the stdlib `wave` module (no extra
    dependency) - just enough to be a valid audio file for a transcription
    liveness probe, without shipping a binary fixture in the repo."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)
    buf.seek(0)
    buf.name = "probe.wav"
    return buf


def _probe_local_dependency(module_name: str) -> tuple[bool, str]:
    """Check whether an optional local-fallback dependency is importable,
    without actually loading the (large, slow-to-load-on-first-use) model
    itself - a full load would defeat the point of a quick health check."""
    if importlib.util.find_spec(module_name) is None:
        return False, "not installed (optional extra not enabled)"
    return True, "installed"


async def _probe_groq_tts() -> tuple[bool, str]:
    key = os.environ.get(GROQ_API_KEY_ENV)
    if not key:
        return False, "not configured"
    try:
        await litellm.aspeech(
            model=TTS_MODEL, voice=DEFAULT_VOICE, input="hi", api_key=key, timeout=HEALTH_PROBE_TIMEOUT
        )
    except Exception as e:
        return False, f"error: {_redact(str(e), key)}"
    return True, "ok"


async def _probe_groq_stt() -> tuple[bool, str]:
    key = os.environ.get(GROQ_API_KEY_ENV)
    if not key:
        return False, "not configured"
    try:
        await litellm.atranscription(
            model=STT_MODEL, file=_tiny_silent_wav(), api_key=key, timeout=HEALTH_PROBE_TIMEOUT
        )
    except Exception as e:
        return False, f"error: {_redact(str(e), key)}"
    return True, "ok"


@mcp.tool()
async def text_to_speech(text: str, voice: str = DEFAULT_VOICE, output_format: str = "mp3") -> str:
    """Convert text to speech, saved to output/.

    Tries Groq's playai-tts first (free tier, no credit card - requires
    GROQ_API_KEY), falling back to a fully local, keyless model (Kokoro-82M,
    Apache-2.0) if Groq is unavailable or the key isn't set. The local
    fallback always produces a .wav file regardless of `output_format`
    (Kokoro's native output), and requires the optional `local-tts` extra
    (`uv sync --extra local-tts`) plus the `espeak-ng` system package for
    full quality on non-trivial or non-English text.

    Args:
        text: Text to speak.
        voice: Groq PlayAI voice name (e.g. "Fritz-PlayAI") - ignored by the
            local fallback, which always uses Kokoro's "af_heart" voice.
        output_format: "mp3" or "wav". Only honored on the Groq tier.
    """
    # ToolError, not ValueError: under mcp >= 2.1 a plain exception is
    # treated as a crash and masked to a generic "Error executing tool ..."
    # (verified against the installed SDK) - these two messages are designed
    # for the caller and must arrive intact.
    if output_format not in ("mp3", "wav"):
        raise ToolError(f"output_format must be 'mp3' or 'wav', got {output_format!r}")
    if not text.strip():
        raise ToolError("text must not be empty")

    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = _stamp()

    key = os.environ.get(GROQ_API_KEY_ENV)
    groq_error = None
    if key:
        hosted_path = OUTPUT_DIR / f"speech_{stamp}.{output_format}"
        try:
            response = await litellm.aspeech(
                model=TTS_MODEL, voice=voice, input=text, response_format=output_format, api_key=key,
                timeout=HOSTED_CALL_TIMEOUT,
            )
            # stream_to_file is a blocking disk write - run it off the event
            # loop like every other I/O call here, not inline in async def.
            await asyncio.to_thread(response.stream_to_file, str(hosted_path))
            return f"Audio saved to {hosted_path} (model: {TTS_MODEL})"
        except Exception as e:
            groq_error = _redact(str(e), key)
            # A partial/corrupt file can exist if the write started before
            # failing - never leave that behind silently.
            hosted_path.unlink(missing_ok=True)
            logger.warning("Groq TTS failed, falling back to local: %s", groq_error)

    local_path = OUTPUT_DIR / f"speech_{stamp}.wav"
    ok = await asyncio.to_thread(_local_text_to_speech, text, local_path)
    if ok:
        note = " (requested format ignored - local fallback always writes .wav)" if output_format != "wav" else ""
        return f"Audio saved to {local_path} (model: local:{_LOCAL_TTS_MODEL_NAME}){note}"

    return _format_unavailable_message("Text-to-speech", groq_error, "local-tts")


@mcp.tool()
async def speech_to_text(audio_path: str, language: str | None = None) -> str:
    """Transcribe a local audio file to text.

    Tries Groq's whisper-large-v3-turbo first (free tier, no credit card -
    requires GROQ_API_KEY), falling back to a fully local, keyless model
    (faster-whisper, MIT) if Groq is unavailable or the key isn't set. The
    local fallback requires the optional `local-stt` extra (`uv sync --extra
    local-stt`) and auto-downloads its model weights on first use.

    Args:
        audio_path: Absolute path to a local audio file (mp3/wav/m4a/flac/
            ogg/webm/mp4/mpeg/mpga), max 25MB.
        language: Optional ISO-639-1 language hint (e.g. "en"). Ignored by
            the local fallback, which auto-detects language.
    """
    path = Path(audio_path)
    if not path.is_file():
        return f"File not found: {audio_path}"
    # This tool reads whatever local file it's pointed at and uploads its
    # bytes to Groq (a third party) - an extension allow-list and size cap
    # up front stop it from being turned into a generic "read and exfiltrate
    # an arbitrary file" primitive by a wrong or maliciously-crafted path.
    if path.suffix.lower() not in ALLOWED_AUDIO_EXTENSIONS:
        return (
            f"Rejected: {path.suffix or '(no extension)'} is not a recognized audio format "
            f"(expected one of {sorted(ALLOWED_AUDIO_EXTENSIONS)})"
        )
    size = path.stat().st_size
    if size > MAX_AUDIO_BYTES:
        return f"Rejected: file is {size / (1024 * 1024):.1f}MB, exceeds the {MAX_AUDIO_BYTES // (1024 * 1024)}MB limit"

    key = os.environ.get(GROQ_API_KEY_ENV)
    groq_error = None
    if key:
        try:
            # Reading the file is a blocking disk read - run it off the
            # event loop, same rule as the TTS write above.
            audio_file = await asyncio.to_thread(_read_audio_file, path)
            response = await litellm.atranscription(
                model=STT_MODEL, file=audio_file, language=language, api_key=key,
                timeout=HOSTED_CALL_TIMEOUT,
            )
            return f"{response.text}\n\n(model: {STT_MODEL})"
        except Exception as e:
            groq_error = _redact(str(e), key)
            logger.warning("Groq STT failed, falling back to local: %s", groq_error)

    text = await asyncio.to_thread(_local_speech_to_text, str(path))
    if text is not None:
        return f"{text}\n\n(model: local:{_LOCAL_STT_MODEL_NAME})"

    return _format_unavailable_message("Speech-to-text", groq_error, "local-stt")


@mcp.tool()
async def list_voices() -> str:
    """List known Groq PlayAI voice names usable with text_to_speech's
    `voice` argument. Static list transcribed from Groq's public API docs,
    not fetched live - like the model names this server wires in, it can
    drift; check_provider_health confirms the default voice still works,
    not the full list."""
    return "\n".join(KNOWN_VOICES)


@mcp.tool()
async def check_provider_health() -> str:
    """Check whether Groq's hosted TTS/STT endpoints are currently reachable
    and whether each local fallback's optional dependency is installed -
    without running a full local model load (kokoro/faster-whisper can take
    real time and disk space on first use, which would defeat the point of
    a quick health check).
    """
    tts_result, stt_result = await asyncio.gather(_probe_groq_tts(), _probe_groq_stt())
    local_tts_ok, local_tts_detail = _probe_local_dependency("kokoro")
    local_stt_ok, local_stt_detail = _probe_local_dependency("faster_whisper")

    lines = ["voice-io provider health check:", ""]

    lines.append("text_to_speech:")
    ok, detail = tts_result
    lines.append(f"  {'OK ' if ok else 'FAIL'} {TTS_MODEL} - {detail}")
    lines.append(f"  {'OK ' if local_tts_ok else 'FAIL'} local:{_LOCAL_TTS_MODEL_NAME} - {local_tts_detail}")

    lines.append("speech_to_text:")
    ok, detail = stt_result
    lines.append(f"  {'OK ' if ok else 'FAIL'} {STT_MODEL} - {detail}")
    lines.append(f"  {'OK ' if local_stt_ok else 'FAIL'} local:{_LOCAL_STT_MODEL_NAME} - {local_stt_detail}")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
