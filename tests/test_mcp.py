"""silicorism_mcp: JSON-RPC dispatch, handshake, tools/list, tools/call."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import silicorism_mcp as mcp  # noqa: E402


def test_initialize_echoes_protocol_version():
    r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"}})
    assert r["id"] == 1
    assert r["result"]["protocolVersion"] == "2025-06-18"  # echoes client's
    assert "tools" in r["result"]["capabilities"]
    assert r["result"]["serverInfo"]["name"] == "silicorism"


def test_initialized_notification_is_silent():
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_exposes_four_canonical_tools():
    r = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in r["result"]["tools"]}
    assert names == {"silicorism_plan_and_submit", "silicorism_get_status",
                     "silicorism_start_workers", "silicorism_gc"}
    # every tool advertises an inputSchema (no handler leakage)
    for t in r["result"]["tools"]:
        assert set(t) == {"name", "description", "inputSchema"}


def test_tools_call_plan_and_get_status(tmp_path):
    dbp = str(tmp_path / "mcp.db")
    # plan_and_submit builds the 5-task DAG
    r = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "silicorism_plan_and_submit",
                               "arguments": {"prompt": "add auth", "db": dbp}}})
    assert r["result"]["isError"] is False
    payload = json.loads(r["result"]["content"][0]["text"])
    assert list(payload["tasks"]) == ["worktree", "scout", "builder", "fixer", "cleanup"]
    # get_status reflects the 5 queued tasks
    r2 = mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "silicorism_get_status",
                                "arguments": {"db": dbp}}})
    status = json.loads(r2["result"]["content"][0]["text"])
    assert status["tasks"]["pending"] == 5


def test_tools_call_missing_prompt_is_error(tmp_path):
    r = mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "silicorism_plan_and_submit",
                               "arguments": {"db": str(tmp_path / "x.db")}}})
    assert r["result"]["isError"] is True
    assert "prompt" in r["result"]["content"][0]["text"]


def test_unknown_tool_and_method():
    r = mcp.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                    "params": {"name": "nope", "arguments": {}}})
    assert r["error"]["code"] == -32602
    r2 = mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "bogus/method"})
    assert r2["error"]["code"] == -32601


def test_end_to_end_stdio_handshake(tmp_path):
    """Drive the real process over stdio with newline-delimited JSON-RPC."""
    msgs = "\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]) + "\n"
    out = subprocess.run([sys.executable, str(ROOT / "silicorism_mcp.py")],
                         input=msgs, capture_output=True, text=True, timeout=30)
    lines = [json.loads(l) for l in out.stdout.splitlines() if l.strip()]
    # initialize + tools/list responses; the notification produced none
    assert len(lines) == 2
    assert lines[0]["result"]["serverInfo"]["name"] == "silicorism"
    assert len(lines[1]["result"]["tools"]) == 4
