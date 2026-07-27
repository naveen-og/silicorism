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


def build_frame(tasks, messages, counts, *, width=100, now=None) -> list[str]:
    """Render the whole dashboard as plain lines. Pure: no curses, no DB."""
    now = now or datetime.now(timezone.utc)
    lines = [f"-- silicorism {'-' * max(width - 26, 4)} {now:%H:%M:%S} --",
             f" pending {counts['pending']}   running {counts['processing']}"
             f"   done {counts['completed']}   failed {counts['failed']}", ""]
    kids = _children(tasks)
    seen: set = set()

    def row_line(row, depth):
        prefix = "  " * depth + ("+-" if depth else "")
        status = _STATUS.get(row["status"], row["status"])
        return (f" {prefix}[{status}] {node_name(row):<20} "
                f"{short_model(row['payload']):<18} "
                f"{elapsed(row, now):>6}  {row['pane_target'] or ''}")

    def walk(parent, depth):
        for row in kids.get(parent, []):
            if row["id"] in seen:  # a first-dep cycle would otherwise recurse
                continue
            seen.add(row["id"])
            lines.append(row_line(row, depth))
            walk(row["id"], depth + 1)

    walk(None, 0)
    # Rows whose first dependency is missing (or cyclic) are unreachable from
    # the root; list them rather than let the tree silently swallow them.
    orphans = [r for r in tasks if r["id"] not in seen]
    if orphans:
        lines.append(" (orphaned)")
        lines += [row_line(r, 1) for r in orphans]
    if not tasks:
        lines.append(" (no tasks)")
    lines += ["", " P2P"]
    if not messages:
        lines.append("  (none)")
    for m in reversed(messages):  # recent_messages is newest-first; read down
        body = (m["content"] or "").replace("\n", " ")
        lines.append(f"  {m['sender_id']}->{m['recipient_id']}: {body}")
    return [ln[:width] for ln in lines]


def frame(conn, *, width=100) -> list[str]:
    """Read the DB once and build a frame."""
    return build_frame(db.all_tasks(conn), db.recent_messages(conn, 6),
                       db.counts(conn), width=width)


def _fit(lines: list[str], height: int) -> list[str]:
    """Drop tree rows from the middle, never the P2P feed at the bottom."""
    if len(lines) <= height:
        return lines
    tail = len(lines) - lines.index(" P2P") if " P2P" in lines else 0
    head = max(height - tail - 1, 1)
    return lines[:head] + [" ..."] + lines[len(lines) - (height - head - 1):]


def _draw(stdscr, conn) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    # height - 1: writing the bottom-right cell raises curses.error.
    for y, line in enumerate(_fit(frame(conn, width=width - 1), height - 1)):
        stdscr.addstr(y, 0, line)
    stdscr.refresh()


def _loop(stdscr, conn, interval: float) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    while True:
        _draw(stdscr, conn)
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline:
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key in (ord("r"), curses.KEY_RESIZE):
                break
            time.sleep(0.05)


def run(conn, interval: float = 1.0) -> None:
    """Curses dashboard; falls back to plain printing on a dumb terminal."""
    try:
        curses.wrapper(_loop, conn, interval)
    except (curses.error, RuntimeError):
        try:
            while True:  # last resort: the old reprint loop
                width = shutil.get_terminal_size((100, 24)).columns
                print("\033[2J\033[H" + "\n".join(frame(conn, width=width)),
                      flush=True)
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
    except KeyboardInterrupt:
        pass
