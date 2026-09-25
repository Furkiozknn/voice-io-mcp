# Changelog

All notable changes to this project are recorded here. The format follows
Keep a Changelog 1.1.0, and versions follow Semantic Versioning. The version
here must match `pyproject.toml` and `server.json`; the release workflow
refuses a tag that does not.

## [0.1.0] - 2026-09-25

First release.

### Tools

- `text_to_speech`: text (up to 4000 characters) to a `.wav` file. Tries
  Groq's Orpheus (`canopylabs/orpheus-v1-english`) first. Text longer than
  Orpheus's 200-character request limit is split at sentence ends, sent as
  several requests and joined. Falls back to local Kokoro-82M (the
  `local-tts` extra).
- `speech_to_text`: audio file to transcript. Tries Groq's
  `whisper-large-v3-turbo` first and falls back to local faster-whisper (the
  `local-stt` extra).
- `list_voices`: the Orpheus voice names.
- `check_provider_health`: sends a real request to each Groq endpoint, which
  spends a little free-tier quota, and reports whether each local extra is
  installed.

### Security

- `speech_to_text` uploads a local file to a third party, so the path is
  checked before anything is read. The extension must be an audio one, on
  the path as given and on the file a symlink resolves to, so
  `memo.wav -> ~/.env` is refused. The target must be a regular file (not a
  directory, FIFO or device) of at most 25 MB. Checks and read happen on one
  descriptor opened with `O_NOFOLLOW`, and both tiers get the same validated
  bytes. A file that grows while it is read is still refused.
- The Groq API key is scrubbed from every error message and log line.
- Only JSON-RPC is written to stdout. litellm's feedback banner is switched
  off, and each local-model call flushes stray `print()` output (Kokoro's
  phonemizer has one) to stderr while the transport still diverts it.

### Behaviour worth knowing

- A call that no tier could serve fails with `isError: true`. The message
  names both causes, and tells "the local extra is not installed" apart from
  "it is installed but failed" (for example, a blocked model download).
- Each hosted request has a 120 s timeout and one retry. Health probes have
  an 8 s timeout and never retry.
- Output is always `.wav`. `output_format="mp3"` is accepted, and the result
  says the file is `.wav`.
- Requires Python 3.11 or newer, because litellm cannot be imported on 3.10.

[0.1.0]: https://github.com/Furkiozknn/voice-io-mcp/releases/tag/v0.1.0
