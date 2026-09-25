"""voice-io-mcp: text-to-speech and speech-to-text as MCP tools.

Groq's OpenAI-compatible audio endpoints first (Orpheus / whisper-large-
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
import stat
import threading
import uuid
import wave
from datetime import datetime
from pathlib import Path
from typing import Literal

import litellm
from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

load_dotenv()

# This server speaks JSON-RPC over stdout. On every failed provider call
# litellm otherwise print()s a "Give Feedback / Get Help" banner to stdout,
# i.e. into the protocol stream, exactly when a fallback is about to run.
litellm.suppress_debug_info = True

logger = logging.getLogger(__name__)

mcp = MCPServer("voice-io")

# Where generated audio lands. Deliberately NOT `Path(__file__).parent`:
# for anyone who `pip install`s this server that directory is inside
# site-packages, which is read-only on many installs and pollutes the
# environment on the rest. Default to a directory under the process's
# current working directory instead, overridable with VOICE_IO_OUTPUT_DIR.
OUTPUT_DIR = Path(os.environ.get("VOICE_IO_OUTPUT_DIR") or Path.cwd() / "output")

GROQ_API_KEY_ENV = "GROQ_API_KEY"

# Groq retired playai-tts (deprecation announced 2025-12-23) in favour of
# Canopy Labs' Orpheus. Orpheus answers in WAV only and takes at most 200
# characters per request, so longer text is split and the parts joined.
TTS_MODEL = "groq/canopylabs/orpheus-v1-english"
DEFAULT_VOICE = "hannah"
TTS_MAX_CHARS = 200
STT_MODEL = "groq/whisper-large-v3-turbo"

# Orpheus English voice names, transcribed from Groq's public documentation
# - like the model names above, not live-verified with a real key while
# building this. Run check_provider_health (or list_voices, which just
# returns this list) and expect it to drift over time.
KNOWN_VOICES = ["autumn", "diana", "hannah", "austin", "daniel", "troy"]

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


# Upper bound on text_to_speech input. Orpheus takes 200 characters per
# request, so this is at most 20 hosted requests for one call - past that a
# single prompt could burn a free tier's whole per-minute allowance (or run
# for many minutes locally) without anyone having asked for an audiobook.
MAX_TTS_TEXT_CHARS = 4000

# Passed to the real hosted TTS/STT calls. litellm's default is 600 seconds;
# a wedged provider would otherwise hold the tool for ten minutes before the
# local fallback even got a chance. Two minutes comfortably covers a 25MB
# upload plus transcription on the free tier.
HOSTED_CALL_TIMEOUT = 120.0

# The OpenAI client under litellm retries twice by default, and the timeout
# above applies per attempt - three attempts could hold a call for six
# minutes. One retry absorbs a transient blip; after that the local
# fallback is the better use of the caller's time. Health probes never
# retry: their job is to report, not to recover.
HOSTED_MAX_RETRIES = 1

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


def _local_speech_to_text(audio: str | io.BytesIO) -> str | None:
    """Fully local, keyless STT fallback via faster-whisper. Weights
    auto-download from Hugging Face Hub on first use - only used if Groq's
    hosted endpoint fails or GROQ_API_KEY isn't set. Returns None (never
    raises) if the optional dependency is missing or transcription fails.

    `audio` may be a path or an in-memory file; speech_to_text passes the
    bytes it already validated so the file is never reopened by path."""
    global _local_stt_model
    try:
        if _local_stt_model is None:
            with _local_stt_lock:
                if _local_stt_model is None:
                    from faster_whisper import WhisperModel

                    _local_stt_model = WhisperModel(_LOCAL_STT_MODEL_SIZE, device="cpu", compute_type="int8")
        segments, _info = _local_stt_model.transcribe(audio)
        return " ".join(segment.text.strip() for segment in segments)
    except Exception as e:
        logger.warning("local STT fallback unavailable: %s", e)
        return None


def _stamp() -> str:
    """Unique, sortable stem for output filenames. Plain second precision
    let two rapid tool calls collide and silently overwrite each other's
    audio; microseconds alone are not enough either, because the wall clock
    on Windows advances in steps far coarser than a microsecond. The random
    suffix makes a collision practically impossible on every platform."""
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:6]}"


def _check_audio_suffix(name: str, shown: str) -> None:
    suffix = Path(name).suffix
    if suffix.lower() not in ALLOWED_AUDIO_EXTENSIONS:
        raise ToolError(
            f"Rejected: {shown} has {suffix or '(no extension)'}, which is not a recognized audio "
            f"format (expected one of {sorted(ALLOWED_AUDIO_EXTENSIONS)})"
        )


def _read_audio_file(audio_path: str) -> tuple[bytes, str]:
    """Validate and read a caller-supplied audio path in one pass, returning
    (bytes, filename). Raises ToolError for anything that must not be read.

    The checks and the read act on the same open file descriptor, not on
    the path twice, so nothing can be swapped in between:
    - the extension is checked on the path as given AND on the file it
      finally resolves to, so `memo.wav -> ~/.env` (a symlink with an audio
      name) is refused instead of uploading the secret it points at;
    - the resolved path is opened with O_NOFOLLOW and O_NONBLOCK where the
      platform has them, so a last-moment symlink swap fails and a FIFO
      cannot hang the call;
    - fstat on that descriptor must say "regular file" (no directory, FIFO,
      socket or device) and within the size cap;
    - at most cap+1 bytes are read, so a file that grows after the fstat is
      still refused rather than uploaded past the limit.
    """
    # A NUL byte makes every os call raise ValueError, which the SDK would
    # mask as a bare "Error executing tool" - say what is wrong instead.
    if "\x00" in audio_path:
        raise ToolError("Rejected: path contains a NUL byte")
    path = Path(audio_path).expanduser()
    _check_audio_suffix(path.name, "path")
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ToolError(f"File not found: {audio_path}") from None
    if real.name != path.name:
        _check_audio_suffix(real.name, f"link target {real.name!r}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(real, flags)
    except OSError as e:
        raise ToolError(f"Cannot open {audio_path}: {e.strerror or e}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ToolError(f"Rejected: {audio_path} is not a regular file")
        if st.st_size > MAX_AUDIO_BYTES:
            raise ToolError(
                f"Rejected: file is {st.st_size / (1024 * 1024):.1f}MB, exceeds the "
                f"{MAX_AUDIO_BYTES // (1024 * 1024)}MB limit"
            )
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as f:
        data = f.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise ToolError(f"Rejected: file grew past the {MAX_AUDIO_BYTES // (1024 * 1024)}MB limit while being read")
    return data, path.name


def _named_buffer(data: bytes, name: str) -> io.BytesIO:
    """In-memory file with a `.name` (upload APIs use it to guess the
    content type)."""
    buf = io.BytesIO(data)
    buf.name = name
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


def _unavailable(action: str, hosted_error: str | None, local_extra: str) -> ToolError:
    """The identical 'both tiers failed' error both tools raise - factored
    out once so text_to_speech and speech_to_text can't drift apart in
    wording as this contract evolves.

    Raised, not returned: a call that produced no audio / no transcript is
    a failed tool call, and MCP reports that with isError=true (the spec
    lists API failures there explicitly). Returned as a plain string it
    looked like a success to every client that checks the flag."""
    detail = hosted_error or f"{GROQ_API_KEY_ENV} not set (environment or .env)"
    return ToolError(
        f"{action} failed (Groq: {detail}) and no local fallback available "
        f"(install the `{local_extra}` extra to enable one)."
    )


def _tts_chunks(text: str, limit: int = TTS_MAX_CHARS) -> list[str]:
    """Split text into pieces of at most `limit` characters, preferring
    sentence ends, then spaces - a word is only cut when it alone is longer
    than the limit."""
    text = " ".join(text.split())
    chunks = []
    while len(text) > limit:
        window = text[: limit + 1]
        cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
        if cut > 0:
            cut += 1  # keep the punctuation with its sentence
        else:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def _join_wavs(parts: list[Path], target: Path) -> None:
    """Concatenate WAV files that share one format into `target`. Raises if
    the formats differ - splicing mismatched PCM would produce noise."""
    params = None
    frames = []
    for part in parts:
        with wave.open(str(part), "rb") as w:
            p = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            if params is None:
                params = p
            elif p != params:
                raise ValueError(f"WAV parts disagree on format: {params} vs {p}")
            frames.append(w.readframes(w.getnframes()))
    with wave.open(str(target), "wb") as out:
        out.setnchannels(params[0])
        out.setsampwidth(params[1])
        out.setframerate(params[2])
        for f in frames:
            out.writeframes(f)


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
            model=TTS_MODEL, voice=DEFAULT_VOICE, input="hi", response_format="wav", api_key=key,
            timeout=HEALTH_PROBE_TIMEOUT, max_retries=0,
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
            model=STT_MODEL, file=_tiny_silent_wav(), api_key=key, timeout=HEALTH_PROBE_TIMEOUT, max_retries=0
        )
    except Exception as e:
        return False, f"error: {_redact(str(e), key)}"
    return True, "ok"


@mcp.tool()
async def text_to_speech(
    text: str, voice: str = DEFAULT_VOICE, output_format: Literal["wav", "mp3"] = "wav"
) -> str:
    """Convert text to speech, saved as a .wav file in the server's output
    directory (VOICE_IO_OUTPUT_DIR, default ./output); returns the path.

    Tries Groq's Orpheus first (free tier, no credit card - requires
    GROQ_API_KEY), falling back to a fully local, keyless model (Kokoro-82M,
    Apache-2.0) if Groq is unavailable or the key isn't set. Text longer
    than Orpheus's 200-character request limit is sent in pieces and joined.
    The local fallback requires the optional `local-tts` extra
    (`uv sync --extra local-tts`) plus the `espeak-ng` system package for
    full quality on non-trivial or non-English text.

    Args:
        text: Text to speak, at most 4000 characters.
        voice: Orpheus voice name (e.g. "hannah", see list_voices) - ignored
            by the local fallback, which always uses Kokoro's "af_heart" voice.
        output_format: "wav" (the default). "mp3" is still accepted for
            compatibility, but both tiers now write .wav and the result says so.
    """
    # ToolError, not ValueError: under mcp >= 2.1 a plain exception is
    # treated as a crash and masked to a generic "Error executing tool ..."
    # (verified against the installed SDK) - these two messages are designed
    # for the caller and must arrive intact.
    if output_format not in ("mp3", "wav"):
        raise ToolError(f"output_format must be 'mp3' or 'wav', got {output_format!r}")
    if not text.strip():
        raise ToolError("text must not be empty")
    if len(text) > MAX_TTS_TEXT_CHARS:
        raise ToolError(
            f"text is {len(text)} characters, over the {MAX_TTS_TEXT_CHARS}-character limit "
            f"- split it into several calls"
        )

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Otherwise the SDK masks this as a bare "Error executing tool".
        raise ToolError(
            f"Cannot create output directory {OUTPUT_DIR}: {e.strerror or e} "
            f"(set VOICE_IO_OUTPUT_DIR to a writable directory)"
        ) from None
    stamp = _stamp()

    # Orpheus and Kokoro both produce WAV; nothing here encodes mp3.
    note = " (requested format ignored - both tiers write .wav)" if output_format != "wav" else ""

    key = os.environ.get(GROQ_API_KEY_ENV)
    groq_error = None
    if key:
        hosted_path = OUTPUT_DIR / f"speech_{stamp}.wav"
        chunks = _tts_chunks(text)
        parts = [hosted_path] if len(chunks) == 1 else [
            OUTPUT_DIR / f"speech_{stamp}.part{i}.wav" for i in range(len(chunks))
        ]
        try:
            for chunk, part in zip(chunks, parts):
                response = await litellm.aspeech(
                    model=TTS_MODEL, voice=voice, input=chunk, response_format="wav", api_key=key,
                    timeout=HOSTED_CALL_TIMEOUT, max_retries=HOSTED_MAX_RETRIES,
                )
                # stream_to_file is a blocking disk write - run it off the event
                # loop like every other I/O call here, not inline in async def.
                await asyncio.to_thread(response.stream_to_file, str(part))
            if len(parts) > 1:
                await asyncio.to_thread(_join_wavs, parts, hosted_path)
            pieces = f", {len(chunks)} requests" if len(chunks) > 1 else ""
            return f"Audio saved to {hosted_path} (model: {TTS_MODEL}{pieces}){note}"
        except Exception as e:
            groq_error = _redact(str(e), key)
            # A partial/corrupt file can exist if the write started before
            # failing - never leave that behind silently.
            hosted_path.unlink(missing_ok=True)
            logger.warning("Groq TTS failed, falling back to local: %s", groq_error)
        finally:
            if len(parts) > 1:
                for part in parts:
                    part.unlink(missing_ok=True)

    local_path = OUTPUT_DIR / f"speech_{stamp}.wav"
    ok = await asyncio.to_thread(_local_text_to_speech, text, local_path)
    if ok:
        return f"Audio saved to {local_path} (model: local:{_LOCAL_TTS_MODEL_NAME}){note}"

    raise _unavailable("Text-to-speech", groq_error, "local-tts")


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
            ogg/webm/mp4/mpeg/mpga, any case), max 25MB. A symlink is
            followed only if its target also has an audio extension;
            directories, FIFOs and devices are refused.
        language: Optional ISO-639-1 language hint (e.g. "en"). Ignored by
            the local fallback, which auto-detects language.

    Raises:
        ToolError: if the path does not exist, is not a regular file with
            a recognized audio extension, or exceeds the 25MB upload limit
            - the same contract text_to_speech uses for its own invalid
            arguments - and also when neither tier could transcribe it.
    """
    # ToolError for bad input and for "no tier could do it" alike, exactly
    # as text_to_speech does: the two tools must not disagree about how a
    # failed call is reported, and both cases are isError=true in MCP.
    # This tool reads whatever local file it's pointed at and uploads its
    # bytes to Groq (a third party) - the allow-list, file-type and size
    # checks in _read_audio_file stop it from being turned into a generic
    # "read and exfiltrate an arbitrary file" primitive by a wrong or
    # maliciously-crafted path. The bytes read there are the only ones
    # either tier ever sees; the path is not opened a second time.
    # (Blocking disk read - off the event loop, same rule as the TTS write.)
    data, name = await asyncio.to_thread(_read_audio_file, audio_path)

    key = os.environ.get(GROQ_API_KEY_ENV)
    groq_error = None
    if key:
        try:
            response = await litellm.atranscription(
                model=STT_MODEL, file=_named_buffer(data, name), language=language, api_key=key,
                timeout=HOSTED_CALL_TIMEOUT, max_retries=HOSTED_MAX_RETRIES,
            )
            return f"{response.text}\n\n(model: {STT_MODEL})"
        except Exception as e:
            groq_error = _redact(str(e), key)
            logger.warning("Groq STT failed, falling back to local: %s", groq_error)

    text = await asyncio.to_thread(_local_speech_to_text, _named_buffer(data, name))
    if text is not None:
        return f"{text}\n\n(model: local:{_LOCAL_STT_MODEL_NAME})"

    raise _unavailable("Speech-to-text", groq_error, "local-stt")


@mcp.tool()
async def list_voices() -> str:
    """List known Groq Orpheus voice names usable with text_to_speech's
    `voice` argument. Static list transcribed from Groq's public API docs,
    not fetched live - like the model names this server wires in, it can
    drift; check_provider_health confirms the default voice still works,
    not the full list."""
    return "\n".join(KNOWN_VOICES)


@mcp.tool()
async def check_provider_health() -> str:
    """Check whether Groq's hosted TTS/STT endpoints answer and whether each
    local fallback's optional dependency is installed - without loading a
    local model (kokoro/faster-whisper can take real time and disk space on
    first use).

    Costs free-tier quota: with GROQ_API_KEY set, every call sends one real
    TTS request ("hi") and one transcription of 0.1s of silence (Groq bills
    a transcription as at least 10 seconds). Run it when something looks
    wrong, not before every other call.
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


def main() -> None:
    """Console entry point.

    A separate function because `[project.scripts]` wants a CALLABLE, not a
    module. Without it the package installs but cannot be run: the user
    would have to clone the repo and point at the file directly, which
    defeats the point of publishing it.
    """
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
