"""The real server over real stdio, as an MCP client sees it.

Everything else in this suite calls the tool functions directly. These tests
start `voice_io.main()` in a subprocess and speak JSON-RPC to it, because two
promises only exist at that level:

- stdout carries nothing but protocol. The local fallbacks run third-party
  code that prints: misaki, Kokoro's English phonemizer, has a bare
  `print('❌', 'TODO:NUM', ...)` in its number handling. One stray line on
  stdout and the client drops the connection.
- a call that produced nothing is reported with isError=true, not as a
  normal-looking result.
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# The real _local_text_to_speech / _local_speech_to_text run, but against
# stand-in model packages that print through Python *and* write to fd 1
# directly (what a C extension would do), then produce nothing.
_SERVER = """
import os, sys, types

def noise(who):
    print(f"TODO:NUM stray print from {who}")
    os.write(1, f"raw fd-1 write from {who}\\n".encode())

class KPipeline:
    def __init__(self, lang_code):
        pass
    def __call__(self, text, **kwargs):
        noise("kokoro")
        return iter(())

class WhisperModel:
    def __init__(self, *args, **kwargs):
        pass
    def transcribe(self, audio):
        noise("faster-whisper")
        raise RuntimeError("no model here")

for name, attrs in {"numpy": {}, "soundfile": {}, "kokoro": {"KPipeline": KPipeline},
                    "faster_whisper": {"WhisperModel": WhisperModel}}.items():
    sys.modules[name] = types.SimpleNamespace(**attrs)

import voice_io
voice_io.main()
"""


def _session(requests, env_extra, timeout=60):
    # PYTHONUNBUFFERED would flush every print at once and hide the bug this
    # file exists for: MCP clients start servers with a buffered stdout.
    env = {k: v for k, v in os.environ.items() if k not in ("GROQ_API_KEY", "PYTHONUNBUFFERED")}
    env.update(env_extra)
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVER],
        cwd=REPO, env=env, text=True, encoding="utf-8",
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    lines, answered = [], threading.Event()
    wanted = {r["id"] for r in requests if "id" in r}

    def read_stdout():
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            try:
                seen = {json.loads(item).get("id") for item in lines}
            except ValueError:
                seen = set()
            if wanted <= seen:
                answered.set()

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    try:
        for request in requests:
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
        answered.wait(timeout)
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        reader.join(5)
        stderr = proc.stderr.read()
    return lines, stderr


_INIT = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
]


@pytest.mark.skipif(sys.platform == "win32", reason="fd-level stdout diversion is exercised on POSIX")
def test_stray_output_from_a_local_model_never_reaches_the_protocol_stream(tmp_path):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF....WAVE")
    lines, stderr = _session(
        _INIT + [
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "text_to_speech", "arguments": {"text": "Costs $5."}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "speech_to_text", "arguments": {"audio_path": str(audio)}}},
        ],
        {"VOICE_IO_OUTPUT_DIR": str(tmp_path)},
    )

    # Every line, including anything written at shutdown, must be JSON-RPC.
    messages = [json.loads(line) for line in lines]
    assert sorted(m.get("id") for m in messages) == [1, 2, 3]
    for who in ("kokoro", "faster-whisper"):
        assert f"TODO:NUM stray print from {who}" in stderr
        assert f"raw fd-1 write from {who}" in stderr

    by_id = {m["id"]: m["result"] for m in messages}
    assert by_id[2]["isError"] is True
    assert "Text-to-speech failed" in by_id[2]["content"][0]["text"]
    assert "GROQ_API_KEY not set" in by_id[2]["content"][0]["text"]
    assert "local fallback (kokoro-82m) failed too: Kokoro produced no audio" in by_id[2]["content"][0]["text"]
    assert by_id[3]["isError"] is True
    assert "Speech-to-text failed" in by_id[3]["content"][0]["text"]


def test_bad_input_arrives_as_the_designed_message_not_a_masked_crash(tmp_path):
    lines, _ = _session(
        _INIT + [{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "speech_to_text", "arguments": {"audio_path": "memo\u0000.wav"}}}],
        {"VOICE_IO_OUTPUT_DIR": str(tmp_path)},
    )

    result = json.loads(lines[-1])["result"]
    assert result["isError"] is True
    # A masked crash reads exactly "Error executing tool speech_to_text".
    assert result["content"][0]["text"].endswith(": Rejected: path contains a NUL byte")
