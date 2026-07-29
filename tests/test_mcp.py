"""silicorism_mcp: JSON-RPC dispatch, handshake, tools/list, tools/call."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import silicorism_mcp as mcp  # noqa: E402
import db  # noqa: E402
import silicorism_tools  # noqa: E402


def test_initialize_echoes_protocol_version():
    r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"}})
    assert r["id"] == 1
    assert r["result"]["protocolVersion"] == "2025-06-18"  # echoes client's
    assert "tools" in r["result"]["capabilities"]
    assert r["result"]["serverInfo"]["name"] == "silicorism"


def test_initialized_notification_is_silent():
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_initialize_carries_orchestration_instructions():
    r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    instr = r["result"]["instructions"]
    assert "ZERO ASSUMPTIONS" in instr and "silicorism_list_skills" in instr


def test_tools_list_exposes_canonical_tools():
    r = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in r["result"]["tools"]}
    assert names == {"silicorism_plan_and_submit", "silicorism_get_status",
                     "silicorism_start_workers", "silicorism_gc",
                     "silicorism_verify_and_continue", "silicorism_list_skills",
                     "silicorism_wait", "silicorism_cancel_task"}
    # every tool advertises an inputSchema (no handler leakage)
    for t in r["result"]["tools"]:
        assert set(t) == {"name", "description", "inputSchema"}


def test_tools_call_plan_and_get_status(tmp_path):
    dbp = str(tmp_path / "mcp.db")
    # plan_and_submit (pipeline fallback); workers=0 so no real processes spawn
    r = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "silicorism_plan_and_submit",
                               "arguments": {"prompt": "add auth", "db": dbp,
                                             "workers": 0}}})
    assert r["result"]["isError"] is False
    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload["mode"] == "pipeline"
    assert list(payload["tasks"]) == ["worktree", "scout", "builder", "fixer",
                                      "verify", "cleanup"]
    # get_status reflects the 6 queued tasks + a not-yet-satisfied verdict
    r2 = mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "silicorism_get_status",
                                "arguments": {"db": dbp}}})
    status = json.loads(r2["result"]["content"][0]["text"])
    assert status["tasks"]["pending"] == 6
    assert status["satisfied"] is False


def test_tools_call_dynamic_dag(tmp_path):
    dbp = str(tmp_path / "dag.db")
    nodes = [
        {"id": "a", "prompt": "recon", "harness": "pi",
         "model": "opencode/deepseek-v4-flash-free", "thinking": "high"},
        {"id": "b", "prompt": "build", "depends_on": ["a"], "harness": "pi",
         "model": "opencode/nemotron-3-ultra-free", "thinking": "high"},
        {"id": "c", "prompt": "review", "depends_on": ["b"], "harness": "claude"},
    ]
    r = mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "silicorism_plan_and_submit",
                               "arguments": {"nodes": nodes, "db": dbp, "workers": 0}}})
    assert r["result"]["isError"] is False
    out = json.loads(r["result"]["content"][0]["text"])
    assert out["mode"] == "dag"
    assert set(out["nodes"]) == {"a", "b", "c"}


def test_verify_and_continue_verdict(tmp_path):
    dbp = str(tmp_path / "v.db")
    r = mcp.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                    "params": {"name": "silicorism_verify_and_continue",
                               "arguments": {"db": dbp}}})
    verdict = json.loads(r["result"]["content"][0]["text"])
    # empty queue: nothing completed yet -> not satisfied, no failures
    assert verdict["satisfied"] is False
    assert verdict["failures"] == []


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
    assert len(lines[1]["result"]["tools"]) == 8


def test_list_skills_tool(tmp_path):
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "review.md").write_text("REVIEW RULES")
    r = mcp.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                    "params": {"name": "silicorism_list_skills",
                               "arguments": {"cwd": str(tmp_path)}}})
    inv = json.loads(r["result"]["content"][0]["text"])
    assert any(s["name"] == "review" and s["harness"] == "claude" for s in inv)


def test_tools_list_includes_wait_and_complexity():
    r = mcp.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}})
    names = {t["name"] for t in r["result"]["tools"]}
    assert "silicorism_wait" in names
    submit = next(t for t in r["result"]["tools"]
                  if t["name"] == "silicorism_plan_and_submit")
    assert "complexity" in submit["inputSchema"]["properties"]


def test_simple_complexity_submits_one_agent(tmp_path):
    dbp = str(tmp_path / "simple.db")
    r = mcp.handle({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                    "params": {"name": "silicorism_plan_and_submit",
                               "arguments": {"prompt": "build a python game",
                                             "complexity": "simple",
                                             "cwd": str(tmp_path),
                                             "db": dbp, "workers": 0}}})
    payload = json.loads(r["result"]["content"][0]["text"])
    assert list(payload["tasks"]) == ["solo"]
    assert payload["worktree_path"] == str(tmp_path)  # cwd reached the builder


def test_instructions_forbid_polling_and_claude_execution():
    assert "DO NOT POLL" in mcp.INSTRUCTIONS
    assert "Never assign a Claude model" in mcp.INSTRUCTIONS

def test_gc_prunes_terminal_tasks_but_keeps_live_ones(tmp_path):
    """A dead pipeline must be clearable without shelling into sqlite."""
    dbp = str(tmp_path / "prune.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    done = db.add_task(conn, "pi", '{"prompt": "a"}')
    conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (done,))
    conn.commit()
    live = db.add_task(conn, "pi", '{"prompt": "b"}')
    assert silicorism_tools.prune_tasks(conn) == {"deleted": 1}
    left = [r["id"] for r in conn.execute("SELECT id FROM tasks")] 
    assert left == [live]
    conn.close()

def test_prune_keeps_a_completed_task_that_a_pending_task_still_depends_on(tmp_path):
    """Deleting a completed dependency parks its dependent forever (db.py:236)."""
    dbp = str(tmp_path / "deadlock.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    parent = db.add_task(conn, "pi", '{"prompt": "a"}')
    conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (parent,))
    conn.commit()
    child = db.add_task(conn, "pi", '{"prompt": "b"}', depends_on=[parent])
    assert silicorism_tools.prune_tasks(conn) == {"deleted": 0}
    assert {r["id"] for r in conn.execute("SELECT id FROM tasks")} == {parent, child}
    conn.close()


def test_node_schema_offers_no_claude_harness():
    """A cold client must not be able to pick a harness that bills Claude."""
    tool = next(t for t in mcp.TOOLS
                if t["name"] == "silicorism_plan_and_submit")
    node = tool["inputSchema"]["properties"]["nodes"]["items"]["properties"]
    assert node["harness"]["enum"] == ["pi", "verify"]
    assert "claude" not in mcp.INSTRUCTIONS.lower().split("never")[0]
