"""A single worker process: claim -> run handler -> log -> mark done, repeat.

Signals (SIGINT/SIGTERM) flip a flag; the loop finishes cleanly and hands any
in-flight task back to the queue via requeue_agent_tasks before exit.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import time

import db
import handlers
import tmux_orchestrator as tmux

_STOP = False
_TMUX = bool(os.environ.get("SILICORISM_TMUX"))
_NATIVE = bool(os.environ.get("SILICORISM_NATIVE"))
_CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli.py")


def _on_signal(signum, _frame):
    global _STOP
    _STOP = True


def _task_cwd(task) -> str:
    if task["worktree_path"]:
        return task["worktree_path"]
    try:
        return json.loads(task["payload"] or "{}").get("cwd") or "."
    except (json.JSONDecodeError, ValueError, AttributeError):
        return "."


def _artifact_path(task_id) -> str:
    """Clean-text artifact written by autoexit.ts (TUI logs are ANSI soup)."""
    return tmux.log_path(task_id) + ".artifact"


def _native_payload(task) -> str:
    """Task payload with the artifact path injected for pi TUI runs."""
    if task["task_type"] != "pi":
        return task["payload"]
    try:
        data = json.loads(task["payload"] or "{}")
        if not isinstance(data, dict):
            return task["payload"]
    except (json.JSONDecodeError, ValueError):
        return task["payload"]
    data["artifact"] = _artifact_path(task["id"])
    return json.dumps(data)


def _pane_label(task) -> str:
    """Agent id makes the best pane label; fall back to the task type."""
    try:
        data = json.loads(task["payload"] or "{}")
        if isinstance(data, dict) and data.get("agent_id"):
            return str(data["agent_id"])
    except (json.JSONDecodeError, ValueError):
        pass
    return f"{task['task_type']}-{task['id']}"


def _place_pane(conn, task, command: str, sentinel: str, logfile: str):
    """Put the agent in a grid pane; fall back to a window if tmux misbehaves.

    Returns (window, pane_id); pane_id is None on the fallback path.
    """
    tid = task["id"]
    cwd = _task_cwd(task)
    try:
        window, pane = tmux.grid_pane(tid, _pane_label(task), cwd, command,
                                      sentinel, logfile=logfile)
        db.set_pane_target(conn, tid, f"{window}.{pane}")
        return window, pane
    except Exception:  # noqa: BLE001 - the grid is a viewport, never a dependency
        window = tmux.run_task_in_pane(tid, task["task_type"], cwd, command,
                                       sentinel, logfile=logfile)
        return window, None


def _mark_pane(task_id, pane, *, failed: bool) -> None:
    """Retitle the grid pane, or the legacy window when there is no pane id."""
    try:
        if pane:
            tmux.mark_pane_done(pane, failed=failed)
        else:
            tmux.mark_done(task_id, failed=failed)
    except Exception:  # noqa: BLE001
        pass


def _run_native(conn, task, agent_id, command: str) -> None:
    """Execute an agent live in a tmux pane; raise on non-zero/absent exit."""
    tid = task["id"]
    sentinel = tmux.sentinel_path(tid)
    logf = tmux.log_path(tid)
    tmux.ensure_session()
    win, pane = _place_pane(conn, task, command, sentinel, logf)
    db.log(conn, tid, agent_id, f"native pane {win}{'.' + pane if pane else ''}")
    try:
        code = tmux.wait_for_exit(sentinel, stop=lambda: _STOP)
        if code != 0:
            raise RuntimeError(
                f"native agent exit {code if code is not None else 'timeout'}")
    except Exception:
        _mark_pane(tid, pane, failed=True)
        raise
    # Prefer the clean autoexit artifact; fall back to the raw log tail.
    artifact = (tmux.read_log_tail(_artifact_path(tid), max_chars=4000)
                or tmux.read_log_tail(logf)
                or f"native pane {win} exit 0")
    db.complete_task(conn, tid, artifact=artifact)
    db.log(conn, tid, agent_id, f"completed (native): {win}")
    _mark_pane(tid, pane, failed=False)


def _open_task_window(db_path, task) -> None:
    """Under SILICORISM_TMUX (in-process mode), tail the task's logs in a window."""
    if not _TMUX:
        return
    cmd = f"silicorism logs --db {shlex.quote(db_path)} --task {task['id']} --follow"
    try:
        tmux.ensure_session()
        tmux.task_window(task["id"], _task_cwd(task), cmd)
    except Exception:  # noqa: BLE001 - tmux is optional; never block execution
        pass


def _close_task_window(task_id, *, failed) -> None:
    if not _TMUX:
        return
    try:
        tmux.mark_done(task_id, failed=failed)
    except Exception:  # noqa: BLE001
        pass


def run_worker(db_path: str, agent_id: str, *, idle_sleep: float = 0.1,
               max_idle_loops: int = 0) -> None:
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    conn = db.connect(db_path)
    db.heartbeat(conn, agent_id, "idle")
    idle_loops = 0
    try:
        while not _STOP:
            task = db.claim_task(conn, agent_id)
            if task is None:
                idle_loops += 1
                db.heartbeat(conn, agent_id, "idle")
                db.checkpoint(conn)  # idle-loop WAL maintenance
                if max_idle_loops and idle_loops >= max_idle_loops:
                    break
                time.sleep(idle_sleep)
                continue

            idle_loops = 0
            tid = task["id"]
            db.heartbeat(conn, agent_id, "busy", tid)
            db.log(conn, tid, agent_id, f"claimed {task['task_type']}")
            context = db.dep_artifacts(conn, tid)
            # SILICORISM_NATIVE: pi/claude tasks run as live CLI processes in a pane.
            native_cmd = (handlers.native_command(
                task["task_type"], _native_payload(task), context, cli_path=_CLI)
                if _NATIVE else None)
            if native_cmd is None:
                _open_task_window(db_path, task)
            try:
                if native_cmd is not None:
                    _run_native(conn, task, agent_id, native_cmd)
                else:
                    result = handlers.run(task["task_type"], task["payload"], context)
                    db.complete_task(conn, tid, artifact=result)
                    db.log(conn, tid, agent_id, f"completed: {result[:120]}")
                    _close_task_window(tid, failed=False)
            except Exception as err:  # noqa: BLE001 - any handler error is a task failure
                status = db.fail_task(conn, tid)
                db.log(conn, tid, agent_id, f"error: {err}",
                       level="error", metadata=f"requeued={status=='pending'}")
                if status == "pending":
                    # Model escalation ladder: retry on the next stronger model.
                    stronger = handlers.escalate_payload(
                        task["task_type"], task["payload"])
                    if stronger:
                        db.set_payload(conn, tid, stronger)
                        db.log(conn, tid, agent_id, "escalated model for retry")
                if native_cmd is None:  # native panes are marked in _run_native
                    _close_task_window(tid, failed=True)
    finally:
        db.requeue_agent_tasks(conn, agent_id)
        db.heartbeat(conn, agent_id, "stopped")
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="orchestrator worker")
    p.add_argument("--db", required=True)
    p.add_argument("--agent-id", default=f"agent-{os.getpid()}")
    p.add_argument("--idle-sleep", type=float, default=0.1)
    p.add_argument("--max-idle-loops", type=int, default=0,
                   help="exit after this many empty polls (0 = run forever)")
    args = p.parse_args()
    run_worker(args.db, args.agent_id,
               idle_sleep=args.idle_sleep, max_idle_loops=args.max_idle_loops)


if __name__ == "__main__":
    main()
