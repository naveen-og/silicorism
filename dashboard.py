"""Curses dashboard: DAG tree, per-node model and timing, P2P feed.

The frame is built by a pure function over task rows, so the whole layout is
testable without a terminal; curses only paints the strings it returns.
Nothing here writes to the DB or shells out to tmux — a monitor must never be
able to take the session down with it.
"""

from __future__ import annotations

import curses
import json
import time
from datetime import datetime, timezone

import db
import handlers

_TS = "%Y-%m-%dT%H:%M:%S.%fZ"
_STATUS = {"completed": "done", "processing": "run", "failed": "FAIL",
           "pending": "wait"}
# Full model id -> the friendly name, so the tree column stays narrow.
_SHORT = {v: k for k, v in handlers.MODEL_ALIASES.items()}


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
        kids.setdefault(deps[0] if deps else None, []).append(row)
    return kids


def build_frame(tasks, messages, counts, *, width=100, now=None) -> list[str]:
    """Render the whole dashboard as plain lines. Pure: no curses, no DB."""
    now = now or datetime.now(timezone.utc)
    lines = [f"-- silicorism {'-' * max(width - 26, 4)} {now:%H:%M:%S} --",
             f" pending {counts['pending']}   running {counts['processing']}"
             f"   done {counts['completed']}   failed {counts['failed']}", ""]
    kids = _children(tasks)

    def walk(parent, depth):
        for row in kids.get(parent, []):
            prefix = "  " * depth + ("+-" if depth else "")
            status = _STATUS.get(row["status"], row["status"])
            lines.append(
                f" {prefix}[{status}] {row['task_type']:<16} "
                f"{short_model(row['payload']):<18} "
                f"{elapsed(row, now):>6}  {row['pane_target'] or ''}")
            walk(row["id"], depth + 1)

    walk(None, 0)
    if not tasks:
        lines.append(" (no tasks)")
    lines += ["", " P2P"]
    if not messages:
        lines.append("  (none)")
    for m in messages:
        body = (m["content"] or "").replace("\n", " ")
        lines.append(f"  {m['sender_id']}->{m['recipient_id']}: {body}")
    return [ln[:width] for ln in lines]


def frame(conn, *, width=100) -> list[str]:
    """Read the DB once and build a frame."""
    return build_frame(db.all_tasks(conn), db.recent_messages(conn, 6),
                       db.counts(conn), width=width)


def _draw(stdscr, conn) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    for y, line in enumerate(frame(conn, width=width - 1)):
        if y >= height - 1:
            break
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
        while True:  # last resort: the old reprint loop
            print("\033[2J\033[H" + "\n".join(frame(conn)), flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
