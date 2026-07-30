"""Curses dashboard: run progress, DAG state, worker liveness, errors, P2P.

Every frame is built by pure functions into `Line(kind, spans)` values, so the
layout, the colours and the fitting are all testable without a terminal;
curses only paints what they return. Nothing here writes to the DB or shells
out to tmux — a monitor must never be able to take the session down with it.
"""

from __future__ import annotations

import curses
import json
import locale
import os
import shutil
import time
from datetime import datetime, timezone
from typing import NamedTuple

import db
import handlers


class Line(NamedTuple):
    """One row: `kind` decides what may be dropped when it does not fit,
    `spans` are (text, colour key) pairs painted left to right."""

    kind: str
    spans: list


_TS = "%Y-%m-%dT%H:%M:%S.%fZ"
# Full model id -> the friendly name, so the model column stays narrow.
# setdefault, not a comprehension: two aliases share one id (mimo-2.5 /
# mimo-v2.5) and the first one listed is the name we want to show.
_SHORT: dict[str, str] = {}
for _name, _id in handlers.MODEL_ALIASES.items():
    _SHORT.setdefault(_id, _name)


# --- glyphs -----------------------------------------------------------------

# Every unicode glyph here is one column wide, so span offsets stay equal to
# character counts and the columns line up. The spinner is half-blocks rather
# than the usual braille dots: at one cell those are too faint to read as
# motion, which is the whole point of having them.
GLYPHS_UNICODE = {
    "done": "✓", "fail": "✗", "wait": "·", "spin": "▌▀▐▄",
    "alive": "▪", "dead": "▫", "rule": "─", "bar": "█", "bar_empty": "░",
    "bar_l": "▏", "bar_r": "▕", "arrow": "→", "more": "…",
}
GLYPHS_ASCII = {
    "done": "+", "fail": "x", "wait": ".", "spin": "|/-\\",
    "alive": "*", "dead": "!", "rule": "-", "bar": "#", "bar_empty": ".",
    "bar_l": "[", "bar_r": "]", "arrow": "->", "more": "..",
}


def glyphs(*, unicode_ok: bool | None = None) -> dict:
    """Glyph set for this terminal: unicode unless the locale says otherwise.

    SILICORISM_ASCII forces the plain set, for a console that draws the box
    characters as mojibake.
    """
    if unicode_ok is None:
        if os.environ.get("SILICORISM_ASCII"):
            unicode_ok = False
        else:
            enc = (locale.getpreferredencoding(False) or "").lower()
            unicode_ok = enc.replace("-", "") in ("utf8", "utf8mb4")
    return GLYPHS_UNICODE if unicode_ok else GLYPHS_ASCII


# --- formatting -------------------------------------------------------------

def _parse_ts(value):
    try:
        return datetime.strptime(value, _TS).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _hms(secs) -> str:
    """m'ss up to an hour, then h'mm — a node can legitimately run for hours."""
    secs = max(int(secs), 0)
    if secs >= 3600:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 60}m{secs % 60:02d}"


def _trunc(text: str, width: int, g: dict) -> str:
    """Cut to width, marking the cut so a clipped name is never mistaken."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    keep = max(width - len(g["more"]), 0)
    return text[:keep] + g["more"][:width]


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
    return str(_SHORT.get(model, model)).split("/")[-1][:18]


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
    """How long a task has been running, or ran for; '-' if it never started."""
    start = _parse_ts(row["started_at"])
    if start is None:
        return "-"
    end = now if row["status"] == "processing" else _parse_ts(row["updated_at"])
    if end is None:
        end = now
    return _hms((end - start).total_seconds())


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
    secs = (now - ts).total_seconds()
    return f"idle {_hms(secs)}" if secs >= threshold else ""


def run_time(tasks, now) -> str:
    """Wall time of the whole run: earliest start to now, or to the last update."""
    starts = [t for t in (_parse_ts(r["started_at"]) for r in tasks) if t]
    if not starts:
        return ""
    if any(r["status"] == "processing" for r in tasks):
        end = now
    else:
        ends = [t for t in (_parse_ts(r["updated_at"]) for r in tasks) if t]
        end = max(ends) if ends else now
    return _hms((end - min(starts)).total_seconds())


# --- span helpers -----------------------------------------------------------

def span_width(spans) -> int:
    return sum(len(t) for t, _ in spans)


def flatten(lines) -> list[str]:
    """Frame as plain strings — what the no-curses fallback prints."""
    return ["".join(t for t, _ in ln.spans) for ln in lines]


def _row(left, right, width):
    """`left` spans, then `right` spans pushed flush against `width`."""
    gap = max(width - span_width(left) - span_width(right), 1)
    return list(left) + [(" " * gap, "")] + list(right)


def _clip(line: Line, width: int) -> Line:
    """Trim a line's spans to `width` columns, dropping what overflows."""
    out, used = [], 0
    for text, key in line.spans:
        if used >= width:
            break
        text = text[:width - used]
        if text:
            out.append((text, key))
            used += len(text)
    return Line(line.kind, out)


# --- sections ---------------------------------------------------------------

_BAR_ORDER = (("completed", "done"), ("failed", "fail"),
              ("processing", "run"), ("pending", "wait"))
# In the bar the pending stretch is absence, so it is drawn as unfilled grey
# rather than a fourth colour competing with the three that mean something.
_BAR_KEY = {"wait": "dim"}


def _allocate(values, total: int) -> list[int]:
    """Split `total` cells across `values` proportionally, largest remainder.

    Every non-zero value gets at least one cell while there is room: one
    failure in a hundred nodes must still show up as a red cell.
    """
    live = sum(values)
    if live <= 0 or total <= 0:
        return [0] * len(values)
    exact = [v * total / live for v in values]
    cells = [min(int(e), total) if v else 0 for e, v in zip(exact, values)]
    cells = [max(c, 1) if v else 0 for c, v in zip(cells, values)]
    # Hand out or claw back the rounding slack, biggest fraction first.
    order = sorted(range(len(values)), key=lambda i: exact[i] - int(exact[i]),
                   reverse=True)
    while sum(cells) < total:
        for i in order:
            if sum(cells) >= total:
                break
            if values[i]:
                cells[i] += 1
    while sum(cells) > total:
        for i in reversed(order):
            if sum(cells) <= total:
                break
            if cells[i] > 1:
                cells[i] -= 1
        else:
            break  # every bar is down to its single reserved cell
    return cells


def progress_bar(counts, width: int, g: dict) -> list:
    """A stacked done/failed/running/pending bar, `width` columns including caps."""
    inner = max(width - 2, 0)
    spans = [(g["bar_l"], "dim")]
    values = [counts.get(name, 0) for name, _ in _BAR_ORDER]
    if sum(values) <= 0 or inner == 0:
        spans.append((g["bar_empty"] * inner, "dim"))
    else:
        for (_, key), n in zip(_BAR_ORDER, _allocate(values, inner)):
            if n:
                spans.append(((g["bar_empty"] if key == "wait" else g["bar"]) * n,
                              _BAR_KEY.get(key, key)))
    spans.append((g["bar_r"], "dim"))
    return spans


def _header(counts, tasks, *, width, now, label, g) -> list[Line]:
    title = [(" silicorism", "accent")]
    if label:
        title.append((f"  {label}", "dim"))
    clock = []
    rt = run_time(tasks, now)
    if rt:
        clock.append((f"run {rt}   ", "dim"))
    clock.append((f"{now:%H:%M:%S} ", "dim"))

    total = sum(counts.get(s, 0) for s in db.STATUSES)
    tally = [(f" {total} nodes", "")]
    for name, key in _BAR_ORDER:
        if counts.get(name):
            tally.append((f"   {counts[name]} {key}", key))
    bar_w = max(min(width - span_width(tally) - 4, 40), 8)
    return [Line("header", _row(title, clock, width)),
            Line("header", [(" ", "")] + progress_bar(counts, bar_w, g) + tally)]


def _section(name: str, width: int, g: dict) -> Line:
    """A section title followed by a rule out to the right edge."""
    head = f" {name} " if name else " "
    rule = g["rule"] * max(width - len(head) - 1, 0)
    return Line("section", [(head, "head"), (rule, "dim")])


def _deps(tasks) -> dict:
    """{task id: [prerequisite ids present in this set]}, garbage tolerated."""
    ids = {r["id"] for r in tasks}
    out = {}
    for row in tasks:
        try:
            deps = json.loads(row["depends_on"] or "[]")
        except (json.JSONDecodeError, ValueError, TypeError):
            deps = []
        if not isinstance(deps, list):   # a scalar or an object, not a dep list
            deps = []
        out[row["id"]] = [d for d in deps if d in ids]
    return out


def _depths(deps: dict) -> dict:
    """Longest-path depth per task — its wave in the pipeline.

    Relaxation rather than recursion: a thousand-node chain must not blow the
    stack inside a monitor, and the pass count caps the work if the graph has
    a cycle (its members settle at whatever depth they were entered from).
    """
    depth = dict.fromkeys(deps, 0)
    for _ in range(len(deps)):
        changed = False
        for tid, parents in deps.items():
            d = max((depth[p] + 1 for p in parents), default=0)
            if d > depth[tid]:
                depth[tid], changed = d, True
        if not changed:
            break
    return depth


def _order(tasks) -> list:
    """Tasks in execution order: dependency depth, then id.

    Not a tree. These DAGs fan out from a scout and fan back into a fixer, and
    parenting each node under its FIRST dependency drew the verify gate above
    the builders it was waiting for. Depth order is the order they actually run
    in, and equal-depth nodes land next to each other, which is what fan-out
    looks like.
    """
    depth = _depths(_deps(tasks))
    return sorted(tasks, key=lambda r: (depth[r["id"]], r["id"]))


_GLYPH_KEY = {"completed": "done", "failed": "fail", "processing": "run",
              "pending": "wait"}


def _status_glyph(row, g: dict, tick: int) -> tuple[str, str]:
    """(glyph, colour key) for a task's status.

    Running nodes spin, which is the only proof the dashboard itself is still
    alive: a frozen monitor and an idle queue looked identical.
    """
    key = _GLYPH_KEY.get(row["status"], "wait")
    if row["status"] == "processing":
        frames = g["spin"]
        return frames[tick % len(frames)], "run"
    return g[key], key


def _task_lines(tasks, now, *, g, tick) -> list[Line]:
    """One line per task in execution order, columns sized to what is in them."""
    if not tasks:
        return [Line("empty", [("  (no tasks yet)", "dim")])]
    rows = _order(tasks)
    idw = max(len(str(r["id"])) for r in rows)
    namew = min(max(len(node_name(r)) for r in rows), 26)
    modelw = min(max(len(short_model(r["payload"])) for r in rows), 18)
    panew = min(max(len(r["pane_target"] or "") for r in rows), 12)

    out = []
    for row in rows:
        glyph, key = _status_glyph(row, g, tick)
        # A finished node is dimmed whole, so the eye lands on what is live.
        body = "dim" if row["status"] == "completed" else ""
        spans = [(" ", ""), (glyph, key),
                 (f"  #{str(row['id']):<{idw}} ", "dim"),
                 (f"{_trunc(node_name(row), namew, g):<{namew}} ",
                  "run" if row["status"] == "processing" else body),
                 (f"{_trunc(short_model(row['payload']), modelw, g):<{modelw}} ",
                  "dim"),
                 (f"{elapsed(row, now):>7} ", body)]
        if panew:
            spans.append((f"  {(row['pane_target'] or ''):<{panew}}", "dim"))
        stall = idle(row, now)
        if stall:
            spans.append((f"  {stall}", "fail"))
        retries = row["retry_count"] if "retry_count" in row.keys() else 0
        if retries:
            spans.append((f"  retry {retries}", "fail"))
        out.append(Line(f"task:{row['status']}", spans))
    return out


def _worker_spans(hb, now, g, *, dead_after=90) -> list:
    """One heartbeat row: agent, state, its task, and how stale it is.

    DEAD is the point of the section. A worker whose process died stops
    beating, and until now the dashboard showed its task as merrily running.
    """
    ts = _parse_ts(hb["last_seen"])
    age = _hms((now - ts).total_seconds()) if ts else "-"
    dead = ts is not None and (now - ts).total_seconds() >= dead_after
    task = f"#{hb['current_task_id']}" if hb["current_task_id"] else "-"
    return [("  ", ""), (g["dead"] if dead else g["alive"],
                         "fail" if dead else "done"),
            (f" {str(hb['agent_id']):<14} ", ""),
            (f"{str(hb['status']):<6} ", "dim"), (f"{task:<5} ", "dim"),
            (f"DEAD {age}", "fail") if dead else (f"{age} ago", "dim")]


def build_frame(tasks, messages, counts, *, width=100, now=None, workers=(),
                errors=(), label="", g=None, tick=0) -> list[Line]:
    """The whole dashboard as tagged, coloured lines. Pure: no curses, no DB."""
    g = g or glyphs()
    now = now or datetime.now(timezone.utc)
    out = _header(counts, tasks, width=width, now=now, label=label, g=g)
    out.append(_section("TASKS", width, g))
    out += _task_lines(tasks, now, g=g, tick=tick)
    if workers:
        out.append(_section("WORKERS", width, g))
        out += [Line("worker", _worker_spans(h, now, g)) for h in workers]
    if errors:
        out.append(_section("ERRORS", width, g))
        for e in errors:
            body = (e["message"] or "").replace("\n", " ")
            out.append(Line("error", [(f"  #{e['task_id']} ", "dim"),
                                      (_trunc(body, width - 8, g), "fail")]))
    if messages:
        out.append(_section("P2P", width, g))
        for m in reversed(messages):  # newest-first from the DB; read downwards
            body = (m["content"] or "").replace("\n", " ")
            who = f"  {m['sender_id']} {g['arrow']}  {m['recipient_id']}   "
            out.append(Line("msg", [(who, "accent"),
                                    (_trunc(body, width - len(who) - 1, g), "")]))
    out.append(_section("", width, g))
    out.append(Line("footer", [("  q", "head"), (" quit   ", "dim"),
                               ("r", "head"), (" redraw", "dim")]))
    return [_clip(ln, width) for ln in out]


def _snapshot(conn) -> tuple:
    """One read of everything a frame needs, so the paint loop can outpace it."""
    return (db.all_tasks(conn), db.recent_messages(conn, 6), db.counts(conn),
            db.heartbeats(conn),
            [r for r in db.recent_logs(conn, 30) if r["level"] == "error"][:4])


def frame(conn, *, width=100, label="", g=None, tick=0) -> list[Line]:
    """Read the DB once and build a frame."""
    tasks, messages, counts, workers, errors = _snapshot(conn)
    return build_frame(tasks, messages, counts, width=width, label=label,
                       workers=workers, errors=errors, g=g, tick=tick)


# --- fitting ----------------------------------------------------------------

def _collapse_done(lines: list[Line], g: dict) -> list[Line]:
    """Fold runs of finished tasks into one line: they are not the news."""
    out: list[Line] = []
    run: list[Line] = []

    def flush():
        if len(run) > 1:
            out.append(Line("task:collapsed",
                            [("  ", ""), (g["done"], "done"),
                             (f" {len(run)} done", "dim")]))
        else:
            out.extend(run)
        run.clear()

    for line in lines:
        if line.kind == "task:completed":
            run.append(line)
            continue
        flush()
        out.append(line)
    flush()
    return out


def fit(lines: list[Line], height: int, g: dict | None = None) -> list[Line]:
    """Shrink a frame to `height` rows, giving up task rows before anything else.

    The task list is the only unbounded section, and inside it a finished node
    is the least interesting thing on screen, so completed runs collapse first
    and the oldest survivors go next. Workers, errors and the footer stay:
    they are what tells you whether the run is in trouble.
    """
    if len(lines) <= height:
        return lines
    g = g or glyphs()
    lines = _collapse_done(lines, g)
    if len(lines) <= height:
        return lines
    idx = [i for i, ln in enumerate(lines) if ln.kind.startswith("task")]
    if len(idx) > 1:
        drop = min(len(lines) - height + 1, len(idx) - 1)
        more = Line("task:more", [("  ", ""),
                                  (f"{g['more']} {drop} earlier nodes", "dim")])
        lines = lines[:idx[0]] + [more] + lines[idx[drop]:]
    return lines[:height]


# --- curses -----------------------------------------------------------------

_PAIRS = {"fail": 1, "run": 2, "done": 3, "wait": 4, "head": 5, "accent": 6}
_FRAME_MS = 125          # the spinner clock, independent of the DB poll


def _init_colors() -> bool:
    """Set up the colour pairs; False if the terminal has none."""
    try:
        curses.start_color()
        curses.use_default_colors()
    except curses.error:
        return False
    if not curses.has_colors():
        return False
    for name, fg in (("fail", curses.COLOR_RED), ("run", curses.COLOR_YELLOW),
                     ("done", curses.COLOR_GREEN), ("wait", curses.COLOR_BLUE),
                     ("head", curses.COLOR_CYAN),
                     ("accent", curses.COLOR_MAGENTA)):
        curses.init_pair(_PAIRS[name], fg, -1)
    return True


def attr(key: str, colour: bool) -> int:
    """curses attribute for a span's colour key."""
    if key == "dim":
        return curses.A_DIM
    bold = curses.A_BOLD if key in ("head", "accent") else 0
    if not key or not colour:
        return bold or curses.A_NORMAL
    return curses.color_pair(_PAIRS.get(key, 0)) | bold


def _paint(stdscr, lines, colour: bool) -> None:
    stdscr.erase()
    for y, line in enumerate(lines):
        x = 0
        for text, key in line.spans:
            if not text:
                continue
            stdscr.addstr(y, x, text, attr(key, colour))
            x += len(text)
    stdscr.refresh()


def _loop(stdscr, conn, interval: float, label: str) -> None:
    curses.curs_set(0)
    colour = _init_colors()
    g = glyphs()
    # The paint clock runs faster than the DB poll: the spinner has to move to
    # show the monitor is alive, but re-reading the queue eight times a second
    # would be pure noise.
    stdscr.timeout(_FRAME_MS)
    snap, due, tick = None, 0.0, 0
    while True:
        if snap is None or time.monotonic() >= due:
            try:
                snap = _snapshot(conn)
            except Exception:
                pass  # a mid-write read must not kill the monitor
            due = time.monotonic() + max(interval, 0.1)
        if snap is not None:
            height, width = stdscr.getmaxyx()
            try:
                _paint(stdscr, fit(build_frame(*snap[:3], width=width - 1,
                                               workers=snap[3], errors=snap[4],
                                               label=label, g=g, tick=tick),
                                   height - 1, g), colour)
            except curses.error:
                pass  # a resize mid-paint must not take the monitor down
        tick += 1
        if stdscr.getch() in (ord("q"), ord("Q")):
            return


def run(conn, interval: float = 1.0, *, label: str = "") -> None:
    """Curses dashboard; falls back to plain printing on a dumb terminal."""
    try:
        locale.setlocale(locale.LC_ALL, "")   # or curses draws the glyphs as ?
    except locale.Error:
        pass
    try:
        curses.wrapper(_loop, conn, interval, label)
    except (curses.error, RuntimeError):
        try:
            while True:  # last resort: the old reprint loop
                width = shutil.get_terminal_size((100, 24)).columns
                print("\033[2J\033[H"
                      + "\n".join(flatten(frame(conn, width=width, label=label))),
                      flush=True)
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
    except KeyboardInterrupt:
        pass
