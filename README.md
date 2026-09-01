<div align="center">

# voice-io-mcp

[![License: MIT](https://img.shields.io/badge/license-MIT-76b900?style=flat-square)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-76b900?style=flat-square)](pyproject.toml)
[![MCP Server](https://img.shields.io/badge/MCP-server-76b900?style=flat-square)](https://modelcontextprotocol.io)
[![Cost](https://img.shields.io/badge/cost-%240-76b900?style=flat-square)](#-setup)

Text-to-speech and speech-to-text as two small MCP tools — Groq's free hosted endpoints first, a fully local, keyless model if Groq isn't reachable or configured at all.

</div>

Every other tool in this ecosystem's [nvidia-nim-mcp](https://github.com/Furkiozknn/nvidia-nim-mcp) wraps image/text/vision/safety/embedding models behind a "try a real provider, fall back if it fails" contract. Audio was the one capability nothing covered — this fills that gap, same philosophy: **a model being slow, rate-limited, or unconfigured should never take a tool down.**

## 📖 Table of Contents

- [Tools](#-tools)
- [The fallback chain](#-the-fallback-chain)
- [Setup](#-setup)
- [Example usage](#-example-usage)
- [Architecture](#-architecture)
- [Development](#-development)
- [Known limitations](#-known-limitations--roadmap)
- [License](#-license)

## 🧰 Tools

| Tool | What it does | Hosted tier (Groq, free) | Local fallback |
|---|---|---|---|
| 🔊 `text_to_speech` | Text → audio file, saved to `output/` | `playai-tts` | Kokoro-82M (Apache-2.0) |
| 🎙️ `speech_to_text` | Audio file → transcript (rejects non-audio extensions and files over 25MB before ever reading them) | `whisper-large-v3-turbo` | faster-whisper (MIT) |
| 🗣️ `list_voices` | List known Groq PlayAI voice names for `text_to_speech`'s `voice` argument | — (static list) | — |
| 🩺 `check_provider_health` | Liveness probe for both hosted endpoints + local-dependency availability check | both | both |

## 🔄 The fallback chain

```
text_to_speech(text)
  ├─ 1. Groq playai-tts        (needs GROQ_API_KEY — free, no credit card)
  └─ 2. Kokoro-82M, local      (needs `uv sync --extra local-tts` — no key, no network)

speech_to_text(audio_path)
  ├─ 1. Groq whisper-large-v3-turbo   (needs GROQ_API_KEY)
  └─ 2. faster-whisper, local          (needs `uv sync --extra local-stt`)
```

Both tools try Groq first *only if* `GROQ_API_KEY` is set in `.env` — if it isn't, or if the Groq call fails for any reason, they drop straight to the local model. **This is the one meaningful difference from nvidia-nim-mcp's own pattern: every tool here works with zero API keys configured at all**, as long as the relevant optional extra is installed — a hosted key is a speed/quality upgrade, not a hard requirement.

The local tiers are genuinely last-resort: Kokoro always writes a `.wav` file regardless of the requested `output_format` (its native output; encoding straight to mp3 depends on the local `libsndfile` build, which isn't guaranteed cross-platform), and the tool's return message says so explicitly rather than silently substituting formats.

**On model names:** `playai-tts` and `whisper-large-v3-turbo` follow Groq's public API documentation, but neither was live-verified with a real key while building this repo (no key was available in the build environment). Run `check_provider_health` once `GROQ_API_KEY` is set to confirm they're still current — Groq's free-tier model lineup shifts over time, the same "don't trust a name from memory" discipline `nvidia-nim-mcp` documents for its own model list.

## ⚙️ Setup

**1. Install the base package** (this project uses [`uv`](https://docs.astral.sh/uv/), not bare pip/venv):

```bash
uv sync
```

**2. (Optional) Enable Groq's hosted tier.** Create a `.env` file in the project root:

```bash
GROQ_API_KEY=your-key-here
```

Get one free at [console.groq.com/keys](https://console.groq.com/keys) — no credit card. Without it, both tools go straight to their local fallback.

**3. (Optional) Enable the local fallbacks** — each is an independent extra, install either or both:

```bash
uv sync --extra local-tts   # Kokoro-82M — also needs the `espeak-ng` system package
uv sync --extra local-stt   # faster-whisper
```

`espeak-ng` is used by Kokoro's phonemizer for out-of-distribution English and non-English text; straightforward English text works without it, but full quality/robustness wants it on `PATH` (`apt install espeak-ng` / `choco install espeak-ng` / `brew install espeak-ng`).

**4. Register it as an MCP server** with Claude Code (project or user scope):

```bash
claude mcp add --transport stdio voice-io -- uv run --project /path/to/this/repo voice_io.py
```

**5. Run `check_provider_health` once, after setting `GROQ_API_KEY`.** The model/voice names this server wires in (`playai-tts`, `whisper-large-v3-turbo`) were transcribed from Groq's public docs but never live-verified with a real key while building this — confirm they're still current before relying on the hosted tier, the same "don't trust a name from memory" discipline `nvidia-nim-mcp` documents for its own model list. If a name has drifted, the local fallback still works regardless (once its extra is installed).

## ▶️ Example usage

```
"Read this changelog entry out loud"
→ text_to_speech  → saved to output/speech_20260901_120000.mp3 (model: groq/playai-tts)

"Transcribe this voice memo at C:\Users\me\Desktop\note.wav"
→ speech_to_text  → returns the transcript (model: groq/whisper-large-v3-turbo)

"What voices can I use for text_to_speech?"
→ list_voices     → returns the known Groq PlayAI voice names, one per line

"Is voice-io's Groq connection actually working right now?"
→ check_provider_health → per-endpoint OK/FAIL report, plus whether the local
                           extras are installed
```

## 🏗 Architecture

Single-file MCP server (`voice_io.py`), same shape as `nvidia-nim-mcp`'s `nvidia_image.py` and `mini-creative-toolkit`'s `toolkit.py` — one module, `@mcp.tool()`-decorated functions, no framework beyond the `mcp` package itself.

- **Hosted calls** go through [`litellm`](https://github.com/BerriAI/litellm) (`aspeech` / `atranscription`), the same library `nvidia-nim-mcp` and `model-comparison-harness` already use for their own multi-provider chat chains — one dependency covering chat, TTS, and STT uniformly across providers, rather than hand-rolling Groq's HTTP shape directly.
- **Local fallbacks** are lazy-loaded singletons (loaded once, on first real use, not at import time) guarded by a `threading.Lock` — a lesson carried over from a real bug caught in `nvidia-nim-mcp`'s own local-embedding fallback: without the lock, two concurrent calls could both start loading the same large model at once.
- **The STT health probe** builds a valid ~0.1s silent WAV in-memory using only Python's stdlib `wave` module — no binary audio fixture shipped in the repo, no extra dependency just to construct a liveness-check payload.
- **`speech_to_text` validates before it reads.** It reads whatever local path it's given and uploads the bytes to Groq (a third party) — an extension allow-list and a 25MB size cap run *before* the file is opened, so a wrong or maliciously-crafted path (e.g. an agent instructed to "transcribe the audio at `.env`") is rejected locally instead of silently uploaded. Any captured Groq error text also has the API key scrubbed out before it's returned or logged, as defense-in-depth against an underlying HTTP client embedding it in an exception message.

## 🛠 Development

```bash
uv sync --group dev
uv run pytest
```

The suite (`tests/`) mocks every `litellm` call — no `GROQ_API_KEY` or real network access needed. It also exercises the *real*, unmocked local-fallback code paths against this repo's base test environment (where `kokoro`/`faster-whisper` are deliberately not installed, being optional extras), confirming both fallbacks fail closed — returning `False`/`None`, never raising — when their dependency is absent. CI (`.github/workflows/ci.yml`) runs the same command on every push/PR.

## 🚧 Known limitations / roadmap

- **Voice cloning is deliberately out of scope for v1.** Kokoro's own upstream ecosystem and other open models (e.g. Chatterbox) support zero-shot voice cloning from a few seconds of reference audio — genuinely useful, but also the most misuse-prone capability in this space. If it's added later, it should ship with a mandatory consent-confirmation step and audio watermarking (Chatterbox bundles [Perth](https://github.com/resemble-ai/chatterbox), a watermarker, for exactly this reason) — not as an afterthought.
- **Groq's Gemini-Flash TTS tier was researched but not wired in.** Its free tier exists but is restricted to non-commercial/personal use per Google's terms, and its request/response shape wasn't verified during this build — a clean second hosted fallback tier to add later once both are confirmed.
- **No streaming.** Both tools return a complete file/transcript, not a chunked stream — fine for short clips and voice memos, a real limitation for long-form audio.
- **Kokoro's local fallback always emits `.wav`, ignoring `output_format`** (see [The fallback chain](#-the-fallback-chain)) — a deliberate cross-platform-safety tradeoff, not an oversight.

## 📄 License

MIT — see [LICENSE](LICENSE). Kokoro-82M's weights are Apache-2.0; faster-whisper is MIT. Neither is vendored in this repo — both are optional dependencies, fetched from their own sources on install/first-use.
