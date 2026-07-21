"""A single worker process: claim -> run handler -> log -> mark done, repeat.

Signals (SIGINT/SIGTERM) flip a flag; the loop finishes cleanly and hands any
in-flight task back to the queue via requeue_agent_tasks before exit.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import time

import db
import handlers
import tmux_orchestrator as tmux

_STOP = False
_TMUX = bool(os.environ.get("HERDR_TMUX"))
_NATIVE = bool(os.environ.get("HERDR_NATIVE"))
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


def _run_native(conn, task, agent_id, command: str) -> None:
    """Execute an agent live in a tmux pane; raise on non-zero/absent exit."""
    tid = task["id"]
    cwd = _task_cwd(task)
    sentinel = tmux.sentinel_path(tid)
    tmux.ensure_session()
    win = tmux.run_task_in_pane(tid, task["task_type"], cwd, command, sentinel)
    db.log(conn, tid, agent_id, f"native pane {win} at {cwd}")
    code = tmux.wait_for_exit(sentinel, stop=lambda: _STOP)
    if code != 0:
        raise RuntimeError(f"native agent exit {code if code is not None else 'timeout'}")
    db.complete_task(conn, tid, artifact=f"native pane {win} exit 0")
    db.log(conn, tid, agent_id, f"completed (native): {win}")
    tmux.mark_done(tid, failed=False)


def _open_task_window(db_path, task) -> None:
    """Under HERDR_TMUX (in-process mode), tail the task's logs in a window."""
    if not _TMUX:
        return
    cmd = f"python cli.py logs --db {db_path} --task {task['id']} --follow"
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
            # HERDR_NATIVE: pi/claude tasks run as live CLI processes in a pane.
            native_cmd = (handlers.native_command(
                task["task_type"], task["payload"], context, cli_path=_CLI)
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
                if native_cmd is not None:
                    try:
                        tmux.mark_done(tid, failed=True)
                    except Exception:  # noqa: BLE001
                        pass
                else:
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
