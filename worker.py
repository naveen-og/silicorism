"""A single worker process: claim -> run handler -> log -> mark done, repeat.

Signals (SIGINT/SIGTERM) flip a flag; the loop finishes cleanly and hands any
in-flight task back to the queue via requeue_agent_tasks before exit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
# A node that hangs at minute 2 used to hold its worker until minute 60: the
# 3600s cap in tmux.wait_for_exit is a ceiling, not a stall detector.
STALL_TIMEOUT_S = float(os.environ.get("SILICORISM_STALL_S") or 600)
# Set per worker process from its DB path: which queue's task ids these are.
_CAPTURE_SLUG = ""


class AgentAlive(RuntimeError):
    """The run ended while the agent was still running (stall or timeout).

    Distinct from a non-zero exit: there is still a live process and a live
    pane to kill, and that pane is not a post-mortem — it is a leak.
    """


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


def _capture_path(task_id) -> str:
    """Pane-capture log for a task, namespaced by the DB the id came from.

    The log dir is shared and every DB numbers its tasks from 1, so two runs'
    task 1 piped into one file: an agent that wrote no artifact had the other
    run's text reported as its output — and that output is handed to the next
    node as its context.
    """
    base = tmux.log_path(task_id)
    if not _CAPTURE_SLUG:
        return base
    head, tail = os.path.split(base)
    return os.path.join(head, f"{_CAPTURE_SLUG}-{tail}")


def _artifact_path(task_id) -> str:
    """Clean-text artifact written by autoexit.ts (TUI logs are ANSI soup)."""
    return _capture_path(task_id) + ".artifact"


def _usage_path(task_id) -> str:
    """Token counts written by autoexit.ts when the agent settles."""
    return _capture_path(task_id) + ".usage"


def _clear_capture(task_id) -> None:
    """Empty a task's log and artifact before its pane opens.

    Namespacing is not quite enough on its own: dropping a DB restarts the ids,
    so a second run in the same repo would still find the first run's files.
    """
    try:
        os.remove(_usage_path(task_id))
    except OSError:
        pass
    for path in (_capture_path(task_id), _artifact_path(task_id)):
        try:
            open(path, "w").close()
        except OSError:
            pass


def _native_payload(task, *, conn=None) -> str:
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
    data["usage"] = _usage_path(task["id"])
    # Mail waiting at launch goes into the prompt. The channel was pull-only,
    # and nothing downstream ever stopped mid-run to go and look, so a peer's
    # answer sat unread until the DAG was over. Drained here, so a later
    # `silicorism-msg poll` does not replay what the prompt already shows.
    if conn is not None and data.get("agent_id"):
        pending = db.poll_inter_agent_messages(conn, data["agent_id"])
        if pending:
            data["inbox"] = [f"{m['sender_id']}: {m['content']}" for m in pending]
    # tmux sets the pane's cwd, but the command builder needs it too, to see
    # whether the repo has a context file worth passing. A worktree node carries
    # that path on the row rather than in the payload.
    data.setdefault("cwd", _task_cwd(task))
    return json.dumps(data)


def _gate_command(task) -> str | None:
    """An agent node's own acceptance test, run by the worker, not by the agent.

    The pane's exit code says the agent process ended, not that the work is
    correct: autoexit.ts exits 0 for any run that settled without an error stop
    reason, so an agent that ran nothing at all still "succeeds". Seen for real:
    a node reported completed while its own test file had `fail 1`.

    Agent types only. For a `verify` node the test command IS the handler, and
    gating it again would run a possibly non-idempotent suite twice.
    """
    if task["task_type"] not in handlers.NATIVE_AGENTS:
        return None
    try:
        data = json.loads(task["payload"] or "{}")
    except (json.JSONDecodeError, ValueError):
        return None
    return data.get("test_command") if isinstance(data, dict) else None


def _requires_spec(task) -> dict:
    """The node's declared deliverables, or {} when it declared none."""
    if task["task_type"] not in handlers.NATIVE_AGENTS:
        return {}
    try:
        data = json.loads(task["payload"] or "{}")
    except (json.JSONDecodeError, ValueError):
        return {}
    spec = data.get("requires") if isinstance(data, dict) else None
    return spec if isinstance(spec, dict) else {}


# Never worth walking to find out what a node touched.
_SNAPSHOT_SKIP_DIRS = frozenset({
    ".git", "node_modules", "target", "dist", "build", "out", "vendor",
    ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
})
# Upper bound on files tracked, so the fence cannot stall on a huge tree.
_SNAPSHOT_MAX_FILES = 20000


def _claimed_files(task) -> list[str] | None:
    if task["task_type"] not in handlers.NATIVE_AGENTS:
        return None
    try:
        data = json.loads(task["payload"] or "{}")
    except (json.JSONDecodeError, ValueError):
        return None
    claims = data.get("writes") if isinstance(data, dict) else None
    return claims if isinstance(claims, list) and claims else None


def _unclaimed_snapshot(task) -> dict[str, tuple[int, int]] | None:
    """(mtime_ns, size) of every existing file this node did NOT claim.

    `writes` was a declaration and nothing more: build_dag used it to reject two
    unordered nodes claiming one file, and after that no one looked. A builder
    could rewrite the tests it was measured by — the oldest way to turn a red
    suite green — and every gate downstream would agree the run had succeeded.

    ponytail: mtime+size, not content hashes. An edit that preserves both is
    possible in principle; an agent writing through its file tools does not do
    it, and hashing a large tree twice per node is a real cost for a case that
    has never happened.
    """
    claims = _claimed_files(task)
    if claims is None:
        return None
    cwd = _task_cwd(task)
    if not cwd or not os.path.isdir(cwd):
        return None
    claimed = {os.path.normpath(c) for c in claims}
    seen: dict[str, tuple[int, int]] = {}
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in _SNAPSHOT_SKIP_DIRS]
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.normpath(os.path.relpath(path, cwd))
            if rel in claimed:
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            seen[rel] = (st.st_mtime_ns, st.st_size)
            if len(seen) >= _SNAPSHOT_MAX_FILES:
                return seen
    return seen


def _assert_unclaimed_untouched(task, before: dict | None) -> str:
    """Fail the node if it changed a file it did not claim. Returns a note."""
    if before is None:
        return ""
    after = _unclaimed_snapshot(task)
    if after is None:
        return ""
    touched = sorted(rel for rel, stamp in after.items()
                     if rel in before and before[rel] != stamp)
    removed = sorted(rel for rel in before if rel not in after)
    if touched or removed:
        detail = ", ".join(touched + [f"{r} (deleted)" for r in removed][:20])
        raise handlers.RequirementsUnmet(
            "node modified files it did not claim in `writes`: " + detail)
    # New files are allowed — go.sum, a lockfile, a CONTEXT.md — but they are
    # reported, because "what else appeared" is the operator's business.
    created = sorted(rel for rel in after if rel not in before)
    if created:
        return "\n\nunclaimed files created: " + ", ".join(created[:20])
    return ""


def _apply_gate(task, artifact: str, unclaimed_before: dict | None = None) -> str:
    """Append the node's gate verdict to its artifact; raise if the gate fails.

    Both execution paths go through here. The gate used to live only in the
    native pane branch, so a node carrying `test_command` was enforced or
    ignored depending on whether SILICORISM_NATIVE happened to be set, with
    nothing saying which you got.

    Two gates, in this order. `requires` is checked first because it catches
    what a test suite structurally cannot: a deliverable that was never
    written. Tests only fail on what they cover, so a node that quietly skipped
    half its prompt — capped a list at 3 where the spec said 6, stubbed a
    function with `TODO`, dropped the test it was told to add — passes a green
    suite and reports success. Checking the plan's own words costs one stat and
    one substring search per claim.
    """
    unmet = handlers.check_requires(_requires_spec(task), _task_cwd(task))
    if unmet:
        raise handlers.RequirementsUnmet(
            "declared deliverables missing:\n- " + "\n- ".join(unmet))
    if unmet == [] and _requires_spec(task):
        artifact += "\n\nrequires: all declared deliverables present"
    artifact += _assert_unclaimed_untouched(task, unclaimed_before)

    gate = _gate_command(task)
    if not gate:
        return artifact
    return artifact + "\n\n" + handlers.verify(json.dumps(
        {"test_command": gate, "cwd": _task_cwd(task)}))


def _timeouts(task) -> tuple[float, float]:
    """(wall-clock cap, stall window) for this node, in seconds."""
    try:
        data = json.loads(task["payload"] or "{}")
        if not isinstance(data, dict):
            data = {}
    except (json.JSONDecodeError, ValueError):
        data = {}
    return (float(data.get("timeout_s") or 3600.0),
            float(data.get("stall_timeout_s", STALL_TIMEOUT_S)))


def _db_slug(path: str) -> str:
    """Repo-ish name for a db path: /repo/.git/silicorism.db -> repo."""
    parts = [p for p in os.path.dirname(os.path.abspath(path)).split(os.sep)
             if p and p != ".git"]
    return (parts[-1] if parts else "db")[:12]


def _pane_label(task) -> str:
    """"<run>/<agent id>" — one session holds panes from several runs at once,
    and telling them apart by token counter is not a workflow."""
    data = {}
    try:
        loaded = json.loads(task["payload"] or "{}")
        if isinstance(loaded, dict):
            data = loaded
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    label = data.get("agent_id") or f"{task['task_type']}-{task['id']}"
    return f"{_db_slug(data['db'])}/{label}" if data.get("db") else str(label)


def _place_pane(conn, task, command: str, sentinel: str, logfile: str):
    """Put the agent in a grid pane; fall back to a window if tmux misbehaves.

    Returns (window, pane_id); pane_id is None on the fallback path.
    """
    tid = task["id"]
    cwd = _task_cwd(task)
    try:
        window, pane = tmux.grid_pane(tid, _pane_label(task), cwd, command,
                                      sentinel, logfile=logfile)
    except Exception:  # noqa: BLE001 - the grid is a viewport, never a dependency
        window = tmux.run_task_in_pane(tid, task["task_type"], cwd, command,
                                       sentinel, logfile=logfile)
        try:  # a retry after a grid run would otherwise keep the closed pane
            db.set_pane_target(conn, tid, window)
        except Exception:  # noqa: BLE001
            pass
        return window, None
    # Recorded only after the launch succeeded: a failed write here must not
    # re-enter the fallback, which would start a second agent for one task.
    try:
        db.set_pane_target(conn, tid, f"{window}.{pane}")
    except Exception:  # noqa: BLE001 - display metadata, not a dependency
        pass
    return window, pane


def _mark_pane(task_id, pane, *, failed: bool, window=None) -> None:
    """Retitle the grid pane, or the legacy window when there is no pane id."""
    try:
        if pane:
            tmux.mark_pane_done(pane, failed=failed)
        elif window:  # run_task_in_pane names it task-<id>-<type>, not task-<id>
            tmux.mark_window_done(window, failed=failed)
    except Exception:  # noqa: BLE001
        pass


# Directories an agent's work never shows up in; walking node_modules on every
# beat would cost more than the signal is worth.
_SKIP_DIRS = {"node_modules", "__pycache__", "venv", "dist", "build", "target"}
# A queue DB inside the task's own directory is written by every heartbeat, so
# its WAL would report progress for an agent that has done nothing.
_SKIP_SUFFIXES = (".db", ".db-wal", ".db-shm")


def newest_mtime(root: str) -> float:
    """Newest mtime under `root`, or 0.0 if it cannot be read.

    The progress signal has to tell drawing apart from working: tmux pipe-pane
    records every repaint, so a spinning TUI grows its log forever while the
    agent is wedged. Files on disk do not lie about that.

    ponytail: a full walk every beat; swap for inotify if it ever profiles.
    """
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.endswith(_SKIP_SUFFIXES):
                continue
            try:
                m = os.stat(os.path.join(dirpath, name)).st_mtime
            except OSError:  # raced deletion, broken symlink
                continue
            newest = max(newest, m)
    return newest


def _kill_pane(pane, window=None) -> None:
    """Close a finished pane; best-effort, no verdict ever depends on it."""
    try:
        if pane:
            tmux.kill_pane(pane)
        elif window:
            tmux.kill_window(window)
    except Exception:  # noqa: BLE001
        pass


def _stop_and_beat(conn, agent_id, task_id, cwd, *, every: float = 30.0,
                   stall_s: float = STALL_TIMEOUT_S, reason=None):
    """Poll callback for wait_for_exit: stop flag, heartbeat AND progress.

    _run_native blocks for the agent's whole run, so without a beat here the
    worker's last_seen freezes at claim time; after 300s db.reap_stale
    (db.py, called from every silicorism_wait loop) hands the task to a
    second worker, which launches a second live agent in the same directory.
    Seen for real: task 2 claimed by worker-0 at 09:30:02 and again by
    worker-1 at 09:35:03, two panes editing the same files.

    The same tick fingerprints the task's tree, because a beat on its own
    cannot tell a working agent from a wedged one — `busy` with a fresh
    last_seen looked like healthy progress for an hour of wall clock. Once the
    tree has been quiet for `stall_s` the poll stops the wait and records why
    in `reason`, so the caller can tell a stall from the operator's SIGINT.
    """
    state = {"next": 0.0, "mtime": None, "progress": time.monotonic()}

    def poll() -> bool:
        if _STOP:
            if reason is not None:
                reason["stopped"] = True
            return True
        if time.monotonic() < state["next"]:
            return False
        state["next"] = time.monotonic() + every
        try:
            db.heartbeat(conn, agent_id, "busy", task_id)
        except Exception:  # noqa: BLE001 - a missed beat must not kill the run
            pass
        m = newest_mtime(cwd)
        if state["mtime"] is None or m > state["mtime"]:
            state["mtime"] = m
            state["progress"] = time.monotonic()
            try:
                db.touch_progress(conn, task_id)
            except Exception:  # noqa: BLE001
                pass
            return False
        idle = time.monotonic() - state["progress"]
        if stall_s > 0 and idle >= stall_s:  # <= 0 disables stall detection
            if reason is not None:
                reason["stalled"] = idle
            return True
        return False

    return poll


def _record_usage(conn, tid) -> None:
    """Bank the token counts autoexit.ts left behind, if it left any.

    Absent is normal — a claude-harness node, a pane killed before it settled,
    an older extension — so nothing here may turn into a task failure.
    """
    try:
        with open(_usage_path(tid)) as fh:
            u = json.load(fh)
        if not isinstance(u, dict):
            return
    except (OSError, json.JSONDecodeError, ValueError):
        return
    provider, model = u.get("provider"), u.get("model") or ""
    name = f"{provider}/{model}" if provider else model
    try:
        db.record_usage(conn, tid,
                        input_tokens=u.get("input") or 0,
                        output_tokens=u.get("output") or 0,
                        cost_usd=handlers.usage_cost(name, u),
                        model_used=name)
    except Exception:  # telemetry must never fail the node it measures
        pass


def _run_native(conn, task, agent_id, command: str, unclaimed_before=None) -> None:
    """Execute an agent live in a tmux pane; raise on non-zero/absent exit."""
    tid = task["id"]
    sentinel = tmux.sentinel_path(tid)
    logf = _capture_path(tid)
    _clear_capture(tid)
    tmux.ensure_session()
    win, pane = _place_pane(conn, task, command, sentinel, logf)
    # Covers the whole body, so no error route leaves the pane titled RUNNING.
    try:
        db.log(conn, tid, agent_id, f"native pane {win}{'.' + pane if pane else ''}")
        cap, stall_s = _timeouts(task)
        reason: dict = {}
        # The beat is also when the stall is measured, so a node asking for a
        # short window must be looked at more often than every 30s.
        beat = min(30.0, stall_s / 2) if stall_s > 0 else 30.0
        code = tmux.wait_for_exit(
            sentinel, timeout=cap,
            stop=_stop_and_beat(conn, agent_id, tid, _task_cwd(task),
                                every=beat, stall_s=stall_s, reason=reason))
        if code != 0:
            if reason.get("stalled"):
                raise AgentAlive("native agent stalled: no progress for "
                                 f"{int(reason['stalled'])}s")
            if reason.get("stopped"):
                # requeue_agent_tasks hands the work back; the pane must not
                # outlive the worker that was supervising it.
                raise AgentAlive("worker stopping, agent still running")
            if code is None:
                raise AgentAlive(f"native agent exit timeout ({int(cap)}s)")
            raise RuntimeError(f"native agent exit {code}")
        # Prefer the clean autoexit artifact; fall back to the raw log tail.
        artifact = (tmux.read_log_tail(_artifact_path(tid), max_chars=4000)
                    or tmux.read_log_tail(logf)
                    or f"native pane {win} exit 0")
        # Raises on non-zero, so the except below fails the task: this is the
        # only thing between a claimed pass and a real one.
        artifact = _apply_gate(task, artifact, unclaimed_before)
        db.complete_task(conn, tid, artifact=artifact)
        db.log(conn, tid, agent_id, f"completed (native): {win}")
    except Exception as err:
        _mark_pane(tid, pane, failed=True, window=win)
        if isinstance(err, AgentAlive):
            # A timed-out pane leaks the agent plus everything it spawned; a
            # plain non-zero exit is already dead and its scrollback is the
            # post-mortem, so that one is kept.
            tmux.trim_log(logf)
            _kill_pane(pane, win)
        raise
    finally:
        # Tokens a failed node burnt were still paid for, so this runs on every
        # exit path, not just the happy one.
        _record_usage(conn, tid)
        # pipe-pane records every repaint; four agents left 13 MB behind once,
        # and a failing run left 137 MB — so this trims on every exit path.
        tmux.trim_log(logf)
    _mark_pane(tid, pane, failed=False, window=win)
    # The artifact is already captured; a session that keeps every DONE pane
    # becomes unreadable after a few runs — 13 panes from four runs once, most
    # of them finished shells.
    _kill_pane(pane, win)


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
    global _CAPTURE_SLUG
    _CAPTURE_SLUG = re.sub(r"[^A-Za-z0-9_-]", "_", _db_slug(db_path))
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
                    # An empty poll only means "nothing claimable right now" —
                    # a long scout blocks its dependents. Exiting here would
                    # leave one worker to run a fan-out serially.
                    c = db.counts(conn)
                    if not c["pending"] and not c["processing"]:
                        break
                    idle_loops = 0
                time.sleep(idle_sleep)
                continue

            idle_loops = 0
            tid = task["id"]
            db.heartbeat(conn, agent_id, "busy", tid)
            db.log(conn, tid, agent_id, f"claimed {task['task_type']}")
            context = db.dep_artifacts(conn, tid)
            # SILICORISM_NATIVE: pi/claude tasks run as live CLI processes in a pane.
            native_cmd = (handlers.native_command(
                task["task_type"], _native_payload(task, conn=conn), context,
                cli_path=_CLI)
                if _NATIVE else None)
            if native_cmd is None:
                _open_task_window(db_path, task)
            # Taken before the agent runs: the fence compares against it after.
            unclaimed_before = _unclaimed_snapshot(task)
            try:
                if native_cmd is not None:
                    _run_native(conn, task, agent_id, native_cmd, unclaimed_before)
                else:
                    result = handlers.run(task["task_type"], task["payload"], context)
                    result = _apply_gate(task, result, unclaimed_before)
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
