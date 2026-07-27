"""tmux supervisor: a live session with a dashboard window plus an `agents`
window where every running agent gets a tiled pane of its own.

The agent owns its pane's tty, so it renders its real TUI; tmux mirrors the
pane into a per-task log. Pure stdlib — every tmux action is a subprocess call,
so the command strings are unit-testable without a running server.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shlex
import subprocess
import tempfile
import time

SESSION = "silicorism-session"
# Per-user: the launch scripts here are executed by the user's shell, so a
# shared /tmp/silicorism-sentinels would let anyone pre-create it and choose
# what runs.
SENTINEL_DIR = os.path.join(tempfile.gettempdir(),
                            f"silicorism-sentinels-{os.getuid()}")
CONFIG_DIR = os.path.expanduser("~/.config/silicorism")
LOG_DIR = os.path.join(CONFIG_DIR, "logs")


def log_path(task_id) -> str:
    """Path to a task's captured stdout/stderr log (dir created on demand)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    return os.path.join(LOG_DIR, f"task-{task_id}.log")


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|\r")


def read_log_tail(path: str, max_chars: int = 2000) -> str:
    """Tail of a captured log, escape codes stripped (empty if none).

    tmux records what the pane *drew*, so the raw file is a repaint stream.
    Handing that to the next agent as context would spend its tokens on colour
    codes.
    """
    try:
        data = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    return _ANSI.sub("", data)[-max_chars:].strip()


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


def mark_window_done(name: str, *, session: str = SESSION,
                     failed: bool = False) -> None:
    """Rename a finished task's window; the caller knows its actual name."""
    suffix = "failed" if failed else "done"
    _tmux("rename-window", "-t", _window_target(session, name), f"{name}-{suffix}")


def trim_log(path: str, max_bytes: int = 262_144) -> None:
    """Keep only the tail of a pane log.

    pipe-pane records every repaint, so a half-minute agent can leave megabytes
    behind; the tail is the only part anything reads.
    """
    try:
        if os.path.getsize(path) <= max_bytes:
            return
        with open(path, "rb") as fh:
            fh.seek(-max_bytes, os.SEEK_END)
            tail = fh.read()
        with open(path, "wb") as fh:
            fh.write(tail)
    except OSError:
        pass


def kill_session(session: str = SESSION) -> None:
    _tmux("kill-session", "-t", session)


# --- native CLI execution in a live pane -----------------------------------

def _sentinel_dir() -> str:
    """Create the sentinel/script dir private to this user (0700)."""
    os.makedirs(SENTINEL_DIR, mode=0o700, exist_ok=True)
    return SENTINEL_DIR


def sentinel_path(task_id) -> str:
    return os.path.join(_sentinel_dir(), f"task-{task_id}.exit")


def _launch_script(task_id, command: str, sentinel: str) -> str:
    """Write the command to a script and return the `sh <path>` that runs it.

    Two things this indirection buys:

    send-keys replays a string as keystrokes, so a newline inside the command
    is an Enter — an agent prompt spanning lines left the shell stuck in quote
    continuation and the TUI never started. `sh <script>` is one line whatever
    the prompt contains.

    Nothing is piped. Piping the agent through `tee` made its stdout a pipe,
    so pi dropped its TUI and printed plain text: the panes showed output
    instead of a live agent. Logging is tmux's job now (see _log_pane).
    The sentinel is written atomically via mv.
    """
    path = os.path.join(_sentinel_dir(), f"task-{task_id}.sh")
    tmp = shlex.quote(sentinel + ".tmp")
    fin = shlex.quote(sentinel)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"{command}\necho $? > {tmp}\nmv {tmp} {fin}\n")
    return f"sh {shlex.quote(path)}"


def _log_pane(target: str, logfile: str) -> None:
    """Mirror everything the pane displays into <logfile>, leaving it a tty."""
    _tmux("pipe-pane", "-o", "-t", target, f"cat >> {shlex.quote(logfile)}")


def run_task_in_pane(task_id, task_type: str, cwd: str, command: str,
                     sentinel: str, *, session: str = SESSION,
                     logfile: str | None = None) -> str:
    """Open task-<id>-<type> at <cwd> and run <command> live, capturing exit.

    The agent owns the pane's tty; tmux mirrors what it draws into <logfile>
    so the worker still has a rich artifact for downstream tasks. The exit
    code is written atomically to <sentinel> for polling; remain-on-exit keeps
    the pane open for post-mortem inspection. Returns the window name.
    """
    name = f"task-{task_id}-{task_type}"
    if os.path.exists(sentinel):
        os.remove(sentinel)
    if logfile is None:
        logfile = log_path(task_id)
    _tmux("new-window", "-t", session, "-n", name, "-c", cwd)
    target = _window_target(session, name)
    _tmux("set-option", "-t", target, "remain-on-exit", "on")
    _log_pane(target, logfile)
    _tmux("send-keys", "-t", target,
          _launch_script(task_id, command, sentinel), "Enter")
    return name


# --- agents grid ------------------------------------------------------------

GRID_WINDOW = "agents"
try:
    GRID_MAX = int(os.environ.get("SILICORISM_GRID_MAX") or 4)
except ValueError:  # a typo in an env var must not break every import
    GRID_MAX = 4
RUNNING, DONE, FAILED = "RUNNING", "DONE", "FAILED"
_MARKERS = (RUNNING, DONE, FAILED)
# Pane border doubles as the label bar: "<agent-id> <status>". The label lives
# in a tmux user option, not pane_title — pi retitles its own pane, which would
# wipe the agent id out of the border seconds after the agent starts.
LABEL_OPT = "@silicorism_label"
PANE_FORMAT = f"#[align=left] #{{{LABEL_OPT}}} "
# Exact grid window names only: "agents" or "agents-<N>" — never a user's own
# "agents-notes" or similar, which would otherwise get agent panes spilled into it.
_GRID_RE = re.compile(rf"^{re.escape(GRID_WINDOW)}(?:-(\d+))?$")


@contextlib.contextmanager
def _grid_lock():
    """Cross-process lock around pane placement (flock, released on close)."""
    fh = open(os.path.join(_sentinel_dir(), "grid.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fh.close()


def _grid_windows(session: str) -> list[str]:
    """Existing agents* window names, oldest first."""
    r = _tmux("list-windows", "-t", session, "-F", "#{window_name}")
    if r.returncode != 0:
        return []
    # splitlines, not split: a user window named "my agents" would otherwise
    # yield a phantom "agents" that passes the exact-match test.
    return [n for n in r.stdout.splitlines() if _GRID_RE.match(n)]


def _pane_count(session: str, window: str) -> int:
    r = _tmux("list-panes", "-t", f"{session}:{window}", "-F", "#{pane_id}")
    return len(r.stdout.split()) if r.returncode == 0 else 0


def _next_grid_window(session: str) -> tuple[str, bool]:
    """(window name, needs_creating) for the next agent pane."""
    wins = _grid_windows(session)
    for w in wins:
        if _pane_count(session, w) < GRID_MAX:
            return w, False
    if not wins:
        return GRID_WINDOW, True
    # Next suffix is one past the highest existing spill number, not the
    # window count — a closed agents-2 must not cause a duplicate agents-3.
    nums = [int(m.group(1)) for w in wins if (m := _GRID_RE.match(w)) and m.group(1)]
    return f"{GRID_WINDOW}-{max(nums, default=1) + 1}", True


def grid_pane(task_id, label: str, cwd: str, command: str, sentinel: str, *,
              session: str = SESSION, logfile: str | None = None) -> tuple[str, str]:
    """Run <command> in a tiled pane of a shared agents window.

    Panes are capped at GRID_MAX per window so a pi TUI stays readable; the
    overflow opens agents-2, agents-3, ... Returns (window, pane_id). The pane
    id (%N) is stable for the pane's life, unlike the shifting w.i index.
    Raises RuntimeError if tmux does not hand back a pane id.
    """
    if os.path.exists(sentinel):
        os.remove(sentinel)
    if logfile is None:
        logfile = log_path(task_id)
    ensure_session(session)
    # Workers are separate processes racing to place a pane. Choosing a window
    # and creating it must be one step, or four workers each find "no agents
    # window" and create four of them (tmux allows duplicate window names).
    with _grid_lock():
        window, is_new = _next_grid_window(session)
        if is_new:
            r = _tmux("new-window", "-t", session, "-n", window, "-c", cwd,
                      "-P", "-F", "#{pane_id}")
        else:
            r = _tmux("split-window", "-t", f"{session}:{window}", "-c", cwd,
                      "-P", "-F", "#{pane_id}")
    pane = r.stdout.strip()
    if r.returncode != 0 or not pane:
        raise RuntimeError(f"tmux pane: {r.stderr.strip()[:200] or 'no pane id'}")
    target = f"{session}:{window}"
    _tmux("set-option", "-w", "-t", target, "pane-border-status", "top")
    _tmux("set-option", "-w", "-t", target, "pane-border-format", PANE_FORMAT)
    _tmux("set-option", "-p", "-t", pane, "remain-on-exit", "on")
    _tmux("set-option", "-p", "-t", pane, LABEL_OPT, f"{label} {RUNNING}")
    _tmux("select-layout", "-t", target, "tiled")
    _log_pane(pane, logfile)
    _tmux("send-keys", "-t", pane,
          _launch_script(task_id, command, sentinel), "Enter")
    return window, pane


def mark_pane_done(pane_id: str, *, failed: bool = False) -> None:
    """Swap a pane's status marker to DONE/FAILED, keeping its label."""
    r = _tmux("display-message", "-p", "-t", pane_id, f"#{{{LABEL_OPT}}}")
    title = r.stdout.strip() if r.returncode == 0 else ""
    base = title.rsplit(" ", 1)[0] if title.endswith(_MARKERS) else title
    _tmux("set-option", "-p", "-t", pane_id, LABEL_OPT,
          f"{base} {FAILED if failed else DONE}".strip())


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
          f"silicorism dashboard --db {shlex.quote(db_path)}", "Enter")
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
    cmd = f"silicorism dashboard --db {shlex.quote(db_path)}"
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
    # the command goes into a script; send-keys only types `sh <path>`.
    script = os.path.join(SENTINEL_DIR, "task-9.sh")
    assert any("send-keys" in f and script in f for f in flat), flat
    body = open(script, encoding="utf-8").read()
    assert "echo $? >" in body and "| tee " not in body, body
    # logging is tmux's job, so the agent keeps a tty and renders its TUI
    assert any("pipe-pane" in f and "task-9.log" in f for f in flat), flat
    os.remove(script)
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
