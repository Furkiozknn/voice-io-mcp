"""The command line a human types, and the import cost an MCP client waits for."""
import subprocess
import sys
from pathlib import Path

import pytest

import voice_io

REPO = Path(__file__).resolve().parent.parent


def test_help_describes_the_flags_and_does_not_start_a_server(capsys):
    assert voice_io.main(["--help"]) == 0
    out = capsys.readouterr().out
    for word in ("--check", "--version", "GROQ_API_KEY", "claude mcp add"):
        assert word in out


def test_version_prints_the_package_version(capsys):
    assert voice_io.main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("voice-io-mcp 0.")


def test_unknown_argument_exits_2_and_points_at_help(capsys):
    assert voice_io.main(["--bogus"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--bogus" in captured.err and "--help" in captured.err


def test_check_without_any_provider_exits_1(monkeypatch, capsys):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(voice_io, "_probe_local_dependency", lambda name: (False, "not installed"))
    assert voice_io.main(["--check"]) == 1
    assert "text_to_speech:" in capsys.readouterr().out


def test_check_exits_0_when_both_tools_have_an_ok_line(monkeypatch, capsys):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(voice_io, "_probe_local_dependency", lambda name: (True, "installed"))
    assert voice_io.main(["--check"]) == 0
    assert "OK  local:" in capsys.readouterr().out


@pytest.mark.parametrize("report, expected", [
    ("h:\n\ntext_to_speech:\n  OK  a\nspeech_to_text:\n  FAIL b\n", False),
    ("h:\n\ntext_to_speech:\n  FAIL a\nspeech_to_text:\n  OK  b\n", False),
    ("h:\n\ntext_to_speech:\n  FAIL a\n  OK  a2\nspeech_to_text:\n  OK  b\n", True),
])
def test_check_ok_needs_an_ok_line_in_each_tool_section(report, expected):
    assert voice_io._check_ok(report) is expected


def test_importing_the_server_does_not_import_litellm():
    """litellm alone took 14 s to import on Windows; the client waits for
    `initialize` while the server imports, so it must load on first use."""
    done = subprocess.run(
        [sys.executable, "-c", "import sys, voice_io; print('litellm' in sys.modules)"],
        cwd=REPO, capture_output=True, text=True, timeout=120,
    )
    assert done.stdout.strip() == "False", done.stderr
