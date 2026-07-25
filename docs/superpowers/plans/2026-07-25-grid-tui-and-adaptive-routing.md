# Grid TUI, Adaptive Routing, and Token Economy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every pi agent visible at once in a tiled tmux grid with a real curses dashboard, size the DAG to the task's complexity, and cut the orchestrator's Claude token spend by replacing its poll loop with a single blocking wait.

**Architecture:** Agents are placed into shared `agents*` tmux windows via `split-window` and `select-layout tiled` instead of one window each, addressed by stable `%pane_id`. A new `dashboard.py` renders a DAG tree from the `depends_on` column using stdlib `curses`, with a pure frame-builder function that is tested without a terminal. `build_pipeline` gains a `complexity` tier that selects one of three DAG shapes. A new `silicorism_wait` MCP tool blocks server-side until the queue settles, so the orchestrator spends one turn per DAG rather than one per poll.

**Tech Stack:** Python 3.10+, stdlib only (`sqlite3`, `curses`, `subprocess`, `json`, `argparse`). tmux 3.7b. pytest for tests. No new dependencies — the project has `dependencies = []` in `pyproject.toml` and must keep it.

## Global Constraints

- **Zero new dependencies.** `pyproject.toml` declares `dependencies = []`. Stdlib only.
- **Python >= 3.10** (`requires-python = ">=3.10"`).
- **No Claude model may appear in any execution path.** The escalation ladder and all tier defaults use bedrock OSS models only.
- **New single-file modules must be added to `py-modules` in `pyproject.toml`** — the project uses flat modules, not a package dir.
- **tmux is optional at runtime.** Every tmux failure must degrade to a working non-grid path, never fail a task.
- **Existing public behaviour stays.** `build_pipeline(conn, db_path, name, prompt)` with no `complexity` argument must still produce the current 6-task pipeline; `tests/test_mcp.py` and `tests/test_workflow.py` must pass unmodified except where a task explicitly says otherwise.
- Run the full suite with `.venv/bin/python -m pytest -q` from the repo root.

---

### Task 1: Fix timestamp format and add the two schema columns

`db.now()` is broken: Python's `%f` is microseconds, but SQLite's `%f` is `SS.SSS`. Today `created_at` (SQLite default) and `updated_at` (Python `db.now()`) hold different formats, so no duration can be computed. Task 4's dashboard depends on this being correct.

**Files:**
- Modify: `db.py:90-91` (`now`), `db.py:29-45` (`_SCHEMA`), `db.py:108-112` (`_MIGRATIONS`), `db.py:204-220` (`claim_task`)
- Test: `tests/test_schema.py` (create)

**Interfaces:**
- Consumes: nothing (first task).
- Produces:
  - `db.now() -> str` returning `"2026-07-25T12:58:25.512Z"` (millisecond precision, matches SQLite's `strftime('%Y-%m-%dT%H:%M:%fZ')`).
  - Columns `tasks.pane_target TEXT` and `tasks.started_at TEXT`.
  - `db.set_pane_target(conn, task_id: int, target: str) -> None`
  - `db.all_tasks(conn) -> list[sqlite3.Row]` ordered by `id`, used by Task 4.

- [ ] **Step 1: Write the failing test**

Create `tests/test_schema.py`:

```python
"""Timestamp format parity between Python and SQLite, plus additive migrations."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402

FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def test_python_and_sqlite_timestamps_share_one_format(tmp_path):
    dbp = str(tmp_path / "t.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "echo", "hi")
    created = conn.execute("SELECT created_at FROM tasks WHERE id=?",
                           (tid,)).fetchone()["created_at"]
    db.complete_task(conn, tid, "done")
    updated = conn.execute("SELECT updated_at FROM tasks WHERE id=?",
                           (tid,)).fetchone()["updated_at"]
    # Both parse with the same format string, and updated >= created.
    assert datetime.strptime(created, FMT) <= datetime.strptime(updated, FMT)
    conn.close()


def test_claim_stamps_started_at(tmp_path):
    dbp = str(tmp_path / "t.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "echo", "hi")
    assert conn.execute("SELECT started_at FROM tasks WHERE id=?",
                        (tid,)).fetchone()["started_at"] is None
    db.claim_task(conn, "w1")
    started = conn.execute("SELECT started_at FROM tasks WHERE id=?",
                           (tid,)).fetchone()["started_at"]
    assert started and datetime.strptime(started, FMT)
    conn.close()


def test_pane_target_roundtrip(tmp_path):
    dbp = str(tmp_path / "t.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", "{}")
    db.set_pane_target(conn, tid, "agents.%5")
    assert conn.execute("SELECT pane_target FROM tasks WHERE id=?",
                        (tid,)).fetchone()["pane_target"] == "agents.%5"
    conn.close()


def test_migration_adds_columns_to_an_old_db(tmp_path):
    """A DB created before these columns existed gains them and keeps its rows."""
    dbp = str(tmp_path / "old.db")
    old = sqlite3.connect(dbp)
    old.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "task_type TEXT NOT NULL, payload TEXT, "
                "status TEXT NOT NULL DEFAULT 'pending', agent_id TEXT, "
                "priority INTEGER NOT NULL DEFAULT 0, "
                "retry_count INTEGER NOT NULL DEFAULT 0, "
                "max_retries INTEGER NOT NULL DEFAULT 3, "
                "created_at TEXT NOT NULL DEFAULT "
                "(strftime('%Y-%m-%dT%H:%M:%fZ','now')), "
                "updated_at TEXT NOT NULL DEFAULT "
                "(strftime('%Y-%m-%dT%H:%M:%fZ','now')))")
    old.execute("INSERT INTO tasks (task_type, payload) VALUES ('echo','keep me')")
    old.commit()
    old.close()

    db.init_db(dbp)  # applies _migrate
    conn = db.connect(dbp)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert {"pane_target", "started_at", "depends_on", "worktree_path"} <= cols
    assert conn.execute("SELECT payload FROM tasks").fetchone()["payload"] == "keep me"
    conn.close()


def test_all_tasks_returns_rows_in_id_order(tmp_path):
    dbp = str(tmp_path / "t.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    a = db.add_task(conn, "echo", "a")
    b = db.add_task(conn, "echo", "b")
    assert [r["id"] for r in db.all_tasks(conn)] == [a, b]
    conn.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_schema.py -q`
Expected: FAIL — `test_python_and_sqlite_timestamps_share_one_format` raises `ValueError: time data '2026-07-25T12:58:510432Z' does not match format`, and the others fail with `no such column: started_at` / `AttributeError: module 'db' has no attribute 'set_pane_target'`.

- [ ] **Step 3: Fix `db.now()`**

Replace `db.py:90-91`:

```python
def now() -> str:
    """UTC timestamp matching SQLite's strftime('%Y-%m-%dT%H:%M:%fZ') exactly.

    Python's %f is microseconds; SQLite's is SS.SSS. Formatting seconds
    explicitly and trimming to milliseconds is what makes the two agree, so
    created_at (SQLite default) and updated_at (this function) are comparable.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
```

- [ ] **Step 4: Add the columns to the schema and the migration list**

In `db.py`'s `_SCHEMA`, add two columns to the `tasks` table, after `worktree_path`:

```sql
    worktree_path   TEXT,            -- dedicated git worktree for this task
    pane_target     TEXT,            -- tmux "<window>.<pane_id>" showing this task
    started_at      TEXT,            -- stamped when a worker claims the task
```

Extend `db.py:108-112`:

```python
_MIGRATIONS = (
    ("depends_on", "TEXT"),
    ("output_artifact", "TEXT"),
    ("worktree_path", "TEXT"),
    ("pane_target", "TEXT"),
    ("started_at", "TEXT"),
)
```

- [ ] **Step 5: Stamp `started_at` on claim and add the two accessors**

In `claim_task` (`db.py:214-218`), replace the UPDATE:

```python
        c.execute(
            "UPDATE tasks SET status='processing', agent_id=?, started_at=?, "
            "updated_at=? WHERE id=?",
            (agent_id, now(), now(), row["id"]),
        )
```

Add after `complete_task`:

```python
def set_pane_target(conn, task_id, target: str) -> None:
    """Record the tmux window.pane showing this task (display metadata only)."""
    with immediate(conn) as c:
        c.execute("UPDATE tasks SET pane_target=? WHERE id=?", (target, task_id))


def all_tasks(conn) -> list[sqlite3.Row]:
    """Every task in id order — the dashboard's read model."""
    return conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_schema.py -q`
Expected: PASS (5 passed)

- [ ] **Step 7: Run the whole suite for regressions**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 56 passed. If `tests/test_db.py` asserts on a timestamp shape, update that assertion to the new format — it is the only place allowed to change here.

- [ ] **Step 8: Commit**

```bash
git add db.py tests/test_schema.py
git commit -m "fix: make db.now() match SQLite's timestamp format; add pane_target/started_at

Python's %f is microseconds, SQLite's is SS.SSS, so created_at and
updated_at held two different formats and no duration could be computed."
```

---

### Task 2: Place agents into a tiled grid window

**Files:**
- Modify: `tmux_orchestrator.py` (add constants + `grid_pane` + `mark_pane_done` + helpers, after `run_task_in_pane` at line 113)
- Test: `tests/test_grid.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `tmux_orchestrator.GRID_WINDOW = "agents"`, `GRID_MAX` (int, env `SILICORISM_GRID_MAX`, default 4)
  - `grid_pane(task_id, label, cwd, command, sentinel, *, session=SESSION, logfile=None) -> tuple[str, str]` returning `(window_name, pane_id)`; raises `RuntimeError` if tmux gives no pane id.
  - `mark_pane_done(pane_id, *, failed=False) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_grid.py`:

```python
"""Grid placement asserts on constructed tmux commands — no tmux server needed."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tmux_orchestrator as tmux  # noqa: E402


class FakeTmux:
    """Records tmux argv and answers the queries grid_pane makes."""

    def __init__(self, existing=()):
        self.calls = []
        self.windows = list(existing)      # window name -> pane list
        self.panes = {w: [] for w in self.windows}
        self._next = 0

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        argv = cmd[1:]  # drop "tmux"
        out = ""
        if argv[0] == "has-session":
            return self._result(0, "")
        if argv[0] == "list-windows":
            out = "\n".join(self.windows)
        elif argv[0] == "list-panes":
            win = argv[2].split(":", 1)[1]
            out = "\n".join(self.panes.get(win, []))
        elif argv[0] in ("new-window", "split-window"):
            win = (argv[argv.index("-n") + 1] if "-n" in argv
                   else argv[argv.index("-t") + 1].split(":", 1)[1])
            self._next += 1
            pane = f"%{self._next}"
            self.panes.setdefault(win, []).append(pane)
            if win not in self.windows:
                self.windows.append(win)
            out = pane
        elif argv[0] == "display-message":
            out = "builder RUNNING"
        return self._result(0, out)

    @staticmethod
    def _result(code, out):
        class R:
            returncode = code
            stdout = out
            stderr = ""
        return R()

    def flat(self):
        return [" ".join(c) for c in self.calls]


def _place(fake, n):
    """Place n agents through grid_pane, returning [(window, pane), ...]."""
    placed = []
    with patch("subprocess.run", side_effect=fake):
        for i in range(n):
            placed.append(tmux.grid_pane(i, f"agent-{i}", "/tmp/wt",
                                         "pi 'go'", f"/tmp/sent-{i}"))
    return placed


def test_first_four_agents_share_one_window():
    fake = FakeTmux()
    placed = _place(fake, 4)
    assert [w for w, _ in placed] == ["agents"] * 4
    assert len({p for _, p in placed}) == 4  # distinct pane ids


def test_fifth_agent_spills_to_a_second_window():
    fake = FakeTmux()
    placed = _place(fake, 5)
    assert [w for w, _ in placed] == ["agents"] * 4 + ["agents-2"]


def test_grid_uses_tiled_layout_and_pane_id_capture():
    fake = FakeTmux()
    _place(fake, 2)
    flat = fake.flat()
    assert any("-P -F #{pane_id}" in f for f in flat), flat
    assert any("select-layout" in f and "tiled" in f for f in flat), flat
    assert any("split-window" in f for f in flat), flat


def test_pane_options_and_title_target_the_pane_id():
    fake = FakeTmux()
    (_, pane), = _place(fake, 1)
    flat = fake.flat()
    assert any(f"set-option -p -t {pane} remain-on-exit on" in f for f in flat), flat
    assert any(f"select-pane -t {pane} -T" in f for f in flat), flat


def test_command_wrapper_preserves_exit_code_and_tee():
    fake = FakeTmux()
    _place(fake, 1)
    sent = [f for f in fake.flat() if "send-keys" in f]
    assert sent and "echo $? >" in sent[0] and "| tee " in sent[0], sent


def test_mark_pane_done_swaps_the_status_marker():
    fake = FakeTmux()
    with patch("subprocess.run", side_effect=fake):
        tmux.mark_pane_done("%3", failed=True)
    title = [f for f in fake.flat() if "select-pane" in f][-1]
    assert "%3" in title and tmux.FAILED in title, title


def test_missing_pane_id_raises():
    fake = FakeTmux()

    def no_pane(cmd, **kw):
        r = fake(cmd, **kw)
        if cmd[1] in ("new-window", "split-window"):
            r.stdout = ""
        return r

    with patch("subprocess.run", side_effect=no_pane):
        try:
            tmux.grid_pane(1, "x", "/tmp", "pi 'go'", "/tmp/s")
        except RuntimeError:
            return
    raise AssertionError("expected RuntimeError when tmux returns no pane id")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_grid.py -q`
Expected: FAIL — `AttributeError: module 'tmux_orchestrator' has no attribute 'grid_pane'`

- [ ] **Step 3: Implement the grid**

Add to `tmux_orchestrator.py` after `run_task_in_pane` (line 113):

```python
# --- agents grid ------------------------------------------------------------

GRID_WINDOW = "agents"
GRID_MAX = int(os.environ.get("SILICORISM_GRID_MAX") or 4)
RUNNING, DONE, FAILED = "RUNNING", "DONE", "FAILED"
_MARKERS = (RUNNING, DONE, FAILED)
# Pane border doubles as the label bar: "<agent-id> <status>".
PANE_FORMAT = "#[align=left] #{pane_title} "


def _grid_windows(session: str) -> list[str]:
    """Existing agents* window names, oldest first."""
    r = _tmux("list-windows", "-t", session, "-F", "#{window_name}")
    if r.returncode != 0:
        return []
    return [n for n in r.stdout.split()
            if n == GRID_WINDOW or n.startswith(GRID_WINDOW + "-")]


def _pane_count(session: str, window: str) -> int:
    r = _tmux("list-panes", "-t", f"{session}:{window}", "-F", "#{pane_id}")
    return len(r.stdout.split()) if r.returncode == 0 else 0


def _next_grid_window(session: str) -> tuple[str, bool]:
    """(window name, needs_creating) for the next agent pane."""
    wins = _grid_windows(session)
    for w in wins:
        if _pane_count(session, w) < GRID_MAX:
            return w, False
    return (GRID_WINDOW if not wins else f"{GRID_WINDOW}-{len(wins) + 1}"), True


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
    _tmux("select-pane", "-t", pane, "-T", f"{label} {RUNNING}")
    _tmux("select-layout", "-t", target, "tiled")
    tmp = shlex.quote(sentinel + ".tmp")
    fin = shlex.quote(sentinel)
    log = shlex.quote(logfile)
    wrapped = f"{{ {command}; echo $? > {tmp}; }} 2>&1 | tee {log}; mv {tmp} {fin}"
    _tmux("send-keys", "-t", pane, wrapped, "Enter")
    return window, pane


def mark_pane_done(pane_id: str, *, failed: bool = False) -> None:
    """Swap a pane's status marker to DONE/FAILED, keeping its label."""
    r = _tmux("display-message", "-p", "-t", pane_id, "#{pane_title}")
    title = r.stdout.strip() if r.returncode == 0 else ""
    base = title.rsplit(" ", 1)[0] if title.endswith(_MARKERS) else title
    _tmux("select-pane", "-t", pane_id, "-T",
          f"{base} {FAILED if failed else DONE}".strip())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_grid.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Verify against a real tmux server**

Run:

```bash
.venv/bin/python -c "
import tmux_orchestrator as t
t.ensure_session('gridcheck')
for i in range(5):
    print(t.grid_pane(i, f'agent-{i}', '/tmp', 'sleep 30', f'/tmp/s{i}', session='gridcheck'))
" && tmux list-panes -a -t gridcheck -F '#{window_name} #{pane_id} #{pane_title}'; tmux kill-session -t gridcheck
```

Expected: four panes in `agents`, one in `agents-2`, each title ending `RUNNING`. Paste the real output into the commit or the task report — mocked tests do not prove tmux accepts these flags.

- [ ] **Step 6: Commit**

```bash
git add tmux_orchestrator.py tests/test_grid.py
git commit -m "feat: tile agents into shared tmux grid windows with pane-id addressing"
```

---

### Task 3: Wire the worker to the grid with a safe fallback

**Files:**
- Modify: `worker.py:59-78` (`_run_native`), `worker.py:141-158` (failure branch)
- Test: `tests/test_grid.py` (append)

**Interfaces:**
- Consumes: `tmux.grid_pane`, `tmux.mark_pane_done` (Task 2); `db.set_pane_target` (Task 1).
- Produces: `worker._place_pane(conn, task, command, sentinel, logfile) -> tuple[str, str | None]` returning `(window, pane_id_or_None)`; `pane_id` is `None` when the grid failed and the legacy window path was used.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_grid.py`:

```python
def test_worker_falls_back_to_a_window_when_the_grid_fails(tmp_path):
    """tmux breakage must never fail a task — the pane is a viewport, not a dep."""
    import db
    import worker

    dbp = str(tmp_path / "w.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", "{}")
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()

    with patch.object(worker.tmux, "grid_pane", side_effect=RuntimeError("no server")), \
         patch.object(worker.tmux, "run_task_in_pane",
                      return_value="task-1-pi") as legacy:
        window, pane = worker._place_pane(conn, task, "pi 'go'", "/tmp/s", "/tmp/l")

    assert pane is None and window == "task-1-pi"
    assert legacy.called
    conn.close()


def test_worker_records_the_pane_target(tmp_path):
    import db
    import worker

    dbp = str(tmp_path / "w2.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", '{"agent_id": "builder-x"}')
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()

    with patch.object(worker.tmux, "grid_pane", return_value=("agents", "%7")):
        worker._place_pane(conn, task, "pi 'go'", "/tmp/s", "/tmp/l")

    stored = conn.execute("SELECT pane_target FROM tasks WHERE id=?",
                          (tid,)).fetchone()["pane_target"]
    assert stored == "agents.%7"
    conn.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_grid.py -q -k worker`
Expected: FAIL — `AttributeError: module 'worker' has no attribute '_place_pane'`

- [ ] **Step 3: Add `_place_pane` and use it**

Add to `worker.py` above `_run_native`:

```python
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
```

Replace the body of `_run_native` (`worker.py:59-78`) with:

```python
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


def _mark_pane(task_id, pane, *, failed: bool) -> None:
    """Retitle the grid pane, or the legacy window when there is no pane id."""
    try:
        if pane:
            tmux.mark_pane_done(pane, failed=failed)
        else:
            tmux.mark_done(task_id, failed=failed)
    except Exception:  # noqa: BLE001
        pass
```

In `run_worker`'s failure branch, delete the now-duplicated marking at `worker.py:152-156`:

```python
                if native_cmd is not None:
                    try:
                        tmux.mark_done(tid, failed=True)
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    _close_task_window(tid, failed=True)
```

becomes:

```python
                if native_cmd is None:  # native panes are marked in _run_native
                    _close_task_window(tid, failed=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_grid.py -q`
Expected: PASS (9 passed)

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, no regressions in `tests/test_workflow.py`.

- [ ] **Step 6: Commit**

```bash
git add worker.py tests/test_grid.py
git commit -m "feat: run native agents in grid panes, recording pane_target"
```

---

### Task 4: Curses dashboard with a DAG tree

**Files:**
- Create: `dashboard.py`, `tests/test_dashboard.py`
- Modify: `cli.py:173-198` (`_dashboard_frame`, `cmd_dashboard`), `pyproject.toml` (`py-modules`)

**Interfaces:**
- Consumes: `db.all_tasks`, `db.recent_messages`, `db.counts` (Task 1).
- Produces:
  - `dashboard.build_frame(tasks, messages, counts, *, width=100, now=None) -> list[str]` — pure, no curses, no DB.
  - `dashboard.run(conn, interval=1.0) -> None` — curses loop, falls back to plain printing.
  - `dashboard.short_model(payload: str | None) -> str`
  - `dashboard.elapsed(row, now) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_dashboard.py`:

```python
"""The frame builder is a pure function, so the whole dashboard is testable
without a terminal. curses only paints the strings it returns."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard  # noqa: E402

NOW = datetime(2026, 7, 25, 12, 0, 30, tzinfo=timezone.utc)


def _row(tid, ttype, status, *, deps=None, payload=None, started=None,
         updated=None, pane=None):
    return {"id": tid, "task_type": ttype, "status": status,
            "depends_on": json.dumps(deps) if deps else None,
            "payload": payload, "started_at": started, "updated_at": updated,
            "pane_target": pane, "agent_id": None}


def test_short_model_reverses_the_alias_table():
    payload = json.dumps(
        {"model": "bedrock-mantle/qwen.qwen3-coder-480b-a35b-instruct"})
    assert dashboard.short_model(payload) == "qwen3-coder-480b"


def test_short_model_survives_garbage():
    assert dashboard.short_model("not json") == "-"
    assert dashboard.short_model(None) == "-"
    assert dashboard.short_model("{}") == "-"


def test_elapsed_formats_running_and_finished_tasks():
    running = _row(1, "pi", "processing", started="2026-07-25T12:00:00.000Z")
    assert dashboard.elapsed(running, NOW) == "0m30"
    done = _row(2, "pi", "completed", started="2026-07-25T12:00:00.000Z",
                updated="2026-07-25T12:01:02.000Z")
    assert dashboard.elapsed(done, NOW) == "1m02"
    assert dashboard.elapsed(_row(3, "pi", "pending"), NOW) == "-"


def test_tree_indents_children_under_their_first_dependency():
    tasks = [_row(1, "worktree_create", "completed"),
             _row(2, "pi", "completed", deps=[1]),
             _row(3, "pi", "processing", deps=[2])]
    frame = dashboard.build_frame(tasks, [], {"pending": 0, "processing": 1,
                                              "completed": 2, "failed": 0},
                                  now=NOW)
    body = [ln for ln in frame if "pi" in ln or "worktree" in ln]
    assert body[0].startswith(" ") and not body[0].lstrip().startswith("+-")
    assert body[1].lstrip().startswith("+-")
    assert body[2].index("+-") > body[1].index("+-")


def test_fanout_siblings_render_at_equal_depth():
    tasks = [_row(1, "pi", "completed"),
             _row(2, "pi", "processing", deps=[1]),
             _row(3, "pi", "processing", deps=[1])]
    frame = dashboard.build_frame(tasks, [], {"pending": 0, "processing": 2,
                                              "completed": 1, "failed": 0},
                                  now=NOW)
    lines = [ln for ln in frame if "pi" in ln]
    assert lines[1].index("+-") == lines[2].index("+-")


def test_counts_and_pane_target_appear():
    tasks = [_row(1, "pi", "processing", pane="agents.%5")]
    frame = dashboard.build_frame(tasks, [], {"pending": 3, "processing": 1,
                                              "completed": 0, "failed": 2},
                                  now=NOW)
    text = "\n".join(frame)
    assert "pending 3" in text and "failed 2" in text
    assert "agents.%5" in text


def test_messages_are_rendered_and_newlines_flattened():
    frame = dashboard.build_frame(
        [], [{"sender_id": "a", "recipient_id": "b", "content": "one\ntwo",
              "status": "unread"}],
        {"pending": 0, "processing": 0, "completed": 0, "failed": 0}, now=NOW)
    text = "\n".join(frame)
    assert "a->b" in text and "one two" in text


def test_lines_are_truncated_never_wrapped():
    tasks = [_row(1, "pi", "processing", payload=json.dumps({"model": "glm-5"}),
                  pane="agents.%5" * 20)]
    frame = dashboard.build_frame(tasks, [], {"pending": 0, "processing": 1,
                                              "completed": 0, "failed": 0},
                                  width=40, now=NOW)
    assert all(len(line) <= 40 for line in frame), frame
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dashboard.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'dashboard'`

- [ ] **Step 3: Write `dashboard.py`**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_dashboard.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Point the CLI at it**

In `cli.py`, replace `_dashboard_frame` and `cmd_dashboard` (lines 173-198) with:

```python
def cmd_dashboard(args) -> None:
    """Curses status + DAG + P2P view (runs in the supervisor's window 0)."""
    import dashboard

    conn = db.connect(args.db)
    try:
        dashboard.run(conn, interval=args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()
```

Add `"dashboard"` to `py-modules` in `pyproject.toml`:

```toml
py-modules = ["cli", "db", "handlers", "worker", "silicorism_tools",
              "tmux_orchestrator", "skills", "silicorism_mcp", "dashboard"]
```

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. If a test referenced `cli._dashboard_frame`, repoint it at `dashboard.build_frame`.

- [ ] **Step 7: Verify it renders against a real DB**

Run:

```bash
.venv/bin/python -c "
import db, dashboard, tempfile, os, json
d = tempfile.mkdtemp(); p = os.path.join(d, 'demo.db'); db.init_db(p)
c = db.connect(p)
a = db.add_task(c, 'worktree_create', '{}')
b = db.add_task(c, 'pi', json.dumps({'model': 'glm-5'}), depends_on=a)
db.add_task(c, 'pi', json.dumps({'model': 'kimi-k2.5'}), depends_on=b)
db.claim_task(c, 'w1')
print('\n'.join(dashboard.frame(c)))
"
```

Expected: header, counts, an indented three-node tree with model names. Paste the output in the task report.

- [ ] **Step 8: Commit**

```bash
git add dashboard.py tests/test_dashboard.py cli.py pyproject.toml
git commit -m "feat: curses dashboard with DAG tree, model and timing columns"
```

---

### Task 5: `simple` and `standard` complexity tiers

**Files:**
- Modify: `silicorism_tools.py:27-74` (`build_pipeline`)
- Test: `tests/test_tiers.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `build_pipeline(conn, db_path, name, prompt, *, base="main", test_command="pytest -q", max_attempts=3, merge=False, complexity="standard", cwd=None) -> dict`
  - `SIMPLE_MODEL = "qwen3-coder-480b"` resolved through `handlers.resolve_model`.
  - Return shape is unchanged: `{"name", "worktree_path", "tasks": {...}}`. For `simple`, `worktree_path` is the working directory and `tasks` holds `{"solo"}` or `{"solo", "verify"}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tiers.py`:

```python
"""Tier shapes: how many agents a request gets, and whether it needs a worktree."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
import silicorism_tools as st  # noqa: E402


def _conn(tmp_path, name="t.db"):
    dbp = str(tmp_path / name)
    db.init_db(dbp)
    return db.connect(dbp), dbp


def _payload(conn, task_id):
    return json.loads(conn.execute("SELECT payload FROM tasks WHERE id=?",
                                   (task_id,)).fetchone()["payload"])


def test_simple_is_one_agent_and_no_worktree(tmp_path):
    conn, dbp = _conn(tmp_path)
    out = st.build_pipeline(conn, dbp, "game", "build a python game",
                            complexity="simple")
    assert list(out["tasks"]) == ["solo"]
    types = [r["task_type"] for r in conn.execute(
        "SELECT task_type FROM tasks ORDER BY id")]
    assert types == ["pi"]  # no worktree_create, no cleanup
    conn.close()


def test_simple_pins_qwen3_coder(tmp_path):
    conn, dbp = _conn(tmp_path)
    out = st.build_pipeline(conn, dbp, "game", "build a python game",
                            complexity="simple")
    assert _payload(conn, out["tasks"]["solo"])["model"] == (
        "bedrock-mantle/qwen.qwen3-coder-480b-a35b-instruct")
    conn.close()


def test_simple_adds_verify_only_when_a_test_command_is_given(tmp_path):
    conn, dbp = _conn(tmp_path)
    out = st.build_pipeline(conn, dbp, "game", "build it", complexity="simple",
                            test_command="pytest -q")
    assert list(out["tasks"]) == ["solo", "verify"]
    row = conn.execute("SELECT depends_on FROM tasks WHERE id=?",
                       (out["tasks"]["verify"],)).fetchone()
    assert json.loads(row["depends_on"]) == [out["tasks"]["solo"]]
    conn.close()


def test_standard_is_unchanged_and_is_the_default(tmp_path):
    conn, dbp = _conn(tmp_path)
    default = st.build_pipeline(conn, dbp, "a", "add auth")
    explicit = st.build_pipeline(conn, dbp, "b", "add auth", complexity="standard")
    assert list(default["tasks"]) == ["worktree", "scout", "builder", "fixer",
                                      "verify", "cleanup"]
    assert list(explicit["tasks"]) == list(default["tasks"])
    conn.close()


def test_unknown_tier_falls_back_to_standard(tmp_path):
    """A typo in a planning hint must not fail a submit."""
    conn, dbp = _conn(tmp_path)
    out = st.build_pipeline(conn, dbp, "c", "add auth", complexity="medium")
    assert list(out["tasks"]) == ["worktree", "scout", "builder", "fixer",
                                  "verify", "cleanup"]
    conn.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tiers.py -q`
Expected: FAIL — `TypeError: build_pipeline() got an unexpected keyword argument 'complexity'`

- [ ] **Step 3: Split the pipeline into tier builders**

In `silicorism_tools.py`, add below `DEFAULT_THINKING`:

```python
# `simple` runs one agent on the strongest coder in the trio; no scout to read
# a codebase that may not exist yet, no fixer loop for a task this size.
SIMPLE_MODEL = "bedrock-mantle/qwen.qwen3-coder-480b-a35b-instruct"
```

Rename the existing `build_pipeline` body to `_build_standard` (keeping every line as-is), then add the dispatcher and the simple shape:

```python
def _build_simple(conn, db_path, name, prompt, *, test_command=None, cwd=None) -> dict:
    """One agent in the current directory; verify only if there is a command.

    No worktree: a fresh scratch project has no git repo to branch from. No
    unconditional verify gate: it would fail a project that has no tests yet.
    """
    work = cwd or os.getcwd()
    solo = db.add_task(conn, "pi", json.dumps({
        "model": SIMPLE_MODEL, "thinking": DEFAULT_THINKING,
        "cwd": work, "p2p": False, "agent_id": f"solo-{name}", "db": db_path,
        "prompt": prompt,
    }))
    tasks = {"solo": solo}
    if test_command:
        tasks["verify"] = db.add_task(
            conn, "verify",
            json.dumps({"test_command": test_command, "cwd": work}),
            depends_on=solo, max_retries=0)
    return {"name": name, "worktree_path": work, "tasks": tasks}


def build_pipeline(conn, db_path, name, prompt, *, base="main",
                   test_command="pytest -q", max_attempts=3, merge=False,
                   complexity="standard", cwd=None) -> dict:
    """Build a DAG sized to the request. Tiers:

      simple    one agent (qwen3-coder-480b) in cwd, verify iff test_command
      standard  worktree -> scout -> builder -> fixer -> verify [-> merge] -> cleanup
      complex   parallel builders in separate worktrees joined by an integrator

    An unknown tier degrades to `standard` — a typo in a planning hint must
    not fail a submit.
    """
    if complexity == "simple":
        return _build_simple(conn, db_path, name, prompt,
                             test_command=test_command, cwd=cwd)
    if complexity == "complex":
        return _build_complex(conn, db_path, name, prompt, base=base,
                              test_command=test_command,
                              max_attempts=max_attempts, merge=merge)
    return _build_standard(conn, db_path, name, prompt, base=base,
                           test_command=test_command,
                           max_attempts=max_attempts, merge=merge)
```

Note: `_build_complex` arrives in Task 6. Until then, add this stub immediately above `build_pipeline` so the module imports cleanly:

```python
def _build_complex(conn, db_path, name, prompt, **kw) -> dict:
    raise NotImplementedError("complex tier lands in Task 6")
```

`_build_simple` calls `build_pipeline`'s caller with `test_command=None` to get the no-verify shape; the MCP layer passes `None` when the planner omits it (Task 7).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tiers.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `tests/test_mcp.py` and `silicorism_tools.py`'s `__main__` self-check both exercise the default path, which is unchanged.

- [ ] **Step 6: Commit**

```bash
git add silicorism_tools.py tests/test_tiers.py
git commit -m "feat: simple/standard complexity tiers for pipeline sizing"
```

---

### Task 6: `complex` tier — parallel builders and `worktree_integrate`

**Files:**
- Modify: `handlers.py` (add `worktree_integrate` after `worktree_merge` at line 293; register it in the handler table), `silicorism_tools.py` (replace the `_build_complex` stub)
- Test: `tests/test_integrate.py` (create), `tests/test_tiers.py` (append)

**Interfaces:**
- Consumes: `build_pipeline` dispatch (Task 5).
- Produces:
  - `handlers.worktree_integrate(payload: str, context=None) -> str` — payload `{into, from_worktree, branch, db?}`. Returns `"clean"` on a conflict-free merge, or `"conflicts: <file>, <file>"` leaving the tree conflicted.
  - `_build_complex(...) -> dict` with `tasks` keys `worktree_a`, `worktree_b`, `scout`, `builder_a`, `builder_b`, `integrate`, `integrator`, `fixer`, `verify`, optionally `merge`, then `cleanup_a`, `cleanup_b`.

- [ ] **Step 1: Write the failing test for the handler**

Create `tests/test_integrate.py`:

```python
"""worktree_integrate against real git — a merge asserted only against command
strings proves nothing about whether the merge works."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handlers  # noqa: E402


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    """A repo on 'main' with two worktrees branched from it."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-b", "main"], root)
    _git(["config", "user.email", "t@t"], root)
    _git(["config", "user.name", "t"], root)
    (root / "base.txt").write_text("base\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "init"], root)
    wt_a, wt_b = tmp_path / "wt-a", tmp_path / "wt-b"
    _git(["worktree", "add", "-b", "feat-a", str(wt_a), "main"], root)
    _git(["worktree", "add", "-b", "feat-b", str(wt_b), "main"], root)
    for wt in (wt_a, wt_b):
        _git(["config", "user.email", "t@t"], wt)
        _git(["config", "user.name", "t"], wt)
    return root, wt_a, wt_b


def test_disjoint_changes_merge_cleanly(repo):
    _, wt_a, wt_b = repo
    (wt_a / "a.py").write_text("A\n")
    _git(["add", "-A"], wt_a)
    _git(["commit", "-m", "a"], wt_a)
    (wt_b / "b.py").write_text("B\n")

    out = handlers.worktree_integrate(json.dumps(
        {"into": str(wt_a), "from_worktree": str(wt_b), "branch": "feat-b"}))

    assert out == "clean"
    assert (wt_a / "b.py").read_text() == "B\n"  # b's work landed in a


def test_overlapping_changes_report_conflicts_and_leave_the_tree_conflicted(repo):
    _, wt_a, wt_b = repo
    (wt_a / "same.py").write_text("from A\n")
    _git(["add", "-A"], wt_a)
    _git(["commit", "-m", "a"], wt_a)
    (wt_b / "same.py").write_text("from B\n")

    out = handlers.worktree_integrate(json.dumps(
        {"into": str(wt_a), "from_worktree": str(wt_b), "branch": "feat-b"}))

    assert out.startswith("conflicts:") and "same.py" in out
    # Left conflicted on purpose so the integrator agent has something to fix.
    assert "<<<<<<<" in (wt_a / "same.py").read_text()
    status = _git(["status", "--porcelain"], wt_a).stdout
    assert "UU" in status or "AA" in status


def test_missing_required_field_raises():
    with pytest.raises(ValueError):
        handlers.worktree_integrate(json.dumps({"into": "/tmp/x"}))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_integrate.py -q`
Expected: FAIL — `AttributeError: module 'handlers' has no attribute 'worktree_integrate'`

- [ ] **Step 3: Implement `worktree_integrate`**

Add to `handlers.py` after `worktree_merge` (line 293):

```python
def worktree_integrate(payload: str, context=None) -> str:
    """Merge one worktree's branch into another worktree, in place.

    Payload: {into, from_worktree, branch, db?}. `worktree_merge` cannot do
    this: it runs `git switch <base>` in the main repo, and git refuses to
    check out a branch that is already checked out in another worktree. Here
    the target branch is already checked out in `into`, so no switch happens.

    Contract differs from worktree_merge on purpose: a conflict does NOT abort.
    The conflicted tree is left in place and the conflicting paths are returned,
    so the integrator agent downstream has something concrete to resolve.
    """
    data = _parse(payload, required=("into", "from_worktree", "branch"))
    into, src, branch = data["into"], data["from_worktree"], data["branch"]
    # Agents leave work uncommitted; commit the source so the merge can see it.
    _git(["add", "-A"], cwd=src)
    _git(["commit", "-m", f"silicorism: {branch}"], cwd=src)  # noop if clean
    mg = _git(["merge", "--no-ff", "-m", f"silicorism: integrate {branch}",
               branch], cwd=into)
    if mg.returncode == 0:
        return "clean"
    conflicted = _git(["diff", "--name-only", "--diff-filter=U"], cwd=into)
    files = [f for f in conflicted.stdout.split() if f]
    _wt_state(data.get("db"), into, "conflicted", branch=branch)
    return "conflicts: " + ", ".join(files or ["unknown"])
```

Register it in the `HANDLERS` dict at `handlers.py:389`, on the line after
`"worktree_merge": worktree_merge,`:

```python
    "worktree_integrate": worktree_integrate,
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_integrate.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Write the failing test for the complex DAG shape**

Append to `tests/test_tiers.py`:

```python
def test_complex_forks_two_builders_from_the_scout(tmp_path):
    conn, dbp = _conn(tmp_path)
    out = st.build_pipeline(conn, dbp, "big", "rewrite the parser",
                            complexity="complex")
    t = out["tasks"]
    assert set(t) >= {"worktree_a", "worktree_b", "scout", "builder_a",
                      "builder_b", "integrate", "integrator", "fixer",
                      "verify", "cleanup_a", "cleanup_b"}

    def deps(key):
        row = conn.execute("SELECT depends_on FROM tasks WHERE id=?",
                           (t[key],)).fetchone()
        return json.loads(row["depends_on"] or "[]")

    # Both builders hang off the scout - that is the fan-out.
    assert deps("builder_a") == [t["scout"]]
    assert deps("builder_b") == [t["scout"]]
    # Integration waits for both.
    assert set(deps("integrate")) == {t["builder_a"], t["builder_b"]}
    assert deps("integrator") == [t["integrate"]]
    conn.close()


def test_complex_gives_each_builder_its_own_worktree(tmp_path):
    conn, dbp = _conn(tmp_path)
    out = st.build_pipeline(conn, dbp, "big", "rewrite the parser",
                            complexity="complex")
    a = _payload(conn, out["tasks"]["builder_a"])["cwd"]
    b = _payload(conn, out["tasks"]["builder_b"])["cwd"]
    assert a != b, "concurrent builders must not share a worktree"


def test_complex_cleans_up_both_worktrees_last(tmp_path):
    conn, dbp = _conn(tmp_path)
    out = st.build_pipeline(conn, dbp, "big", "rewrite", complexity="complex")
    t = out["tasks"]
    for key in ("cleanup_a", "cleanup_b"):
        row = conn.execute("SELECT depends_on FROM tasks WHERE id=?",
                           (t[key],)).fetchone()
        # Cleanup trails the last real work node, so a failed run keeps its
        # worktree (and branch) intact for post-mortem.
        assert json.loads(row["depends_on"])[0] >= t["verify"]
    conn.close()
```

- [ ] **Step 6: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tiers.py -q -k complex`
Expected: FAIL — `NotImplementedError: complex tier lands in Task 6`

- [ ] **Step 7: Replace the `_build_complex` stub**

In `silicorism_tools.py`:

```python
SPLIT_NOTE = (
    "Partition the work into exactly TWO slices that touch DISJOINT files. "
    "Write CONTEXT.md, then end your reply with:\n"
    "SLICE A: <files and what to build>\n"
    "SLICE B: <files and what to build>\n"
    "Overlapping slices cause merge conflicts downstream — keep them disjoint."
)


def _build_complex(conn, db_path, name, prompt, *, base="main",
                   test_command="pytest -q", max_attempts=3, merge=False) -> dict:
    """Two builders in separate worktrees, joined by a merge + integrator agent.

    The scout partitions the work; each builder receives that partition through
    the existing artifact hand-off (dep_artifacts -> _with_context), so
    builder-b needs no file from worktree-a.
    """
    name_a, name_b = f"{name}-a", f"{name}-b"
    path_a = os.path.join(WORKTREE_ROOT, name_a)
    path_b = os.path.join(WORKTREE_ROOT, name_b)
    wt_a = db.add_task(conn, "worktree_create",
                       json.dumps({"branch": name_a, "base": base, "db": db_path}),
                       worktree_path=path_a)
    wt_b = db.add_task(conn, "worktree_create",
                       json.dumps({"branch": name_b, "base": base, "db": db_path}),
                       worktree_path=path_b)
    scout = db.add_task(conn, "pi", json.dumps({
        "model": DEFAULT_MODELS["scout"], "thinking": DEFAULT_THINKING,
        "cwd": path_a, "p2p": True, "agent_id": f"scout-{name}", "db": db_path,
        "prompt": f"Scout the repo for: {prompt}. {SPLIT_NOTE}",
    }), depends_on=[wt_a, wt_b], worktree_path=path_a)
    builder_a = db.add_task(conn, "pi", json.dumps({
        "model": DEFAULT_MODELS["builder"], "thinking": DEFAULT_THINKING,
        "cwd": path_a, "p2p": True, "agent_id": f"builder-a-{name}", "db": db_path,
        "prompt": f"Builder A: implement SLICE A only. {prompt}",
    }), depends_on=scout, worktree_path=path_a)
    builder_b = db.add_task(conn, "pi", json.dumps({
        "model": DEFAULT_MODELS["builder"], "thinking": DEFAULT_THINKING,
        "cwd": path_b, "p2p": True, "agent_id": f"builder-b-{name}", "db": db_path,
        "prompt": f"Builder B: implement SLICE B only. {prompt}",
    }), depends_on=scout, worktree_path=path_b)
    integrate = db.add_task(conn, "worktree_integrate", json.dumps({
        "into": path_a, "from_worktree": path_b, "branch": name_b, "db": db_path,
    }), depends_on=[builder_a, builder_b], worktree_path=path_a, max_retries=0)
    integrator = db.add_task(conn, "pi", json.dumps({
        "model": DEFAULT_MODELS["fixer"], "thinking": DEFAULT_THINKING,
        "cwd": path_a, "p2p": True, "agent_id": f"integrator-{name}", "db": db_path,
        "prompt": ("Integration step. The prior task's artifact says either "
                   "'clean' or 'conflicts: <files>'. If clean, reply 'nothing "
                   "to do' and stop. Otherwise resolve every conflict marker in "
                   "those files, keeping BOTH slices' behaviour, then "
                   "`git add -A && git commit`."),
    }), depends_on=integrate, worktree_path=path_a)
    fixer = db.add_task(conn, "fixer_loop", json.dumps({
        "test_command": test_command, "agent_type": "pi",
        "model": DEFAULT_MODELS["fixer"], "thinking": DEFAULT_THINKING,
        "cwd": path_a, "max_attempts": max_attempts, "db": db_path,
        "upstream": f"integrator-{name}", "agent_id": f"fixer-{name}",
    }), depends_on=integrator, worktree_path=path_a)
    verify_id = db.add_task(conn, "verify",
                            json.dumps({"test_command": test_command, "cwd": path_a}),
                            depends_on=fixer, worktree_path=path_a, max_retries=0)
    tasks = {"worktree_a": wt_a, "worktree_b": wt_b, "scout": scout,
             "builder_a": builder_a, "builder_b": builder_b,
             "integrate": integrate, "integrator": integrator,
             "fixer": fixer, "verify": verify_id}
    last = verify_id
    if merge:
        last = db.add_task(conn, "worktree_merge",
                           json.dumps({"worktree_path": path_a, "branch": name_a,
                                       "base": base, "db": db_path}),
                           depends_on=last, worktree_path=path_a, max_retries=0)
        tasks["merge"] = last
    # Both cleanups trail the last work node: a failed run keeps its worktrees
    # and branches for post-mortem.
    tasks["cleanup_a"] = db.add_task(
        conn, "worktree_cleanup",
        json.dumps({"worktree_path": path_a, "branch": name_a, "db": db_path}),
        depends_on=last, worktree_path=path_a)
    tasks["cleanup_b"] = db.add_task(
        conn, "worktree_cleanup",
        json.dumps({"worktree_path": path_b, "branch": name_b, "db": db_path}),
        depends_on=last, worktree_path=path_b)
    return {"name": name, "worktree_path": path_a, "tasks": tasks}
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tiers.py tests/test_integrate.py -q`
Expected: PASS (11 passed)

- [ ] **Step 9: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add handlers.py silicorism_tools.py tests/test_tiers.py tests/test_integrate.py
git commit -m "feat: complex tier with parallel builders and worktree_integrate"
```

---

### Task 7: `silicorism_wait` and the token-economy changes

**Files:**
- Modify: `silicorism_tools.py` (add `wait_for_settle`, trim `get_status`), `silicorism_mcp.py` (`_plan_and_submit` gains `complexity`, new `_wait` handler + TOOLS entry, `INSTRUCTIONS` rewrite), `handlers.py:44-45` (stale comment)
- Test: `tests/test_wait.py` (create), `tests/test_mcp.py` (append)

**Interfaces:**
- Consumes: `verify_status` (existing), `build_pipeline(..., complexity=...)` (Task 5).
- Produces:
  - `silicorism_tools.wait_for_settle(conn, *, timeout_s=600.0, poll=1.0, stop=None) -> dict` — the `verify_status` dict plus `"settled": bool`.
  - MCP tool `silicorism_wait` with `{db?, timeout_s?}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_wait.py`:

```python
"""wait_for_settle turns the orchestrator's poll loop into one call."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
import silicorism_tools as st  # noqa: E402


def _conn(tmp_path):
    dbp = str(tmp_path / "w.db")
    db.init_db(dbp)
    return db.connect(dbp), dbp


def test_returns_immediately_when_the_queue_is_already_settled(tmp_path):
    conn, _ = _conn(tmp_path)
    tid = db.add_task(conn, "echo", "hi")
    db.claim_task(conn, "w")
    db.complete_task(conn, tid, "done")
    out = st.wait_for_settle(conn, timeout_s=30, poll=0.01)
    assert out["settled"] is True and out["satisfied"] is True
    conn.close()


def test_times_out_while_work_is_still_pending(tmp_path):
    conn, _ = _conn(tmp_path)
    db.add_task(conn, "echo", "hi")
    out = st.wait_for_settle(conn, timeout_s=1, poll=0.05)
    assert out["settled"] is False and out["active"] == 1
    conn.close()


def test_returns_early_on_the_first_failure(tmp_path):
    """A failed node must not cost the orchestrator a full timeout of waiting."""
    conn, _ = _conn(tmp_path)
    tid = db.add_task(conn, "fail", "boom", max_retries=0)
    db.add_task(conn, "echo", "still pending")
    db.claim_task(conn, "w")
    db.fail_task(conn, tid)
    out = st.wait_for_settle(conn, timeout_s=30, poll=0.01)
    assert out["settled"] is True and out["failures"]
    conn.close()


def test_timeout_is_capped(tmp_path):
    conn, _ = _conn(tmp_path)
    db.add_task(conn, "echo", "hi")
    out = st.wait_for_settle(conn, timeout_s=99999, poll=0.01,
                             stop=lambda: True)  # stop fires immediately
    assert out["settled"] is False
    conn.close()


def test_status_stays_small(tmp_path):
    """Successful artifacts must never reach the orchestrator's context."""
    conn, _ = _conn(tmp_path)
    for i in range(30):
        tid = db.add_task(conn, "echo", f"t{i}")
        db.claim_task(conn, "w")
        db.complete_task(conn, tid, "x" * 5000)
    status = st.get_status(conn)
    assert len(status["messages"]) <= 5 and len(status["logs"]) <= 5
    assert "x" * 5000 not in str(status)
    conn.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_wait.py -q`
Expected: FAIL — `AttributeError: module 'silicorism_tools' has no attribute 'wait_for_settle'`

- [ ] **Step 3: Implement the wait and trim the status**

Add `import time` to `silicorism_tools.py`'s imports, then add after `verify_status`:

```python
WAIT_CAP_S = 3600.0


def wait_for_settle(conn, *, timeout_s=600.0, poll=1.0, stop=None) -> dict:
    """Block until the queue settles, then return the verdict once.

    Settled = nothing pending or processing, OR at least one task has failed
    (waiting out a doomed run costs the orchestrator a turn for nothing). This
    replaces the poll loop: one Claude turn per DAG instead of one per poll.
    """
    deadline = time.monotonic() + min(max(float(timeout_s), 1.0), WAIT_CAP_S)
    while True:
        verdict = verify_status(conn)
        if verdict["active"] == 0 or verdict["failures"]:
            verdict["settled"] = True
            return verdict
        if (stop and stop()) or time.monotonic() >= deadline:
            verdict["settled"] = False
            return verdict
        time.sleep(poll)
```

In `get_status`, cut the payload the orchestrator pays for — change the three list comprehensions to:

```python
        "agents": [dict(h) for h in db.heartbeats(conn)],
        "messages": [dict(m) for m in db.recent_messages(conn, 5)],
        "logs": [dict(r) for r in db.recent_logs(conn, 5)],
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_wait.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Expose it over MCP and pass `complexity` through**

In `silicorism_mcp.py`, add the handler after `_get_status`:

```python
def _wait(args: dict) -> str:
    """Block until the queue settles, then return the verdict once.

    One call replaces a poll loop: every poll would otherwise be a full
    orchestrator turn that learns nothing but "still running".
    """
    dbp = _db(args)
    db.init_db(dbp)
    conn = db.connect(dbp)
    try:
        return json.dumps(silicorism_tools.wait_for_settle(
            conn, timeout_s=float(args.get("timeout_s") or 600)))
    finally:
        conn.close()
```

In `_plan_and_submit`, pass the tier through to `build_pipeline`:

```python
            out = silicorism_tools.build_pipeline(
                conn, dbp, args.get("name") or _slug(args["prompt"]), args["prompt"],
                base=args.get("base") or "main",
                test_command=args.get("test_command"),
                max_attempts=int(args.get("max_attempts") or 3),
                complexity=args.get("complexity") or "standard")
            out["mode"] = "pipeline"
```

Because `test_command` now defaults to `None` here, `standard` must keep its own default. In `_build_standard` and `_build_complex`, change the signature default to `test_command="pytest -q"` and add at the top of each:

```python
    test_command = test_command or "pytest -q"
```

Add the tool definition to `TOOLS`. Dispatch is by the `"handler"` key inside each
entry (`silicorism_mcp.py:327` does `tool["handler"](...)`), so there is no separate
registration step — the `"handler"` line below IS the registration:

```python
    {
        "name": "silicorism_wait",
        "description": "Block until the queue settles (all tasks terminal, or "
                       "any task failed), then return the verdict once. Use "
                       "this instead of polling silicorism_get_status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "db": {"type": "string"},
                "timeout_s": {"type": "number",
                              "description": "Max seconds to block (cap 3600)."},
            },
        },
        "handler": _wait,
    },
```

The entry must appear after `_wait` is defined, since `"handler": _wait` evaluates the
name at module import.

Add `complexity` to `silicorism_plan_and_submit`'s `inputSchema` properties:

```python
                "complexity": {
                    "type": "string",
                    "enum": ["simple", "standard", "complex"],
                    "description": "Sizes the DAG. simple = one agent in cwd, "
                                   "no worktree (a small self-contained "
                                   "program). standard = scout/builder/fixer "
                                   "in a worktree. complex = parallel builders "
                                   "in separate worktrees plus an integrator. "
                                   "Defaults to standard.",
                },
```

- [ ] **Step 6: Rewrite the orchestrator protocol**

Replace steps 4-5 of `INSTRUCTIONS` in `silicorism_mcp.py` and its model line:

```python
    "4. SUBMIT: call silicorism_plan_and_submit with `nodes` (a custom DAG) or "
    "with `prompt` + `complexity` (simple | standard | complex). Size the tier to "
    "the request: a small self-contained program is `simple` (ONE agent, no "
    "worktree), not three. It auto-starts native tmux-pane workers. Tell the user "
    "to watch with `tmux attach -t silicorism-session`.\n"
    "5. WAIT, DO NOT POLL: call silicorism_wait once. It blocks until the queue "
    "settles and returns the verdict. Polling silicorism_get_status in a loop "
    "burns a full turn per poll for no information.\n"
    "6. VERIFY & LOOP: if not satisfied, inspect the failed tasks' artifacts and "
    "errors, formulate a corrective DAG, and resubmit until every requirement and "
    "test gate is met.\n"
    "Spend your reasoning at plan time: each node's prompt must carry explicit "
    "acceptance criteria and file-level scope, because the executing models are "
    "smaller than you and fail on underspecified instructions.\n"
    "Execution models are the bedrock OSS trio with thinking=high: "
    "qwen3-coder-480b (build), kimi-k2.5 (review/fix), glm-5 (reason/scout). "
    "Never assign a Claude model to an execution node."
```

- [ ] **Step 7: Fix the stale escalation comment**

`handlers.py:44-45` claims the ladder ends at Claude; it does not, and it must not. Replace:

```python
# Retry escalation: each failed attempt bumps a pi task to the next stronger
# model. OSS-only by design — a retry must never silently bill Claude tokens.
```

- [ ] **Step 8: Add the MCP-level test**

Append to `tests/test_mcp.py`:

```python
def test_tools_list_includes_wait_and_complexity():
    r = mcp.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}})
    names = {t["name"] for t in r["result"]["tools"]}
    assert "silicorism_wait" in names
    submit = next(t for t in r["result"]["tools"]
                  if t["name"] == "silicorism_plan_and_submit")
    assert "complexity" in submit["inputSchema"]["properties"]


def test_simple_complexity_submits_one_agent(tmp_path):
    dbp = str(tmp_path / "simple.db")
    r = mcp.handle({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                    "params": {"name": "silicorism_plan_and_submit",
                               "arguments": {"prompt": "build a python game",
                                             "complexity": "simple",
                                             "db": dbp, "workers": 0}}})
    payload = json.loads(r["result"]["content"][0]["text"])
    assert list(payload["tasks"]) == ["solo"]


def test_instructions_forbid_polling_and_claude_execution():
    assert "DO NOT POLL" in mcp.INSTRUCTIONS
    assert "Never assign a Claude model" in mcp.INSTRUCTIONS
```

- [ ] **Step 9: Run everything**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. `test_tools_call_plan_and_get_status` still asserts the 6-task standard pipeline and must remain green — the default tier did not change.

- [ ] **Step 10: Commit**

```bash
git add silicorism_tools.py silicorism_mcp.py handlers.py tests/test_wait.py tests/test_mcp.py
git commit -m "feat: silicorism_wait replaces the orchestrator poll loop

Also passes complexity through plan_and_submit, trims get_status payloads,
and corrects the stale comment claiming the retry ladder ends at Claude."
```

---

### Task 8: Screenshots and README

**Files:**
- Create: `docs/images/agents-grid.png`, `docs/images/dashboard.png`
- Modify: `README.md`

**Interfaces:**
- Consumes: everything above — this task runs only once Tasks 1-7 are green.

- [ ] **Step 1: Run a real DAG so there is something to photograph**

```bash
.venv/bin/python -c "
import silicorism_tools as st, db, tempfile, os
d = tempfile.mkdtemp(); p = os.path.join(d, 'demo.db'); db.init_db(p)
c = db.connect(p)
st.build_pipeline(c, p, 'demo', 'add a health check endpoint')
c.close(); print(p)
"
```

Then start the supervisor and workers against that DB path, and attach:

```bash
silicorism supervise --db <path>
SILICORISM_NATIVE=1 silicorism run --db <path> --workers 4 &
tmux attach -t silicorism-session
```

- [ ] **Step 2: Capture the two windows**

With the `agents` window on screen:

```bash
mkdir -p docs/images
maim -d 3 docs/images/agents-grid.png
```

Switch to the dashboard window (`prefix + 0`), then:

```bash
maim -d 3 docs/images/dashboard.png
```

`-d 3` gives three seconds to focus the right window. If `maim` reports no X display, capture from within tmux instead with `tmux capture-pane -p` and note in the report that image capture was unavailable — do not fake a screenshot.

- [ ] **Step 3: Update the README**

Add after the intro paragraph, and rewrite the "Live TUI panes" paragraph to describe the grid rather than per-task windows:

```markdown
![Agents grid](docs/images/agents-grid.png)

Four pi agents working in parallel, tiled in one tmux window. Panes are capped
at four per window so each TUI stays readable; a fifth agent opens `agents-2`.

![Dashboard](docs/images/dashboard.png)

Window 0 is the dashboard: the live DAG, each node's model and elapsed time,
and the P2P message feed.
```

Document the tiers in the MCP section:

```markdown
**Complexity tiers.** `silicorism_plan_and_submit` takes
`complexity: simple | standard | complex`. `simple` is one agent on
qwen3-coder-480b in the current directory with no worktree — the right shape
for a small self-contained program. `standard` is the six-task pipeline.
`complex` forks two builders into separate worktrees and rejoins them through
`worktree_integrate` plus an integrator agent.

**Waiting, not polling.** `silicorism_wait` blocks until the queue settles and
returns once, so the orchestrator spends one turn per DAG instead of one per
poll. This is the difference between planning being cheap and monitoring being
expensive.
```

- [ ] **Step 4: Verify the README references resolve**

Run: `.venv/bin/python -c "
import pathlib, re
md = pathlib.Path('README.md').read_text()
missing = [m for m in re.findall(r'!\[[^\]]*\]\(([^)]+)\)', md)
           if not pathlib.Path(m).exists()]
print('missing images:', missing)
assert not missing
"`
Expected: `missing images: []`

- [ ] **Step 5: Commit and push**

```bash
git add README.md docs/images
git commit -m "docs: grid and dashboard screenshots, tier and wait documentation"
git push
```

---

## Self-Review

**Spec coverage:** Section A → Tasks 2, 3. Section B → Task 4 (with Task 1's timestamp fix as its prerequisite). Section C simple/standard → Task 5; complex fan-out and `worktree_integrate` → Task 6. Section D token economy → Task 7. Spec's testing table → covered across Tasks 1-7; the "Migrations" row is Task 1 Step 1, "Grid fallback" is Task 3, "`silicorism_wait` caps at 3600s" is Task 7's `test_timeout_is_capped`.

**Gap found and closed:** the spec did not mention the `db.now()` format bug, which was discovered while planning Task 4's duration column. Added as Task 1, since every timing display depends on it.

**Type consistency:** `grid_pane` returns `(window, pane_id)` in Task 2 and is destructured that way in Task 3. `mark_pane_done(pane_id, *, failed)` matches its call in `worker._mark_pane`. `build_frame(tasks, messages, counts, *, width, now)` matches its call in `dashboard.frame`. `wait_for_settle`'s `settled` key is what `test_wait.py` asserts and what `_wait` serialises. `_build_complex` is called with the keyword arguments its signature declares.

**Known ordering constraint:** Task 5 introduces a `_build_complex` stub that raises `NotImplementedError`; Task 6 replaces it. Tasks 5 and 6 must not be run out of order or in parallel.
