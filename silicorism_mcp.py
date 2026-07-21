"""Silicorism MCP server — pure-stdlib JSON-RPC 2.0 over stdio.

Exposes the orchestrator to Claude Code (or any MCP client) with four tools:
silicorism_plan_and_submit, silicorism_get_status, silicorism_start_workers,
silicorism_gc. No dependency on the `mcp` package — the stdio transport is just
newline-delimited JSON-RPC, so a few dozen lines of stdlib cover it.

Register with Claude Code:  claude mcp add silicorism -- python silicorism_mcp.py
DB resolves from SILICORISM_DB or the CWD-relative default (see cli.default_db).
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

PROTOCOL = "2025-11-25"
HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "cli.py")


def _db(args: dict) -> str:
    return args.get("db") or os.environ.get("SILICORISM_DB") or cli.default_db()


def _slug(text: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", (text or "feature").lower()).strip("-")
            or "feature")[:32]


# --- tool handlers ----------------------------------------------------------

def _plan_and_submit(args: dict) -> str:
    """Build + submit the 5-task DAG (worktree->scout->builder->fixer->cleanup)."""
    prompt = args.get("prompt")
    if not prompt:
        raise ValueError("prompt is required")
    dbp = _db(args)
    db.init_db(dbp)
    conn = db.connect(dbp)
    try:
        p = silicorism_tools.build_pipeline(
            conn, dbp, args.get("name") or _slug(prompt), prompt,
            base=args.get("base") or "main",
            test_command=args.get("test_command") or "pytest -q",
            max_attempts=int(args.get("max_attempts") or 3))
    finally:
        conn.close()
    return json.dumps(p)


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
    n = int(args.get("count") or 3)
    dbp = _db(args)
    db.init_db(dbp)
    env = dict(os.environ, SILICORISM_NATIVE="1")
    subprocess.Popen(
        [sys.executable, CLI, "run", "--db", dbp, "--workers", str(n), "--drain"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    return f"started {n} workers on {dbp}"


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
        "description": "Build and submit the 5-task feature DAG (worktree, scout, "
                       "builder, fixer, cleanup) to the orchestrator queue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "What to build"},
                "name": {"type": "string", "description": "Feature/branch name"},
                "base": {"type": "string", "description": "Base branch"},
                "test_command": {"type": "string"},
                "max_attempts": {"type": "integer"},
                "db": {"type": "string", "description": "Override DB path"},
            },
            "required": ["prompt"],
        },
        "handler": _plan_and_submit,
    },
    {
        "name": "silicorism_get_status",
        "description": "Live DAG execution status (task counts, agents), recent "
                       "P2P messages, and worktree states.",
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
