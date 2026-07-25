"""Silicorism MCP server — pure-stdlib JSON-RPC 2.0 over stdio.

Exposes the orchestrator to Claude Code (or any MCP client) with four tools:
silicorism_plan_and_submit, silicorism_get_status, silicorism_start_workers,
silicorism_gc. No dependency on the `mcp` package — the stdio transport is just
newline-delimited JSON-RPC, so a few dozen lines of stdlib cover it.

Register with Claude Code:  claude mcp add silicorism -- silicorism-mcp
DB resolves from SILICORISM_DB or the per-repo default (see cli.default_db):
the git root's .git/silicorism.db, else ~/.config/silicorism/repos/<slug>/.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import cli
import db
import silicorism_tools
import skills

PROTOCOL = "2025-11-25"

# Operating protocol the orchestrating client (Claude Code) must follow. Sent as
# the MCP `initialize.instructions` so the client adopts it when this server loads.
INSTRUCTIONS = (
    "You are the Silicorism orchestrator. Follow this protocol every time:\n"
    "1. DISCOVER: call silicorism_list_skills to inventory available skills before "
    "planning.\n"
    "2. ZERO ASSUMPTIONS: if the user's request is ambiguous, missing specs, or "
    "lacks environment/test context, STOP and ask targeted clarifying questions. "
    "Do NOT queue any tasks until it is unambiguous.\n"
    "3. MASTER PLAN: evaluate multiple implementation routes, pick the optimal one, "
    "and present a plan with (a) selected route + trade-off rationale, (b) the DAG "
    "nodes with their skill assignments, and (c) each node's model, harness, and "
    "thinking level. Bind discovered skills to the nodes that need them.\n"
    "4. SUBMIT: call silicorism_plan_and_submit with `nodes` (the custom DAG). It "
    "auto-starts native tmux-pane workers. Tell the user to watch with "
    "`tmux attach -t silicorism-session`.\n"
    "5. VERIFY & LOOP: poll silicorism_get_status / silicorism_verify_and_continue. "
    "If not satisfied, inspect the failed task artifacts, formulate a corrective "
    "DAG, and resubmit until every requirement and test gate is 100% met.\n"
    "Models are opencode free tier (unlimited): use friendly names deepseek-v4-flash, "
    "nemotron-3-ultra, hy3, mimo-2.5, north-mini-code with thinking=high."
)
HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "cli.py")


def _db(args: dict) -> str:
    return args.get("db") or os.environ.get("SILICORISM_DB") or cli.default_db()


def _slug(text: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", (text or "feature").lower()).strip("-")
            or "feature")[:32]


def _spawn_workers(dbp: str, n: int) -> str:
    """Launch N detached native-pane workers that drain the queue."""
    env = dict(os.environ, SILICORISM_NATIVE="1")
    subprocess.Popen(
        [sys.executable, CLI, "run", "--db", dbp, "--workers", str(n), "--drain"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    return f"started {n} workers on {dbp}"


# --- tool handlers ----------------------------------------------------------

def _plan_and_submit(args: dict) -> str:
    """Submit a plan and auto-start workers in one action.

    Pass `nodes` for a custom DAG (each node: id, prompt, depends_on, harness,
    model, thinking, skills) — this is the orchestrator's real planning surface.
    Or pass `prompt` for the default 5-task pipeline fallback. Workers spawn
    automatically (SILICORISM_NATIVE=1) unless workers=0.
    """
    dbp = _db(args)
    db.init_db(dbp)
    conn = db.connect(dbp)
    try:
        nodes = args.get("nodes")
        if nodes:
            out = silicorism_tools.build_dag(
                conn, dbp, nodes, name=args.get("name"),
                base=args.get("base") or "main", cwd=args.get("cwd"))
            out["mode"] = "dag"
        elif args.get("prompt"):
            out = silicorism_tools.build_pipeline(
                conn, dbp, args.get("name") or _slug(args["prompt"]), args["prompt"],
                base=args.get("base") or "main",
                test_command=args.get("test_command") or "pytest -q",
                max_attempts=int(args.get("max_attempts") or 3))
            out["mode"] = "pipeline"
        else:
            raise ValueError("provide either 'nodes' (custom DAG) or 'prompt'")
    finally:
        conn.close()
    workers = int(args.get("workers", 3))
    if workers > 0:
        out["workers"] = _spawn_workers(dbp, workers)
    return json.dumps(out)


def _verify_and_continue(args: dict) -> str:
    """Verdict (satisfied? + failed-task artifacts/errors) for the re-loop.

    If not satisfied and a corrective `nodes` DAG is supplied, submit it and
    (re)start workers so the orchestrator can iterate until the goal is met.
    """
    dbp = _db(args)
    db.init_db(dbp)
    conn = db.connect(dbp)
    try:
        verdict = silicorism_tools.verify_status(conn)
        nodes = args.get("nodes")
        if nodes and not verdict["satisfied"]:
            verdict["resubmitted"] = silicorism_tools.build_dag(
                conn, dbp, nodes, name=args.get("name"),
                base=args.get("base") or "main", cwd=args.get("cwd"))
            workers = int(args.get("workers", 3))
            if workers > 0:
                verdict["workers"] = _spawn_workers(dbp, workers)
    finally:
        conn.close()
    return json.dumps(verdict)


def _get_status(args: dict) -> str:
    """Live DAG + P2P + worktree snapshot. (pipeline_id is accepted but the
    store keeps one shared queue, so the snapshot is global.)"""
    dbp = _db(args)
    db.init_db(dbp)
    conn = db.connect(dbp)
    try:
        return json.dumps(silicorism_tools.get_status(conn))
    finally:
        conn.close()


def _start_workers(args: dict) -> str:
    """Launch N detached native-pane workers that drain the queue."""
    dbp = _db(args)
    db.init_db(dbp)
    return _spawn_workers(dbp, int(args.get("count") or 3))


def _list_skills(args: dict) -> str:
    """Inventory available skills (global + local, both harnesses) for planning."""
    return json.dumps(skills.inventory(args.get("cwd")))


def _gc(args: dict) -> str:
    """Reclaim finished worktrees (failed=true also clears quarantined)."""
    dbp = _db(args)
    db.init_db(dbp)
    conn = db.connect(dbp)
    try:
        return json.dumps(silicorism_tools.gc_worktrees(
            conn, dbp, failed=bool(args.get("failed"))))
    finally:
        conn.close()


TOOLS = [
    {
        "name": "silicorism_plan_and_submit",
        "description": "Submit a plan AND auto-start native-pane workers in one "
                       "action. Pass 'nodes' for a custom DAG you design (per-node "
                       "harness/model/thinking/skills), or 'prompt' for the default "
                       "5-task pipeline. Watch live with: tmux attach -t "
                       "silicorism-session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "nodes": {
                    "type": "array",
                    "description": "Custom DAG. Each node designs one agent task.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Unique node id"},
                            "prompt": {"type": "string"},
                            "depends_on": {"type": "array", "items": {"type": "string"},
                                           "description": "ids of prerequisite nodes"},
                            "harness": {"type": "string", "enum": ["pi", "claude"]},
                            "model": {"type": "string",
                                      "description": "friendly name (deepseek-v4-flash, "
                                      "nemotron-3-ultra, hy3, mimo-2.5, north-mini-code) "
                                      "or full opencode id"},
                            "thinking": {"type": "string",
                                         "description": "off|minimal|low|medium|high|xhigh|max"},
                            "skills": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["id", "prompt"],
                    },
                },
                "prompt": {"type": "string",
                           "description": "Fallback: default 5-task pipeline goal"},
                "name": {"type": "string", "description": "Feature/branch name; when "
                         "set the DAG runs in a git worktree"},
                "base": {"type": "string", "description": "Base branch"},
                "test_command": {"type": "string"},
                "max_attempts": {"type": "integer"},
                "workers": {"type": "integer",
                            "description": "Workers to auto-start (default 3, 0 = none)"},
                "cwd": {"type": "string"},
                "db": {"type": "string", "description": "Override DB path"},
            },
        },
        "handler": _plan_and_submit,
    },
    {
        "name": "silicorism_verify_and_continue",
        "description": "Verification verdict for the re-loop: is the goal satisfied, "
                       "and if not, each failed task's artifact + last error. Supply "
                       "a corrective 'nodes' DAG to resubmit and restart workers, "
                       "iterating until satisfied or you stop.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "nodes": {"type": "array", "items": {"type": "object"},
                          "description": "Corrective DAG (same node schema as "
                                         "plan_and_submit); submitted only if not satisfied"},
                "name": {"type": "string"},
                "base": {"type": "string"},
                "workers": {"type": "integer"},
                "cwd": {"type": "string"},
                "db": {"type": "string"},
            },
        },
        "handler": _verify_and_continue,
    },
    {
        "name": "silicorism_list_skills",
        "description": "Inventory all available skills (global ~/.claude, ~/.pi and "
                       "local ./.claude, ./.pi) with name, harness, scope, and a "
                       "one-line description. Call this FIRST when planning so skills "
                       "can be bound to DAG nodes.",
        "inputSchema": {
            "type": "object",
            "properties": {"cwd": {"type": "string"}},
        },
        "handler": _list_skills,
    },
    {
        "name": "silicorism_get_status",
        "description": "Aggregated DAG status: task counts + satisfied verdict, each "
                       "failed task's artifact + last error, agents, recent execution "
                       "logs, P2P messages, and worktree states.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pipeline_id": {"type": "string"},
                "db": {"type": "string"},
            },
        },
        "handler": _get_status,
    },
    {
        "name": "silicorism_start_workers",
        "description": "Launch N detached workers that run pi/claude tasks live in "
                       "tmux panes until the queue drains.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Worker count"},
                "db": {"type": "string"},
            },
        },
        "handler": _start_workers,
    },
    {
        "name": "silicorism_gc",
        "description": "Garbage-collect finished worktrees; failed=true also "
                       "removes quarantined ones.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "failed": {"type": "boolean"},
                "db": {"type": "string"},
            },
        },
        "handler": _gc,
    },
]
_BY_NAME = {t["name"]: t for t in TOOLS}


def _public(tool: dict) -> dict:
    return {k: tool[k] for k in ("name", "description", "inputSchema")}


# --- JSON-RPC dispatch ------------------------------------------------------

def _result(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg: dict) -> dict | None:
    """Dispatch one JSON-RPC message; None means 'no response' (a notification)."""
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        pv = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        return _result(mid, {
            "protocolVersion": pv,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "silicorism", "version": "0.1.0"},
            "instructions": INSTRUCTIONS,
        })
    if method == "notifications/initialized" or method == "ping":
        return _result(mid, {}) if mid is not None else None
    if method == "tools/list":
        return _result(mid, {"tools": [_public(t) for t in TOOLS]})
    if method == "tools/call":
        params = msg.get("params") or {}
        tool = _BY_NAME.get(params.get("name"))
        if tool is None:
            return _error(mid, -32602, f"unknown tool {params.get('name')!r}")
        try:
            text = tool["handler"](params.get("arguments") or {})
            return _result(mid, {"content": [{"type": "text", "text": text}],
                                 "isError": False})
        except Exception as e:  # noqa: BLE001 - tool errors go back as isError
            return _result(mid, {"content": [{"type": "text", "text": str(e)}],
                                 "isError": True})
    if mid is None:
        return None  # unknown notification: stay silent
    return _error(mid, -32601, f"method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
