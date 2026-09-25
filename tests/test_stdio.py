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

# Stand-in for a noisy local model: prints through Python *and* writes to
# fd 1 directly (what a C extension would do), then reports failure.
_SERVER = """
import os, voice_io

def noisy_local_tts(text, filepath, voice="af_heart"):
    print("TODO:NUM stray print from a local model")
    os.write(1, b"raw fd-1 write from native code\\n")
    return False

voice_io._local_text_to_speech = noisy_local_tts
voice_io.main()
"""


def _session(requests, env_extra, timeout=60):
    env = {k: v for k, v in os.environ.items() if k != "GROQ_API_KEY"}
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
    lines, stderr = _session(
        _INIT + [{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "text_to_speech", "arguments": {"text": "Costs $5."}}}],
        {"VOICE_IO_OUTPUT_DIR": str(tmp_path)},
    )

    messages = [json.loads(line) for line in lines]  # raises on any non-JSON line
    assert [m.get("id") for m in messages] == [1, 2]
    assert "TODO:NUM stray print" in stderr
    assert "raw fd-1 write" in stderr

    result = messages[1]["result"]
    assert result["isError"] is True
    assert "Text-to-speech failed" in result["content"][0]["text"]
    assert "GROQ_API_KEY not set" in result["content"][0]["text"]


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
