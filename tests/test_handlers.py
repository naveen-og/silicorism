"""Unit tests for the pi/claude CLI handlers — subprocess.run is mocked so we
assert command construction, cwd handling, and failure propagation only."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import handlers  # noqa: E402


def _proc(returncode=0, stdout="ok", stderr=""):
    m = MagicMock()
    m.returncode, m.stdout, m.stderr = returncode, stdout, stderr
    return m


def test_resolve_model_aliases():
    assert handlers.resolve_model("kimi-k2.5") == "bedrock-mantle/moonshotai.kimi-k2.5"
    assert handlers.resolve_model("deepseek-v4-flash") == "opencode/deepseek-v4-flash-free"
    # full opencode ids and unknown strings pass through untouched
    assert handlers.resolve_model("bedrock-mantle/zai.glm-5") == "bedrock-mantle/zai.glm-5"
    assert handlers.resolve_model("gpt-4o") == "gpt-4o"
    assert handlers.resolve_model(None) is None


@patch("handlers.subprocess.run")
def test_pi_full_payload(run):
    run.return_value = _proc(stdout="done")
    out = handlers.run("pi", json.dumps({
        "prompt": "hi", "model": "glm-5", "thinking": "max", "cwd": "/work"}))
    assert out == "done"
    # friendly name resolves to the opencode free id
    argv = run.call_args.args[0]
    assert argv[:6] == [
        "pi", "-p", "--model", "bedrock-mantle/zai.glm-5", "--thinking", "max"]
    # The prompt is last and starts with what was asked for; the default skill
    # text is appended to it (handlers.DEFAULT_SKILLS).
    assert argv[-1].startswith("hi")
    assert run.call_args.kwargs["cwd"] == "/work"


@patch("handlers.subprocess.run")
def test_pi_defaults(run):
    run.return_value = _proc()
    handlers.run("pi", json.dumps({"prompt": "go"}))
    argv = run.call_args.args[0]
    assert argv[:4] == ["pi", "-p", "--model", handlers.DEFAULT_PI_MODEL]
    assert argv[-1].startswith("go")
    assert run.call_args.kwargs["cwd"] is None


@patch("handlers.subprocess.run")
def test_pi_fallback_string(run):
    run.return_value = _proc()
    handlers.run("pi", "just a prompt")
    argv = run.call_args.args[0]
    assert argv[:4] == ["pi", "-p", "--model", handlers.DEFAULT_PI_MODEL]
    assert argv[-1].startswith("just a prompt")


@patch("handlers.subprocess.run")
def test_pi_failure_propagates(run):
    run.return_value = _proc(returncode=2, stderr="model unavailable")
    try:
        handlers.run("pi", json.dumps({"prompt": "x"}))
    except RuntimeError as e:
        assert "model unavailable" in str(e)
    else:
        raise AssertionError("expected RuntimeError on non-zero exit")


@patch("handlers.subprocess.run")
def test_claude_payload(run):
    run.return_value = _proc(stdout="summary")
    out = handlers.run("claude", json.dumps({"prompt": "review", "cwd": "/repo"}))
    assert out == "summary"
    argv = run.call_args.args[0]
    assert argv[:2] == ["claude", "-p"]
    assert argv[-1].startswith("review")
    assert run.call_args.kwargs["cwd"] == "/repo"


@patch("handlers.subprocess.run")
def test_claude_failure_propagates(run):
    run.return_value = _proc(returncode=1, stderr="boom")
    try:
        handlers.run("claude", json.dumps({"prompt": "x"}))
    except RuntimeError as e:
        assert "boom" in str(e)
    else:
        raise AssertionError("expected RuntimeError on non-zero exit")


def test_missing_prompt_raises():
    for tt in ("pi", "claude"):
        try:
            handlers.run(tt, json.dumps({"model": "x"}))
        except ValueError:
            pass
        else:
            raise AssertionError(f"{tt} should require prompt")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("test_handlers OK")
