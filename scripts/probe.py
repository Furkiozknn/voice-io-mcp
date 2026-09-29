#!/usr/bin/env python3
"""Talk to voice-io-mcp the way an MCP client does: over stdio, JSON-RPC.

    python scripts/probe.py                                  # initialize + tools/list
    python scripts/probe.py --call speech_to_text '{"audio_path": ".env"}'
    python scripts/probe.py --server "uvx --from git+... voice-io-mcp"

Prints what the server answered, nothing invented: the time until it answered
`initialize`, the tools with the first sentence of each description, and for
each --call the isError flag and text. Exit 0 when every request was answered.
This is the source of the demo in docs/demo (scripts/demo-uret.py).
"""
import argparse
import json
import shlex
import subprocess
import sys
import time

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "probe", "version": "0"}}}


def first_sentence(text: str) -> str:
    flat = " ".join((text or "").split())
    line = flat.split(". ")[0].rstrip(".")
    return line if len(line) <= 88 else line[:85].rsplit(" ", 1)[0] + " ..."


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--server", default="voice-io-mcp", help="command that starts the server (default: voice-io-mcp)")
    ap.add_argument("--call", nargs=2, action="append", default=[], metavar=("TOOL", "JSON"),
                    help="call a tool after listing; repeatable")
    ap.add_argument("--timeout", type=float, default=120)
    args = ap.parse_args()

    requests = [{"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    for i, (name, raw) in enumerate(args.call, start=3):
        requests.append({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                         "params": {"name": name, "arguments": json.loads(raw)}})

    started = time.monotonic()
    proc = subprocess.Popen(shlex.split(args.server), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
    proc.stdin.write(json.dumps(INIT) + "\n")
    proc.stdin.flush()
    answers = {}

    def read(want):
        while want not in answers:
            line = proc.stdout.readline()
            if not line:
                raise SystemExit("server closed stdout before answering")
            msg = json.loads(line)  # a non-JSON line here means stdout is polluted: fail loudly
            if "id" in msg:
                answers[msg["id"]] = msg
            if time.monotonic() - started > args.timeout:
                raise SystemExit("timed out")

    read(1)
    took = time.monotonic() - started
    info = answers[1]["result"]["serverInfo"]
    print(f"initialize  {info['name']}  answered after {took:.1f} s")
    for r in requests:
        proc.stdin.write(json.dumps(r) + "\n")
    proc.stdin.flush()
    read(2)
    tools = answers[2]["result"]["tools"]
    print(f"tools/list  {len(tools)} tools")
    for t in tools:
        print(f"  {t['name']:<22}{first_sentence(t.get('description', ''))}")
    for i, (name, raw) in enumerate(args.call, start=3):
        read(i)
        res = answers[i]["result"]
        text = " ".join(c.get("text", "") for c in res.get("content", []))
        print(f"\ntools/call {name} {raw}\n  isError={str(res.get('isError', False)).lower()}\n  {text}")
    proc.stdin.close()
    proc.wait(timeout=20)
    return 0


if __name__ == "__main__":
    sys.exit(main())
