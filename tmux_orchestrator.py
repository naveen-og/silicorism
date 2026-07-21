"""tmux supervisor: a live session with a dashboard window plus one window per
running task, streaming the agent's stdout/stderr in real time.

Pure stdlib — every tmux action is a subprocess call, so the command strings
are unit-testable without a running server.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time

SESSION = "silicorism-session"
SENTINEL_DIR = os.path.join(tempfile.gettempdir(), "silicorism-sentinels")
CONFIG_DIR = os.path.expanduser("~/.config/silicorism")
LOG_DIR = os.path.join(CONFIG_DIR, "logs")


def log_path(task_id) -> str:
    """Path to a task's captured stdout/stderr log (dir created on demand)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    return os.path.join(LOG_DIR, f"task-{task_id}.log")


def read_log_tail(path: str, max_chars: int = 2000) -> str:
    """Return the tail of a captured log for artifact hand-off (empty if none)."""
    try:
        data = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    return data[-max_chars:].strip()


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=15)


def session_exists(session: str = SESSION) -> bool:
    return _tmux("has-session", "-t", session).returncode == 0


def ensure_session(session: str = SESSION) -> None:
    """Create a detached session with a 'dashboard' window if none exists."""
    if not session_exists(session):
        _tmux("new-session", "-d", "-s", session, "-n", "dashboard")


def _window_target(session: str, name: str) -> str:
    return f"{session}:{name}"


def task_window(task_id, cwd: str, command: str, *, session: str = SESSION) -> str:
    """Open a window named task-<id> at <cwd> and run <command> live in it.

    Returns the window name. The command streams to the terminal; when it exits
    the shell stays open (remain-on-exit) so logs are inspectable.
    """
    name = f"task-{task_id}"
    _tmux("new-window", "-t", session, "-n", name, "-c", cwd)
    target = _window_target(session, name)
    _tmux("set-option", "-t", target, "remain-on-exit", "on")
    # send-keys runs it in the pane's shell so stdout/stderr stream in real time.
    _tmux("send-keys", "-t", target, command, "Enter")
    return name


def mark_done(task_id, *, session: str = SESSION, failed: bool = False) -> None:
    """Rename a finished task's window to task-<id>-done / -failed."""
    name = f"task-{task_id}"
    suffix = "failed" if failed else "done"
    _tmux("rename-window", "-t", _window_target(session, name), f"{name}-{suffix}")


def kill_session(session: str = SESSION) -> None:
    _tmux("kill-session", "-t", session)


# --- native CLI execution in a live pane -----------------------------------

def sentinel_path(task_id) -> str:
    os.makedirs(SENTINEL_DIR, exist_ok=True)
    return os.path.join(SENTINEL_DIR, f"task-{task_id}.exit")


def run_task_in_pane(task_id, task_type: str, cwd: str, command: str,
                     sentinel: str, *, session: str = SESSION,
                     logfile: str | None = None) -> str:
    """Open task-<id>-<type> at <cwd> and run <command> live, capturing exit.

    stdout+stderr both stream live to the pane AND tee to <logfile> so the
    worker can read a rich artifact for downstream tasks. The command's own
    exit code (not tee's) is written atomically to <sentinel> for polling;
    remain-on-exit keeps the pane open for post-mortem inspection.
    Returns the window name.
    """
    name = f"task-{task_id}-{task_type}"
    if os.path.exists(sentinel):
        os.remove(sentinel)
    if logfile is None:
        logfile = log_path(task_id)
    _tmux("new-window", "-t", session, "-n", name, "-c", cwd)
    target = _window_target(session, name)
    _tmux("set-option", "-t", target, "remain-on-exit", "on")
    tmp = shlex.quote(sentinel + ".tmp")
    fin = shlex.quote(sentinel)
    log = shlex.quote(logfile)
    # brace group's exit ($?) captured before the pipe, so tee doesn't mask it.
    wrapped = f"{{ {command}; echo $? > {tmp}; }} 2>&1 | tee {log}; mv {tmp} {fin}"
    _tmux("send-keys", "-t", target, wrapped, "Enter")
    return name


def wait_for_exit(sentinel: str, *, timeout: float = 3600.0, poll: float = 0.5,
                  stop=None) -> int | None:
    """Poll a sentinel file for the task's exit code. None on timeout/stop."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop and stop():
            return None
        if os.path.exists(sentinel):
            try:
                return int(open(sentinel).read().strip() or "1")
            except (ValueError, OSError):
                return 1
        time.sleep(poll)
    return None


# --- supervisor layout ------------------------------------------------------

def supervisor_layout(db_path: str, *, agent: str = "pi", extension: str | None = None,
                      launch: bool = False, session: str = SESSION) -> None:
    """Window 0 split into orchestrator agent (top) + live dashboard (bottom)."""
    ensure_session(session)
    win = _window_target(session, "dashboard")
    # bottom pane: the polling dashboard.
    _tmux("split-window", "-v", "-t", win, "-c", ".")
    _tmux("send-keys", "-t", f"{win}.1",
          f"python cli.py dashboard --db {shlex.quote(db_path)}", "Enter")
    # top pane: the orchestrator agent (pi/claude), optionally auto-started.
    if launch:
        if agent == "pi":
            cmd = "pi" + (f" -e {shlex.quote(extension)}" if extension else "")
        else:
            cmd = "claude"
        _tmux("send-keys", "-t", f"{win}.0", cmd, "Enter")


def launch_dashboard(db_path: str, *, session: str = SESSION) -> None:
    """Run the polling dashboard inside window 0 of the session."""
    ensure_session(session)
    cmd = f"python cli.py dashboard --db {shlex.quote(db_path)}"
    _tmux("send-keys", "-t", _window_target(session, "dashboard"), cmd, "Enter")


if __name__ == "__main__":
    # self-check: command construction is correct without touching a real server.
    from unittest.mock import patch

    calls = []

    def fake(*args, **kw):
        calls.append(list(args[0]))
        class R:  # minimal CompletedProcess stand-in
            returncode = 1  # force ensure_session to create
            stdout = stderr = ""
        return R()

    sent = sentinel_path("9")
    with patch("subprocess.run", side_effect=fake):
        ensure_session()
        task_window(7, "/tmp/worktrees/x", "pi --model hy3 'go'")
        mark_done(7)
        run_task_in_pane(9, "pi", "/tmp/worktrees/y", "pi -p 'go'", sent)
    flat = [" ".join(c) for c in calls]
    assert any("new-session -d -s silicorism-session" in f for f in flat), flat
    assert any("new-window -t silicorism-session -n task-7 -c /tmp/worktrees/x" in f
               for f in flat), flat
    assert any("rename-window" in f and "task-7-done" in f for f in flat), flat
    assert any("new-window -t silicorism-session -n task-9-pi -c /tmp/worktrees/y" in f
               for f in flat), flat
    assert any("send-keys" in f and "echo $? >" in f for f in flat), flat
    # stdout/stderr tee'd to a log file for the downstream artifact
    assert any("| tee " in f and "task-9.log" in f for f in flat), flat
    # log tail is read back as the artifact
    lp = log_path("test")
    open(lp, "w").write("line1\nCONTEXT.md written\n")
    assert read_log_tail(lp) == "line1\nCONTEXT.md written"
    assert read_log_tail("/no/such/log") == ""
    os.remove(lp)
    # exit capture reads the sentinel file
    os.makedirs(SENTINEL_DIR, exist_ok=True)
    open(sent, "w").write("0\n")
    assert wait_for_exit(sent, timeout=1) == 0
    os.remove(sent)
    assert wait_for_exit(sent, timeout=0.2) is None
    print("tmux_orchestrator OK")
