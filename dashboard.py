"""Curses dashboard: DAG tree, per-node model and timing, P2P feed.

The frame is built by a pure function over task rows, so the whole layout is
testable without a terminal; curses only paints the strings it returns.
Nothing here writes to the DB or shells out to tmux — a monitor must never be
able to take the session down with it.
"""

from __future__ import annotations

import curses
import json
import shutil
import time
from datetime import datetime, timezone

import db
import handlers

_TS = "%Y-%m-%dT%H:%M:%S.%fZ"
_STATUS = {"completed": "done", "processing": "run", "failed": "FAIL",
           "pending": "wait"}
# Full model id -> the friendly name, so the tree column stays narrow.
# setdefault, not a comprehension: two aliases share one id (mimo-2.5 /
# mimo-v2.5) and the first one listed is the name we want to show.
_SHORT: dict[str, str] = {}
for _name, _id in handlers.MODEL_ALIASES.items():
    _SHORT.setdefault(_id, _name)


def _parse_ts(value):
    try:
        return datetime.strptime(value, _TS).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def short_model(payload) -> str:
    """Friendly model name from a task payload; '-' when absent or unparseable."""
    try:
        data = json.loads(payload or "{}")
    except (json.JSONDecodeError, ValueError, TypeError):
        return "-"
    if not isinstance(data, dict):
        return "-"
    model = data.get("model") or data.get("test_command")
    if not model:
        return "-"
    return _SHORT.get(model, model).split("/")[-1][:18]


def node_name(row) -> str:
    """The agent id if the payload names one, else the task type.

    Four rows all reading `pi` tell you nothing about which node is which.
    """
    try:
        data = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, ValueError, TypeError):
        return str(row["task_type"])
    if isinstance(data, dict) and data.get("agent_id"):
        return str(data["agent_id"])
    return str(row["task_type"])


def elapsed(row, now) -> str:
    """m'ss for a task that has started; '-' for one that has not."""
    start = _parse_ts(row["started_at"])
    if start is None:
        return "-"
    end = now if row["status"] == "processing" else _parse_ts(row["updated_at"])
    if end is None:
        end = now
    secs = max(int((end - start).total_seconds()), 0)
    return f"{secs // 60}m{secs % 60:02d}"


def _age(value, now) -> str:
    """m'ss since a timestamp; '-' when it cannot be read."""
    ts = _parse_ts(value)
    if ts is None:
        return "-"
    secs = max(int((now - ts).total_seconds()), 0)
    return f"{secs // 60}m{secs % 60:02d}"


def idle(row, now, *, threshold=60) -> str:
    """'idle Nm' for a running task whose files stopped changing, else ''.

    A stalled agent and a working one look identical without this: both sit in
    `processing` with a growing elapsed time. The worker stamps
    last_progress_at whenever the task's tree changes, so the gap between that
    and now is the only visible difference.
    """
    if row["status"] != "processing":
        return ""
    ts = _parse_ts(row["last_progress_at"])
    if ts is None:
        return ""
    secs = int((now - ts).total_seconds())
    return f"idle {secs // 60}m{secs % 60:02d}" if secs >= threshold else ""


def worker_line(hb, now, *, dead_after=90) -> str:
    """One heartbeat row: agent, state, its task, and how stale it is.

    DEAD is the point of the section. A worker whose process died stops
    beating, and until now the dashboard showed its task as merrily running.
    """
    age = _age(hb["last_seen"], now)
    ts = _parse_ts(hb["last_seen"])
    stale = ts is not None and (now - ts).total_seconds() >= dead_after
    mark = f"DEAD {age}" if stale else f"{age} ago"
    return (f"  {hb['agent_id']:<14} {hb['status']:<8} "
            f"task {str(hb['current_task_id'] or '-'):<5} {mark}")


# Deeper nesting than this reads the same but costs screen width.
_MAX_DEPTH = 4


def _children(tasks) -> dict:
    """{parent_id: [rows]} keyed on each task's FIRST dependency.

    First-dep parenting is what makes fan-out siblings render at equal depth:
    two builders that both depend on the scout are both its children.
    """
    kids: dict = {}
    for row in tasks:
        try:
            deps = json.loads(row["depends_on"] or "[]")
        except (json.JSONDecodeError, ValueError, TypeError):
            deps = []
        if not isinstance(deps, list):  # a scalar or object would blow up on [0]
            deps = []
        kids.setdefault(deps[0] if deps else None, []).append(row)
    return kids


def build_frame(tasks, messages, counts, *, width=100, now=None,
                workers=(), errors=(), label="") -> list[str]:
    """Render the whole dashboard as plain lines. Pure: no curses, no DB."""
    now = now or datetime.now(timezone.utc)
    head = f" silicorism {label}".rstrip()
    clock = f"{now:%H:%M:%S}  q quit  r redraw"
    lines = [f"{head}{' ' * max(width - len(head) - len(clock) - 1, 1)}{clock}",
             f" pending {counts['pending']}   running {counts['processing']}"
             f"   done {counts['completed']}   failed {counts['failed']}",
             "", " TASKS"]
    kids = _children(tasks)
    seen: set = set()

    flat: list[tuple] = []          # (indent, row) in display order
    # A submitted plan is a linear chain, where indentation says nothing and
    # only staircases every column to the right. Indent only when some node
    # really has more than one child.
    fanout = any(len(v) > 1 for v in kids.values())

    def walk(parent, depth):
        for row in kids.get(parent, []):
            if row["id"] in seen:  # a first-dep cycle would otherwise recurse
                continue
            seen.add(row["id"])
            shown = depth if fanout else 0
            flat.append(("  " * min(shown, _MAX_DEPTH) + ("+-" if shown else ""),
                         row))
            walk(row["id"], depth + 1)

    walk(None, 0)
    # Rows whose first dependency is missing (or cyclic) are unreachable from
    # the root; list them rather than let the tree silently swallow them.
    orphans = [r for r in tasks if r["id"] not in seen]

    def emit(pairs, gutter):
        # One gutter width for every row, sized to the deepest one actually
        # drawn: a plan is a linear chain, so per-row indentation staircased
        # the status/model/timing columns off the right edge, and a fixed
        # gutter would leave a flat DAG with an empty margin.
        for indent, row in pairs:
            status = _STATUS.get(row["status"], row["status"])
            lines.append(
                (f" {indent:<{gutter}}[{status:<4}] {node_name(row):<20} "
                 f"{short_model(row['payload']):<18} "
                 f"{elapsed(row, now):>6}  {row['pane_target'] or '':<12} "
                 f"{idle(row, now)}").rstrip())

    gutter = max([len(i) for i, _ in flat] + ([2] if orphans else [0]))
    emit(flat, gutter)
    if orphans:
        lines.append(" (orphaned)")
        emit([("+-", r) for r in orphans], gutter)
    if not tasks:
        lines.append(" (no tasks)")
    if workers:
        lines += ["", " WORKERS"]
        lines += [worker_line(h, now) for h in workers]
    if errors:
        lines += ["", " ERRORS"]
        for e in errors:
            body = (e["message"] or "").replace("\n", " ")
            lines.append(f"  #{e['task_id']} {body}")
    lines += ["", " P2P"]
    if not messages:
        lines.append("  (none)")
    for m in reversed(messages):  # recent_messages is newest-first; read down
        body = (m["content"] or "").replace("\n", " ")
        lines.append(f"  {m['sender_id']}->{m['recipient_id']}: {body}")
    return [ln[:width] for ln in lines]


def frame(conn, *, width=100, label="") -> list[str]:
    """Read the DB once and build a frame."""
    errors = [r for r in db.recent_logs(conn, 30) if r["level"] == "error"][:4]
    return build_frame(db.all_tasks(conn), db.recent_messages(conn, 6),
                       db.counts(conn), width=width, label=label,
                       workers=db.heartbeats(conn), errors=errors)


# The section headers that mark the end of the task tree. The tree is the only
# unbounded section, so it is the only one worth eliding.
_TAIL_HEADS = (" WORKERS", " ERRORS", " P2P")


def _fit(lines: list[str], height: int) -> list[str]:
    """Elide the task tree, never the sections under it.

    The tree grows without limit while workers/errors/P2P are capped, so the
    tail gets at most half the screen and the tree keeps the rest.
    """
    if len(lines) <= height:
        return lines
    start = min((lines.index(h) for h in _TAIL_HEADS if h in lines),
                default=len(lines))
    tail = lines[start:][-max(height // 2, 1):]
    return lines[:max(height - len(tail) - 1, 1)] + [" ..."] + tail


# Line -> colour key. Checked in order, so a FAIL row wins over its indent.
_KEYS = (("[FAIL]", "fail"), ("DEAD ", "fail"), ("idle ", "fail"),
         ("[run ]", "run"), ("[done]", "done"), ("[wait]", "wait"))
_PAIRS = {"fail": 1, "run": 2, "done": 3, "wait": 4, "head": 5}


def line_key(line: str) -> str:
    """Colour key for a rendered line: '' means paint it plainly.

    Pure so the colour rules are testable without a terminal.
    """
    for token, key in _KEYS:
        if token in line:
            return key
    stripped = line.strip()
    if stripped and stripped == stripped.upper() and line.startswith(" ") \
            and not line.startswith("  "):
        return "head"
    return ""


def _init_colors() -> bool:
    """Set up the status colour pairs; False if the terminal has no colour."""
    try:
        curses.start_color()
        curses.use_default_colors()
    except curses.error:
        return False
    if not curses.has_colors():
        return False
    for name, fg in (("fail", curses.COLOR_RED), ("run", curses.COLOR_YELLOW),
                     ("done", curses.COLOR_GREEN), ("wait", curses.COLOR_BLUE),
                     ("head", curses.COLOR_CYAN)):
        curses.init_pair(_PAIRS[name], fg, -1)
    return True


def _attr(key: str, colour: bool) -> int:
    if not key:
        return curses.A_NORMAL
    pair = curses.color_pair(_PAIRS[key]) if colour else 0
    return pair | (curses.A_BOLD if key == "head" else 0)


def _draw(stdscr, conn, label: str, colour: bool) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    # height - 1: writing the bottom-right cell raises curses.error.
    for y, line in enumerate(_fit(frame(conn, width=width - 1, label=label),
                                 height - 1)):
        stdscr.addstr(y, 0, line, _attr(line_key(line), colour))
    stdscr.refresh()


def _loop(stdscr, conn, interval: float, label: str) -> None:
    curses.curs_set(0)
    colour = _init_colors()
    # A blocking getch with a timeout is the redraw clock: no 20Hz spin loop,
    # and a keypress repaints immediately instead of up to `interval` later.
    stdscr.timeout(max(int(interval * 1000), 50))
    while True:
        try:
            _draw(stdscr, conn, label, colour)
        except curses.error:
            pass  # a resize mid-paint must not take the monitor down
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            return


def run(conn, interval: float = 1.0, *, label: str = "") -> None:
    """Curses dashboard; falls back to plain printing on a dumb terminal."""
    try:
        curses.wrapper(_loop, conn, interval, label)
    except (curses.error, RuntimeError):
        try:
            while True:  # last resort: the old reprint loop
                width = shutil.get_terminal_size((100, 24)).columns
                print("\033[2J\033[H"
                      + "\n".join(frame(conn, width=width, label=label)),
                      flush=True)
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
    except KeyboardInterrupt:
        pass
