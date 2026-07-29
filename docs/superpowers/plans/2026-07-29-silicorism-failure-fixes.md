# Silicorism Failure Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a silicorism node unable to declare its own success, and make a wedged node visible and killable in minutes instead of an hour.

**Architecture:** Five mechanisms, all inside the existing worker/db/tmux split. The worker gains a post-exit test gate and a file-mtime progress fingerprint; the fingerprint drives a stall timeout, a pane kill, and a force-fail recovery path; the wait verdict says when it timed out. No new dependency, no new process.

**Tech Stack:** Python 3 stdlib only, SQLite WAL (`db.py`), tmux via subprocess (`tmux_orchestrator.py`), pytest with `unittest.mock.patch`.

## Global Constraints

- Pure stdlib. No new dependency in `pyproject.toml`.
- Execution models are `kimi-k2.5` (build/fix) and `glm-5` (scout/reason). `qwen3-coder-480b` must never be a default or a ladder rung; it stays only as a `MODEL_ALIASES` key.
- Every module with a `if __name__ == "__main__":` self-check keeps it passing (`python handlers.py`, `python silicorism_tools.py`, `python tmux_orchestrator.py`).
- Full suite must stay green: `.venv/bin/python -m pytest -q`.
- Comments explain *why*, matching the existing house style. No comment restates the code.
- Timestamps use `db.now()` so string comparison is time comparison.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `db.py` | SQLite state | add `last_progress_at` column, `touch_progress`, `fail_stuck`, `cancel_task` |
| `worker.py` | claim → run → complete | test gate, progress fingerprint, stall stop, pane kill |
| `tmux_orchestrator.py` | tmux commands | `kill_pane`, `kill_window`, signal trap in the launch script |
| `silicorism_tools.py` | harness-agnostic bridge | `cancel_task`, wait `timed_out`/`elapsed_s`, `stalled` in status, node fields, prompt block, models |
| `silicorism_mcp.py` | MCP surface | `silicorism_cancel_task` tool, `stuck` flag on gc, schema + instruction text |
| `handlers.py` | task-type handlers | model defaults and escalation ladder |
| `tests/test_progress.py` | new | gate, fingerprint, stall, pane kill |
| `tests/test_recovery.py` | new | `fail_stuck`, `cancel_task`, gc `stuck` |
| `tests/test_wait.py` | existing | `timed_out` / `elapsed_s` |
| `tests/test_tiers.py` | existing | model defaults |

---

### Task 1: Worker-run test gate on pi nodes (F1)

**Files:**
- Modify: `worker.py` (add `_gate_command`, use it in `_run_native` around line 147-151)
- Modify: `silicorism_tools.py:303` (carry `test_command` into the node payload)
- Test: `tests/test_progress.py` (create)

**Interfaces:**
- Consumes: `handlers.verify(payload_json)` — raises `RuntimeError` on non-zero exit, returns `"verify passed: <cmd>"` on success.
- Produces: `worker._gate_command(task) -> str | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_progress.py`:

```python
"""Worker-side truth: a node cannot declare its own success, and a node that
stops making progress is failed instead of held for an hour."""

import json
from unittest.mock import patch

import db


def _task(conn, payload):
    tid = db.add_task(conn, "pi", json.dumps(payload))
    return tid, conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


@patch("worker.tmux")
def test_gate_failure_fails_the_task(mock_tmux, tmp_path):
    import worker
    dbp = str(tmp_path / "gate.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    mock_tmux.sentinel_path.side_effect = lambda tid: str(tmp_path / f"{tid}.exit")
    mock_tmux.log_path.side_effect = lambda tid: str(tmp_path / f"{tid}.log")
    mock_tmux.grid_pane.return_value = ("agents", "%1")
    mock_tmux.read_log_tail.return_value = "agent says it is done"
    mock_tmux.wait_for_exit.return_value = 0

    tid, task = _task(conn, {"prompt": "x", "cwd": str(tmp_path),
                             "test_command": "false"})
    try:
        worker._run_native(conn, task, "w0", "pi 'x'")
    except RuntimeError as err:
        assert "verify failed" in str(err)
    else:
        raise AssertionError("a failing gate must fail the task")
    # the agent exited 0 and still did not get to complete its own task
    assert db.counts(conn)["completed"] == 0
    conn.close()


@patch("worker.tmux")
def test_gate_pass_records_the_evidence(mock_tmux, tmp_path):
    import worker
    dbp = str(tmp_path / "gate2.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    mock_tmux.sentinel_path.side_effect = lambda tid: str(tmp_path / f"{tid}.exit")
    mock_tmux.log_path.side_effect = lambda tid: str(tmp_path / f"{tid}.log")
    mock_tmux.grid_pane.return_value = ("agents", "%1")
    mock_tmux.read_log_tail.return_value = "agent summary"
    mock_tmux.wait_for_exit.return_value = 0

    tid, task = _task(conn, {"prompt": "x", "cwd": str(tmp_path),
                             "test_command": "true"})
    worker._run_native(conn, task, "w0", "pi 'x'")
    art = conn.execute("SELECT output_artifact FROM tasks WHERE id=?",
                       (tid,)).fetchone()["output_artifact"]
    assert db.counts(conn)["completed"] == 1
    assert "verify passed: true" in art
    conn.close()


def test_dag_node_carries_test_command(tmp_path):
    import silicorism_tools
    dbp = str(tmp_path / "dag.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    dag = silicorism_tools.build_dag(conn, dbp, [
        {"id": "build", "prompt": "do it", "test_command": "npm test"}],
        cwd=str(tmp_path))
    payload = json.loads(conn.execute(
        "SELECT payload FROM tasks WHERE id=?",
        (dag["nodes"]["build"],)).fetchone()["payload"])
    assert payload["test_command"] == "npm test"
    conn.close()
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_progress.py -q`
Expected: three failures — the task completes despite `false`, no `verify passed` in the artifact, `KeyError: 'test_command'`.

- [ ] **Step 3: Add the gate to the worker**

In `worker.py`, after `_native_payload` (around line 57) add:

```python
def _gate_command(task) -> str | None:
    """A pi node's own acceptance test, run by the worker — not by the agent.

    The pane's exit code says the agent process ended, not that the work is
    correct: autoexit.ts exits 0 for any run that settled without an error stop
    reason, so an agent that ran nothing still "succeeds".
    """
    try:
        data = json.loads(task["payload"] or "{}")
    except (json.JSONDecodeError, ValueError):
        return None
    return data.get("test_command") if isinstance(data, dict) else None
```

In `_run_native`, replace the artifact/complete block (currently lines 147-151):

```python
        # Prefer the clean autoexit artifact; fall back to the raw log tail.
        artifact = (tmux.read_log_tail(_artifact_path(tid), max_chars=4000)
                    or tmux.read_log_tail(logf)
                    or f"native pane {win} exit 0")
        gate = _gate_command(task)
        if gate:
            # Raises on non-zero, so the except below fails the task: this is
            # the only thing standing between a claimed pass and a real one.
            artifact += "\n\n" + handlers.verify(json.dumps(
                {"test_command": gate, "cwd": _task_cwd(task)}))
        db.complete_task(conn, tid, artifact=artifact)
```

- [ ] **Step 4: Carry `test_command` through the node schema**

In `silicorism_tools.build_dag`, extend the payload copy loop (line 303):

```python
            for key in ("model", "thinking", "skills", "test_command",
                        "timeout_s", "stall_timeout_s"):
                if n.get(key):
                    payload[key] = n[key]
```

(`timeout_s` / `stall_timeout_s` are consumed in Task 3; carrying them now keeps the schema in one place.)

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_progress.py tests/test_workflow.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add worker.py silicorism_tools.py tests/test_progress.py
git commit -m "fix: a node's own tests decide whether it completed, not the agent"
```

---

### Task 2: Progress fingerprint (F2)

**Files:**
- Modify: `db.py` (`_SCHEMA` tasks table, `_MIGRATIONS`, new `touch_progress`)
- Modify: `worker.py` (`newest_mtime`, `_stop_and_beat` signature and body, its call site)
- Modify: `silicorism_tools.py` (`get_status` gains `stalled`)
- Test: `tests/test_progress.py`

**Interfaces:**
- Produces: `db.touch_progress(conn, task_id) -> None`; `worker.newest_mtime(root: str) -> float`; `get_status()["stalled"] -> list[{"id", "agent_id", "last_progress_at", "idle_s"}]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_progress.py`:

```python
def test_newest_mtime_ignores_noise_dirs(tmp_path):
    import os
    import time
    import worker
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    base = worker.newest_mtime(str(tmp_path))
    junk = tmp_path / "node_modules"
    junk.mkdir()
    (junk / "b.js").write_text("y")
    os.utime(junk / "b.js", (time.time() + 500, time.time() + 500))
    assert worker.newest_mtime(str(tmp_path)) == base
    (tmp_path / "src" / "c.py").write_text("z")
    assert worker.newest_mtime(str(tmp_path)) > base


def test_beat_stamps_progress_only_when_files_change(tmp_path):
    import worker
    dbp = str(tmp_path / "beat.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("1")
    tid, _ = _task(conn, {"prompt": "x", "cwd": str(work)})
    db.claim_task(conn, "w0")

    poll = worker._stop_and_beat(conn, "w0", tid, str(work), every=0.0)
    assert poll() is False
    first = conn.execute("SELECT last_progress_at FROM tasks WHERE id=?",
                         (tid,)).fetchone()["last_progress_at"]
    assert first is not None
    assert poll() is False
    same = conn.execute("SELECT last_progress_at FROM tasks WHERE id=?",
                        (tid,)).fetchone()["last_progress_at"]
    assert same == first, "an unchanged tree must not look like progress"

    (work / "b.txt").write_text("2")
    assert poll() is False
    moved = conn.execute("SELECT last_progress_at FROM tasks WHERE id=?",
                         (tid,)).fetchone()["last_progress_at"]
    assert moved > first
    conn.close()


def test_status_reports_stalled_tasks(tmp_path):
    import silicorism_tools
    dbp = str(tmp_path / "stall.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", json.dumps({"prompt": "x"}))
    db.claim_task(conn, "w0")
    conn.execute("UPDATE tasks SET last_progress_at=? WHERE id=?",
                 ("2020-01-01T00:00:00.000Z", tid))
    stalled = silicorism_tools.get_status(conn)["stalled"]
    assert [s["id"] for s in stalled] == [tid]
    assert stalled[0]["idle_s"] > 60
    conn.close()
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_progress.py -q -k "mtime or beat or stalled"`
Expected: `AttributeError: module 'worker' has no attribute 'newest_mtime'` and `KeyError: 'stalled'`.

- [ ] **Step 3: Add the column and the stamp**

In `db.py`, add `last_progress_at TEXT,` to the `tasks` table in `_SCHEMA` (next to `started_at`, with the comment `-- stamped when the task's files last changed`), and add `("last_progress_at", "TEXT"),` to `_MIGRATIONS`.

Add next to `complete_task`:

```python
def touch_progress(conn, task_id) -> None:
    """Stamp observable progress: the task's files changed since the last beat.

    Distinct from the heartbeat on purpose. A heartbeat says the worker process
    is alive; a live worker blocked on a wedged agent beats `busy` for an hour
    while nothing is written. This is the column that can tell them apart.
    """
    with immediate(conn) as c:
        c.execute("UPDATE tasks SET last_progress_at=? WHERE id=?",
                  (now(), task_id))
```

- [ ] **Step 4: Fingerprint the tree in the worker**

In `worker.py`, above `_stop_and_beat`:

```python
# Directories an agent's work never shows up in; walking node_modules every
# beat would cost more than the signal is worth.
_SKIP_DIRS = {"node_modules", "__pycache__", "venv", "dist", "build", "target"}


def newest_mtime(root: str) -> float:
    """Newest mtime under `root`, or 0.0 if it cannot be read.

    The progress signal has to distinguish drawing from working: tmux
    pipe-pane records every repaint, so a spinning TUI grows its log forever
    while the agent is wedged. Files on disk do not lie about that.

    ponytail: a full walk every 30s; swap for inotify if it ever profiles.
    """
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            try:
                m = os.stat(os.path.join(dirpath, name)).st_mtime
            except OSError:  # raced deletion, broken symlink
                continue
            newest = max(newest, m)
    return newest
```

Replace `_stop_and_beat` with (docstring keeps the existing double-claim history):

```python
def _stop_and_beat(conn, agent_id, task_id, cwd, *, every: float = 30.0):
    """Poll callback for wait_for_exit: stop flag, heartbeat AND progress.

    _run_native blocks for the agent's whole run, so without a beat here the
    worker's last_seen freezes at claim time; after 300s db.reap_stale
    (db.py, called from every silicorism_wait loop) hands the task to a second
    worker, which launches a second live agent in the same directory.
    Seen for real: task 2 claimed by worker-0 at 09:30:02 and again by
    worker-1 at 09:35:03, two panes editing the same files.

    The same tick fingerprints the task's tree, because a beat on its own
    cannot tell a working agent from a wedged one.
    """
    state = {"next": 0.0, "mtime": None}

    def poll() -> bool:
        if _STOP:
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
            try:
                db.touch_progress(conn, task_id)
            except Exception:  # noqa: BLE001
                pass
        return False

    return poll
```

Update the call site in `_run_native`:

```python
        code = tmux.wait_for_exit(
            sentinel, stop=_stop_and_beat(conn, agent_id, tid, _task_cwd(task)))
```

- [ ] **Step 5: Surface it in the status**

In `silicorism_tools.py`, above `get_status`:

```python
def _stalled(conn, *, idle_s: float = 300.0) -> list[dict]:
    """Processing tasks whose files have not changed for `idle_s`.

    `busy` with a fresh heartbeat looked exactly like healthy progress for an
    hour of wall clock; this is the row that makes it machine-detectable.
    """
    rows = conn.execute(
        "SELECT id, agent_id, last_progress_at, started_at FROM tasks "
        "WHERE status='processing'").fetchall()
    out = []
    for r in rows:
        stamp = r["last_progress_at"] or r["started_at"]
        if not stamp:
            continue
        try:
            seen = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        idle = (datetime.now(timezone.utc) - seen).total_seconds()
        if idle >= idle_s:
            out.append({"id": r["id"], "agent_id": r["agent_id"],
                        "last_progress_at": stamp, "idle_s": round(idle)})
    return out
```

Add `from datetime import datetime, timezone` to the imports, and add `"stalled": _stalled(conn),` to the dict returned by `get_status`.

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (the whole suite — `_stop_and_beat` changed signature, so `tests/test_grid.py` and `tests/test_workflow.py` must still pass).

- [ ] **Step 7: Commit**

```bash
git add db.py worker.py silicorism_tools.py tests/test_progress.py
git commit -m "feat: a progress fingerprint that tells a working agent from a wedged one"
```

---

### Task 3: Stall timeout (F3)

**Files:**
- Modify: `worker.py` (`AgentAlive`, stall in the poll callback, per-node timeouts)
- Test: `tests/test_progress.py`

**Interfaces:**
- Consumes: `worker.newest_mtime`, `db.touch_progress` from Task 2.
- Produces: `worker.AgentAlive(RuntimeError)`; `_stop_and_beat(conn, agent_id, task_id, cwd, *, every=30.0, stall_s=STALL_TIMEOUT_S, reason=None)`; `worker.STALL_TIMEOUT_S = 600.0`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_progress.py`:

```python
def _stopping_wait(sentinel, **kw):
    """Stand-in for tmux.wait_for_exit: polls stop() and returns None when it fires.

    Returning `None` is what the real function does on stop/timeout, and that
    None is what _run_native turns into a stall or a timeout.
    """
    import time
    for _ in range(50):
        if kw["stop"]():
            return None
        time.sleep(0.01)
    raise AssertionError("stop() never fired")


def test_poll_stops_and_reports_a_stall(tmp_path):
    import time
    import worker
    dbp = str(tmp_path / "st.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    work = tmp_path / "w"
    work.mkdir()
    (work / "a.txt").write_text("1")
    tid, _ = _task(conn, {"prompt": "x", "cwd": str(work)})
    db.claim_task(conn, "w0")

    reason = {}
    poll = worker._stop_and_beat(conn, "w0", tid, str(work), every=0.0,
                                 stall_s=0.05, reason=reason)
    assert poll() is False, "the first tick establishes the baseline"
    time.sleep(0.06)
    assert poll() is True, "no new files past the stall window is a stall"
    assert reason["stalled"] >= 0.05
    conn.close()


@patch("worker.tmux")
def test_stalled_run_fails_with_its_reason(mock_tmux, tmp_path):
    import worker
    dbp = str(tmp_path / "st2.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    mock_tmux.sentinel_path.side_effect = lambda tid: str(tmp_path / f"{tid}.exit")
    mock_tmux.log_path.side_effect = lambda tid: str(tmp_path / f"{tid}.log")
    mock_tmux.grid_pane.return_value = ("agents", "%7")
    mock_tmux.wait_for_exit.side_effect = _stopping_wait

    tid, task = _task(conn, {"prompt": "x", "cwd": str(tmp_path),
                             "stall_timeout_s": 0.05})
    try:
        worker._run_native(conn, task, "w0", "pi 'x'")
    except worker.AgentAlive as err:
        assert "stalled" in str(err)
    else:
        raise AssertionError("a stalled agent must raise AgentAlive")
    conn.close()
```

Note on the guard: `stall_s <= 0` means "no stall detection" (the escape hatch for a node that legitimately writes nothing), so tests use a small positive window, never `0.0`.

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_progress.py -q -k stall`
Expected: `TypeError: _stop_and_beat() got an unexpected keyword argument 'stall_s'`.

- [ ] **Step 3: Implement the stall**

In `worker.py`, near the module constants:

```python
# A node that hangs at minute 2 used to hold its worker until minute 60: the
# 3600s cap in tmux.wait_for_exit is a ceiling, not a stall detector.
STALL_TIMEOUT_S = float(os.environ.get("SILICORISM_STALL_S") or 600)


class AgentAlive(RuntimeError):
    """The run ended while the agent was still running (stall or timeout).

    Distinct from a non-zero exit: there is still a live process and a live
    pane to kill, and the pane is not a post-mortem — it is a leak.
    """
```

Extend `_stop_and_beat`'s signature to `(conn, agent_id, task_id, cwd, *, every=30.0, stall_s=STALL_TIMEOUT_S, reason=None)`, track the last progress time, and return `True` on a stall:

```python
    state = {"next": 0.0, "mtime": None, "progress": time.monotonic()}

    def poll() -> bool:
        if _STOP:
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
        if stall_s > 0 and idle >= stall_s:  # <=0 disables stall detection
            if reason is not None:
                reason["stalled"] = idle
            return True
        return False
```

- [ ] **Step 4: Wire per-node timeouts into `_run_native`**

Add a payload reader beside `_gate_command`:

```python
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
```

and use it where `wait_for_exit` is called:

```python
        cap, stall_s = _timeouts(task)
        reason: dict = {}
        code = tmux.wait_for_exit(
            sentinel, timeout=cap,
            stop=_stop_and_beat(conn, agent_id, tid, _task_cwd(task),
                                stall_s=stall_s, reason=reason))
        if code != 0:
            if reason.get("stalled"):
                raise AgentAlive("native agent stalled: no progress for "
                                 f"{int(reason['stalled'])}s")
            if code is None:
                raise AgentAlive(f"native agent exit timeout ({int(cap)}s)")
            raise RuntimeError(f"native agent exit {code}")
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS. `tests/test_workflow.py::test_worker_native_completes_and_fails` asserts `"exit 3" in str(e)` — the new message still contains it.

- [ ] **Step 6: Commit**

```bash
git add worker.py tests/test_progress.py
git commit -m "fix: fail a node that stops making progress instead of holding it for an hour"
```

---

### Task 4: Pane and process hygiene (F4, F10)

**Files:**
- Modify: `tmux_orchestrator.py` (`kill_pane`, `kill_window`, trap in `_launch_script`, its self-check)
- Modify: `worker.py` (`_kill_pane`, kill on `AgentAlive`, kill on success, db-slug pane label)
- Test: `tests/test_progress.py`

**Interfaces:**
- Consumes: `worker.AgentAlive` from Task 3.
- Produces: `tmux.kill_pane(pane_id: str) -> None`; `tmux.kill_window(name: str, *, session=SESSION) -> None`; `worker._db_slug(path: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_progress.py`:

```python
def test_launch_script_kills_its_children(tmp_path):
    import tmux_orchestrator as tmux
    script = tmux._launch_script("42", "sleep 5", str(tmp_path / "s.exit"))
    body = open(script.split(" ", 1)[1].strip("'"), encoding="utf-8").read()
    # the pane's process group, so gopls/pyright die with the pane
    assert "kill -TERM 0" in body and "trap" in body, body


def test_pane_label_names_the_run(tmp_path):
    import worker
    assert worker._db_slug("/home/me/Projects/splice/.git/silicorism.db") == "splice"
    task = {"payload": json.dumps({"agent_id": "step4",
                                   "db": "/home/me/Projects/splice/.git/x.db"}),
            "task_type": "pi", "id": 3}
    assert worker._pane_label(task) == "splice/step4"


@patch("worker.tmux")
def test_stall_kills_the_pane_and_clean_exit_does_not(mock_tmux, tmp_path):
    import worker
    dbp = str(tmp_path / "kill.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    mock_tmux.sentinel_path.side_effect = lambda tid: str(tmp_path / f"{tid}.exit")
    mock_tmux.log_path.side_effect = lambda tid: str(tmp_path / f"{tid}.log")
    mock_tmux.grid_pane.return_value = ("agents", "%9")

    # a non-zero exit: the process is already dead, keep the pane for a look
    tid, task = _task(conn, {"prompt": "x", "cwd": str(tmp_path)})
    mock_tmux.wait_for_exit.return_value = 3
    try:
        worker._run_native(conn, task, "w0", "pi 'x'")
    except RuntimeError:
        pass
    mock_tmux.kill_pane.assert_not_called()

    # a stall: the agent is still live, so the pane and its children must go
    tid2, task2 = _task(conn, {"prompt": "y", "cwd": str(tmp_path),
                               "stall_timeout_s": 0.05})
    mock_tmux.wait_for_exit.side_effect = _stopping_wait
    try:
        worker._run_native(conn, task2, "w0", "pi 'y'")
    except worker.AgentAlive:
        pass
    mock_tmux.kill_pane.assert_called_once_with("%9")
    conn.close()
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_progress.py -q -k "launch_script or pane_label or kills_the_pane"`
Expected: no `trap` in the script body; `AttributeError: module 'worker' has no attribute '_db_slug'`.

- [ ] **Step 3: Trap signals in the launch script and add the kill commands**

In `tmux_orchestrator.py`, `_launch_script` writes:

```python
    with open(path, "w", encoding="utf-8") as fh:
        # kill-pane hangs up the shell but leaves its grandchildren (gopls,
        # pyright-langserver) resident. tmux gives each pane its own process
        # group, so signalling group 0 takes the whole tree down; `trap -`
        # first so the handler cannot re-enter itself.
        fh.write("trap 'trap - TERM; kill -TERM 0' HUP INT TERM\n")
        fh.write(f"{command}\necho $? > {tmp}\nmv {tmp} {fin}\n")
```

and extend its docstring's last paragraph to mention the trap. Add beside `mark_pane_done`:

```python
def kill_pane(pane_id: str) -> None:
    """Close a pane for good — used when the agent in it is still running."""
    _tmux("kill-pane", "-t", pane_id)


def kill_window(name: str, *, session: str = SESSION) -> None:
    """Close a whole task window (the no-grid fallback path)."""
    _tmux("kill-window", "-t", _window_target(session, name))
```

In the module self-check, add after the existing `body` assertions:

```python
    assert "kill -TERM 0" in body, body
```

- [ ] **Step 4: Kill panes from the worker**

In `worker.py`, add beside `_mark_pane`:

```python
def _kill_pane(pane, window=None) -> None:
    """Close a finished pane; best-effort, the run's verdict never depends on it."""
    try:
        if pane:
            tmux.kill_pane(pane)
        elif window:
            tmux.kill_window(window)
    except Exception:  # noqa: BLE001
        pass
```

In `_run_native`'s `except` branch, kill only when the agent may still be alive:

```python
    except Exception as err:
        _mark_pane(tid, pane, failed=True, window=win)
        if isinstance(err, AgentAlive):
            # A timed-out pane leaks the agent plus everything it spawned;
            # a plain non-zero exit is already dead and its scrollback is the
            # post-mortem, so that one is kept.
            tmux.trim_log(logf)
            _kill_pane(pane, win)
        raise
```

and at the successful tail, replace `_mark_pane(tid, pane, failed=False, window=win)` with:

```python
    _mark_pane(tid, pane, failed=False, window=win)
    # The artifact is already captured; a session that keeps every DONE pane
    # becomes impossible to read after a few runs.
    _kill_pane(pane, win)
```

- [ ] **Step 5: Label panes with the run**

Replace `_pane_label` with:

```python
def _db_slug(path: str) -> str:
    """Repo-ish name for a db path: /repo/.git/silicorism.db -> repo."""
    parts = [p for p in os.path.dirname(os.path.abspath(path)).split(os.sep)
             if p and p != ".git"]
    return (parts[-1] if parts else "db")[:12]


def _pane_label(task) -> str:
    """"<run>/<agent id>" — a long-lived session holds panes from many runs."""
    data = {}
    try:
        loaded = json.loads(task["payload"] or "{}")
        if isinstance(loaded, dict):
            data = loaded
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    label = data.get("agent_id") or f"{task['task_type']}-{task['id']}"
    return f"{_db_slug(data['db'])}/{label}" if data.get("db") else str(label)
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python tmux_orchestrator.py`
Expected: suite PASS, then `tmux_orchestrator OK`.

- [ ] **Step 7: Commit**

```bash
git add tmux_orchestrator.py worker.py tests/test_progress.py
git commit -m "fix: kill the pane and its children when the agent is still running"
```

---

### Task 5: Recovery from a wedged run (F5)

**Files:**
- Modify: `db.py` (`fail_stuck`, `cancel_task`, shared `_cutoff` helper)
- Modify: `silicorism_tools.py` (`cancel_task` wrapper)
- Modify: `silicorism_mcp.py` (`stuck` flag on `_gc`, new `silicorism_cancel_task` tool)
- Test: `tests/test_recovery.py` (create)

**Interfaces:**
- Consumes: `last_progress_at` from Task 2; `tmux.kill_pane` / `tmux.kill_window` from Task 4.
- Produces: `db.fail_stuck(conn, *, older_than_s=300.0) -> list[int]`; `db.cancel_task(conn, task_id) -> str | None` (the pane target); `silicorism_tools.cancel_task(conn, task_id) -> dict`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_recovery.py`:

```python
"""There has to be a way out of a wedged pipeline that is not "throw the DB away"."""

import json

import db
import silicorism_tools


def _stamp(conn, tid, column, value):
    conn.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, tid))


OLD = "2020-01-01T00:00:00.000Z"


def test_fail_stuck_clears_a_wedged_task(tmp_path):
    dbp = str(tmp_path / "stuck.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    wedged = db.add_task(conn, "pi", json.dumps({"prompt": "x"}))
    live = db.add_task(conn, "pi", json.dumps({"prompt": "y"}))
    db.claim_task(conn, "w0")
    db.claim_task(conn, "w1")
    db.heartbeat(conn, "w0", "busy", wedged)   # fresh beat, wedged agent
    db.heartbeat(conn, "w1", "busy", live)
    _stamp(conn, wedged, "last_progress_at", OLD)
    db.touch_progress(conn, live)

    assert db.fail_stuck(conn) == [wedged]
    rows = {r["id"]: r["status"] for r in db.all_tasks(conn)}
    assert rows[wedged] == "failed" and rows[live] == "processing"
    # now terminal, so the existing pruner can finally clear it
    assert silicorism_tools.prune_tasks(conn)["deleted"] == 1
    conn.close()


def test_fail_stuck_also_catches_a_dead_worker(tmp_path):
    dbp = str(tmp_path / "dead.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", json.dumps({"prompt": "x"}))
    db.claim_task(conn, "w0")
    db.touch_progress(conn, tid)               # progress fresh...
    conn.execute("UPDATE agent_heartbeats SET last_seen=? WHERE agent_id=?",
                 (OLD, "w0"))                  # ...but the worker is gone
    assert db.fail_stuck(conn) == [tid]
    conn.close()


def test_cancel_task_is_unconditional(tmp_path):
    dbp = str(tmp_path / "cancel.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", json.dumps({"prompt": "x"}))
    db.claim_task(conn, "w0")
    db.set_pane_target(conn, tid, "agents.%3")
    killed = []
    out = silicorism_tools.cancel_task(conn, tid, _kill=killed.append)
    assert out["cancelled"] is True and killed == ["%3"]
    assert db.counts(conn)["failed"] == 1
    assert silicorism_tools.cancel_task(conn, 999)["cancelled"] is False
    conn.close()
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_recovery.py -q`
Expected: `AttributeError: module 'db' has no attribute 'fail_stuck'`.

- [ ] **Step 3: Implement the db side**

In `db.py`, factor the cutoff `reap_stale` already computes:

```python
def _cutoff(older_than_s: float) -> str:
    """Timestamp `older_than_s` in the past, in now()'s format.

    Same format as now(), so the string comparison is a time comparison.
    """
    return (datetime.now(timezone.utc) - timedelta(seconds=older_than_s)
            ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
```

Use it inside `reap_stale` (replacing its inline computation), then add:

```python
def fail_stuck(conn, *, older_than_s: float = 300.0) -> list[int]:
    """Force-fail processing tasks that are not going anywhere. Returns their ids.

    reap_stale only sees a dead heartbeat, and a live worker blocked on a
    wedged agent keeps beating `busy`. That row is not terminal, so
    silicorism_gc(tasks=True) cannot prune it either, and it poisons every
    later wait verdict for that DB. Stale by heartbeat OR by progress is
    enough here: this is a manual recovery, so it skips the retry ladder.
    """
    cutoff = _cutoff(older_than_s)
    with immediate(conn) as c:
        ids = [r["id"] for r in c.execute(
            "SELECT t.id FROM tasks t "
            "LEFT JOIN agent_heartbeats h ON h.agent_id = t.agent_id "
            "WHERE t.status='processing' AND ("
            "  COALESCE(t.last_progress_at, t.started_at, t.updated_at) < ? "
            "  OR COALESCE(h.last_seen, '') < ?)",
            (cutoff, cutoff))]
        if ids:
            marks = ",".join("?" * len(ids))
            c.execute(
                f"UPDATE tasks SET status='failed', agent_id=NULL, updated_at=? "
                f"WHERE id IN ({marks})", [now(), *ids])
    return ids


def cancel_task(conn, task_id) -> str | None:
    """Force one task terminal whatever its state; returns its pane target.

    The surgical version of fail_stuck, for the task the operator can name.
    """
    with immediate(conn) as c:
        row = c.execute("SELECT pane_target FROM tasks WHERE id=?",
                        (task_id,)).fetchone()
        if row is None:
            return None
        c.execute("UPDATE tasks SET status='failed', agent_id=NULL, "
                  "updated_at=? WHERE id=?", (now(), task_id))
    return row["pane_target"] or ""
```

- [ ] **Step 4: Wrap it in the bridge**

In `silicorism_tools.py`, add `import tmux_orchestrator as tmux` and:

```python
def cancel_task(conn, task_id, *, _kill=None) -> dict:
    """Fail a named task and close its pane. `_kill` is injected by tests."""
    pane = db.cancel_task(conn, task_id)
    if pane is None:
        return {"cancelled": False, "reason": f"no task {task_id}"}
    db.log(conn, task_id, "operator", "cancelled by operator", level="error")
    killed = False
    if pane:
        target = pane.rsplit(".", 1)[-1] if "%" in pane else pane
        kill = _kill or (tmux.kill_pane if "%" in pane else tmux.kill_window)
        try:
            kill(target)
            killed = True
        except Exception:  # noqa: BLE001 - the pane may already be gone
            pass
    return {"cancelled": True, "task_id": task_id, "pane_killed": killed}
```

- [ ] **Step 5: Expose both on the MCP surface**

In `silicorism_mcp.py`, extend `_gc` (before the `tasks` prune, so the pruner sees the new terminal rows):

```python
        out = silicorism_tools.gc_worktrees(
            conn, dbp, failed=bool(args.get("failed")))
        if args.get("stuck"):
            out["stuck"] = db.fail_stuck(conn)
        if args.get("tasks"):
            out["tasks"] = silicorism_tools.prune_tasks(conn)
```

Add `"stuck"` to the gc input schema:

```python
                "stuck": {"type": "boolean",
                          "description": "force-fail processing tasks whose "
                                         "worker or files have stopped moving, "
                                         "so a wedged run stops poisoning the "
                                         "wait verdict"},
```

and update the gc `description` string (both the tool entry and `_gc`'s docstring) to end with `; stuck=true force-fails wedged processing tasks`.

Add a handler and a tool entry:

```python
def _cancel_task(args: dict) -> str:
    """Force one task terminal and kill its pane (the wedged-run escape hatch)."""
    dbp = _db(args)
    db.init_db(dbp)
    conn = db.connect(dbp)
    try:
        return json.dumps(silicorism_tools.cancel_task(
            conn, int(args["task_id"])))
    finally:
        conn.close()
```

```python
    {
        "name": "silicorism_cancel_task",
        "description": "Force one task to 'failed' whatever its state and kill "
                       "its tmux pane. Use on a task silicorism_get_status "
                       "reports in 'stalled', then silicorism_gc(tasks=true) "
                       "to prune it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "db": {"type": "string"},
            },
            "required": ["task_id"],
        },
        "handler": _cancel_task,
    },
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (`tests/test_mcp.py` walks the `TOOLS` list; a new entry with the same shape keeps it green).

- [ ] **Step 7: Commit**

```bash
git add db.py silicorism_tools.py silicorism_mcp.py tests/test_recovery.py
git commit -m "feat: clear a wedged task instead of abandoning the whole database"
```

---

### Task 6: Honest wait verdict (F6)

**Files:**
- Modify: `silicorism_tools.py:387` (`wait_for_settle`)
- Modify: `silicorism_mcp.py` (wait tool description)
- Test: `tests/test_wait.py`

**Interfaces:**
- Produces: `wait_for_settle` return dict gains `timed_out: bool` and `elapsed_s: float`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_wait.py`:

```python
def test_wait_says_when_it_timed_out(tmp_path):
    dbp = str(tmp_path / "to.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    db.add_task(conn, "sleep", "5")          # stays pending: never settles
    out = silicorism_tools.wait_for_settle(conn, timeout_s=1, poll=0.1)
    assert out["settled"] is False and out["timed_out"] is True
    assert out["elapsed_s"] >= 1

    db.complete_task(conn, 1, artifact="done")
    out2 = silicorism_tools.wait_for_settle(conn, timeout_s=5, poll=0.1)
    assert out2["settled"] is True and out2["timed_out"] is False
    conn.close()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_wait.py -q -k timed_out`
Expected: `KeyError: 'timed_out'`.

- [ ] **Step 3: Implement**

In `wait_for_settle`, record the start and stamp both exits:

```python
    started = time.monotonic()
    deadline = started + min(max(float(timeout_s), 1.0), WAIT_CAP_S)
    already_failed = {f["id"] for f in verify_status(conn)["failures"]}
    while True:
        db.reap_stale(conn)
        verdict = verify_status(conn)
        fresh = [f for f in verdict["failures"] if f["id"] not in already_failed]
        if verdict["active"] == 0 or fresh:
            verdict.update(settled=True, timed_out=False,
                           elapsed_s=round(time.monotonic() - started, 1))
            return verdict
        if (stop and stop()) or time.monotonic() >= deadline:
            # A timeout used to be shape-identical to a real verdict; the only
            # discriminator was settled=false, which is easy to skim past.
            verdict.update(settled=False,
                           timed_out=time.monotonic() >= deadline,
                           elapsed_s=round(time.monotonic() - started, 1))
            return verdict
        time.sleep(poll)
```

- [ ] **Step 4: Say what to do about it**

In `silicorism_mcp.py`, the `silicorism_wait` tool description becomes:

```python
        "description": "Block until the queue settles (all tasks terminal, or "
                       "any task failed), then return the verdict once. Use "
                       "this instead of polling silicorism_get_status. On "
                       "'timed_out': true nothing settled — read "
                       "silicorism_get_status's 'stalled' list, cancel a wedged "
                       "task with silicorism_cancel_task, then call wait again.",
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add silicorism_tools.py silicorism_mcp.py tests/test_wait.py
git commit -m "fix: a wait that timed out must not look like a verdict"
```

---

### Task 7: Model policy and prompt hardening (F8, F11)

**Files:**
- Modify: `handlers.py:23,46-50,477-482` (default model, escalation ladder, self-check)
- Modify: `silicorism_tools.py:18-29,200,528` (role defaults, simple tier, docstring, self-check)
- Modify: `silicorism_mcp.py:70,248` (instruction text, model description)
- Modify: `skills/silicorism/SKILL.md:26`, `.claude/commands/silicorism.md:20`, `README.md:91,102,122`
- Modify: `tests/test_tiers.py:37-42`, `tests/test_dashboard.py:28-29`
- Test: `tests/test_progress.py` (the deliverables block)

**Interfaces:**
- Produces: `silicorism_tools.DELIVERABLES` (the constant appended to every pi node prompt).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_progress.py`:

```python
def test_every_pi_node_must_paste_its_evidence(tmp_path):
    import silicorism_tools
    dbp = str(tmp_path / "deliv.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    dag = silicorism_tools.build_dag(conn, dbp, [
        {"id": "a", "prompt": "do it"},
        {"id": "gate", "harness": "verify", "test_command": "true",
         "depends_on": ["a"]}], cwd=str(tmp_path))
    pi_payload = json.loads(conn.execute(
        "SELECT payload FROM tasks WHERE id=?",
        (dag["nodes"]["a"],)).fetchone()["payload"])
    assert "do it" in pi_payload["prompt"]
    assert "verbatim output" in pi_payload["prompt"]
    # a gate has no prompt to harden
    gate_payload = json.loads(conn.execute(
        "SELECT payload FROM tasks WHERE id=?",
        (dag["nodes"]["gate"],)).fetchone()["payload"])
    assert "prompt" not in gate_payload
    conn.close()


def test_no_default_uses_a_banned_model(tmp_path):
    import handlers
    import silicorism_tools
    dbp = str(tmp_path / "models.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    silicorism_tools.build_pipeline(conn, dbp, "x", "add auth")
    silicorism_tools.build_pipeline(conn, dbp, "y", "add auth",
                                    complexity="simple", cwd=str(tmp_path))
    payloads = [r["payload"] for r in
                conn.execute("SELECT payload FROM tasks").fetchall()]
    assert not [p for p in payloads if "qwen" in (p or "")]
    assert "qwen" not in handlers.DEFAULT_PI_MODEL
    assert not [m for m in handlers.ESCALATION if "qwen" in m]
    conn.close()
```

Update `tests/test_tiers.py:37-42` in the same step — rename it and change the expected id:

```python
def test_simple_pins_kimi(tmp_path):
```

with the assertion becoming `"bedrock-mantle/moonshotai.kimi-k2.5"`.

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_progress.py tests/test_tiers.py -q`
Expected: `qwen` still present in payloads; no `verbatim output` in the prompt.

- [ ] **Step 3: Move the defaults off qwen**

`handlers.py`:

```python
DEFAULT_PI_MODEL = "bedrock-mantle/moonshotai.kimi-k2.5"
```

```python
# Retry escalation: each failed attempt bumps a pi task to the next stronger
# model. OSS-only by design — a retry must never silently bill Claude tokens.
# qwen3-coder-480b is deliberately absent: it stays reachable by name in
# MODEL_ALIASES, but nothing routes onto it by default.
ESCALATION = [
    "bedrock-mantle/moonshotai.kimi-k2.5",
    "bedrock-mantle/zai.glm-5",
]
```

Fix the self-check at the bottom of `handlers.py`:

```python
    # escalation ladder: kimi -> glm -> None; non-pi untouched
    p1 = escalate_payload("pi", json.dumps({"prompt": "x", "model": "kimi-k2.5"}))
    assert json.loads(p1)["model"] == "bedrock-mantle/zai.glm-5"
    assert escalate_payload("pi", p1) is None
    assert escalate_payload("shell", "echo hi") is None
```

`silicorism_tools.py`:

```python
# Default per-role models: the OSS pair on bedrock-mantle, matched to role
# strengths — glm-5 reasons (scout), kimi-k2.5 builds and reviews.
DEFAULT_MODELS = {
    "scout": "bedrock-mantle/zai.glm-5",
    "builder": "bedrock-mantle/moonshotai.kimi-k2.5",
    "fixer": "bedrock-mantle/moonshotai.kimi-k2.5",
}
```

```python
SIMPLE_MODEL = "bedrock-mantle/moonshotai.kimi-k2.5"
```

Update the `simple` line in `build_pipeline`'s docstring to `one agent (kimi-k2.5) in cwd, verify iff test_command`, and the self-check assertion at line 528 to `"bedrock-mantle/moonshotai.kimi-k2.5"`.

`tests/test_dashboard.py:28-29` keeps testing `short_model`; leave the qwen id there — it exercises the string trimmer, not a default.

- [ ] **Step 4: Add the deliverables block**

In `silicorism_tools.py`, above `build_dag`:

```python
# Appended to every pi node prompt. Each line answers an observed failure: an
# agent that reported a green suite while its own test failed, one that dropped
# a config value the prompt told it to choose, and one that summarised output
# instead of pasting it.
DELIVERABLES = (
    "\n\n--- Required deliverables ---\n"
    "1. Paste the verbatim output of every command that proves the acceptance "
    "criteria. No summaries, no paraphrase.\n"
    "2. State every value this prompt asked you to choose, and why you chose it.\n"
    "3. Never report a command as passing without its pasted output. If you did "
    "not run it, say so."
)
```

and in the node loop:

```python
            payload = {"prompt": n["prompt"] + DELIVERABLES, "cwd": work_path,
                       "p2p": n.get("p2p", True), "agent_id": nid, "db": db_path}
```

- [ ] **Step 5: Fix the docs**

`silicorism_mcp.py` INSTRUCTIONS — replace the execution-models sentence with:

```python
    "Execution models are the bedrock OSS pair with thinking=high: "
    "kimi-k2.5 (build/review/fix) and glm-5 (reason/scout). Never assign a "
    "Claude model to an execution node, and never qwen3-coder-480b.\n\n"
```

and the node `model` description with:

```python
                            "model": {"type": "string",
                                      "description": "friendly name: kimi-k2.5 "
                                      "(build/review/fix), glm-5 (reason/scout). "
                                      "These resolve on the pi harness only. "
                                      "Never a Claude model."},
```

`skills/silicorism/SKILL.md` line 26 becomes:

```markdown
   Models: `glm-5` scouts and reasons, `kimi-k2.5` builds, reviews and fixes.
   Never `qwen3-coder-480b`.
```

Add to that skill's step 4 (after the verify-node sentence):

```markdown
   A `pi` node may also carry `test_command`: the worker runs it after the agent
   exits and fails the node on non-zero, so a node cannot report its own success.
   Give slow nodes `stall_timeout_s` (default 600) — a node that writes no files
   for that long is failed instead of held to the 3600s ceiling.
```

Add to that skill's step 6:

```markdown
   A `timed_out: true` wait means nothing settled: read `silicorism_get_status`'s
   `stalled` list, `silicorism_cancel_task` the wedged node, then wait again.
```

`.claude/commands/silicorism.md:20` — same model line as SKILL.md.

`README.md` — line 91 `builder`/`fixer` become `moonshotai.kimi-k2.5`; line 102's `simple` row becomes `one agent on kimi-k2.5`; line 122's ladder becomes `kimi-k2.5 → glm-5`.

- [ ] **Step 6: Sync the installed skill**

```bash
cp skills/silicorism/SKILL.md ~/.claude/skills/silicorism/SKILL.md
diff skills/silicorism/SKILL.md ~/.claude/skills/silicorism/SKILL.md && echo synced
```

- [ ] **Step 7: Run everything**

Run:
```bash
.venv/bin/python -m pytest -q
.venv/bin/python handlers.py && .venv/bin/python silicorism_tools.py && .venv/bin/python tmux_orchestrator.py
```
Expected: suite PASS; the three self-checks print their OK lines.

- [ ] **Step 8: Commit**

```bash
git add handlers.py silicorism_tools.py silicorism_mcp.py skills/ .claude/ README.md tests/
git commit -m "fix: drop the banned model from every default and make nodes paste their evidence"
```

---

### Task 8: End-to-end proof

**Files:**
- Test: `tests/test_progress.py` (one real-worker test)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the test**

Append to `tests/test_progress.py`:

```python
def test_real_worker_fails_a_lying_node(tmp_path, monkeypatch):
    """The F1 acceptance check: a node whose gate fails must end `failed`.

    No tmux: run_worker only goes native under SILICORISM_NATIVE, so the
    in-process pi handler is patched to a no-op agent that writes nothing and
    claims success — exactly the behaviour that shipped a broken step 4.
    """
    import handlers
    import worker
    dbp = str(tmp_path / "e2e.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    monkeypatch.setitem(handlers.HANDLERS, "pi",
                        lambda payload, context=None: "all tests pass, honest")
    db.add_task(conn, "pi", json.dumps(
        {"prompt": "x", "cwd": str(tmp_path)}), max_retries=0)
    db.add_task(conn, "verify", json.dumps(
        {"test_command": "false", "cwd": str(tmp_path)}),
        depends_on=1, max_retries=0)
    worker.run_worker(dbp, "w0", max_idle_loops=2)
    counts = db.counts(conn)
    assert counts["failed"] == 1 and counts["completed"] == 1
    conn.close()
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_progress.py::test_real_worker_fails_a_lying_node -q`
Expected: PASS. If it hangs, the worker never drained — check `max_idle_loops`.

- [ ] **Step 3: Full suite plus self-checks**

Run:
```bash
.venv/bin/python -m pytest -q
.venv/bin/python handlers.py && .venv/bin/python silicorism_tools.py && .venv/bin/python tmux_orchestrator.py && .venv/bin/python worker.py --help >/dev/null && echo all-ok
```
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_progress.py
git commit -m "test: a node that lies about passing ends failed end to end"
```

---

## Manual verification (after Task 8)

The report's own acceptance checks, run by hand against a scratch DB:

1. **F1** — submit a one-node DAG with `test_command` pointing at a deliberately broken suite. The node must end `failed`, `silicorism_get_status` must show `failed: 1`.
2. **F3** — submit a node whose prompt is `sleep 3600` with `stall_timeout_s: 120`. Within ~3 minutes the task must be `failed` with `no progress for` in its error log.
3. **F4** — during that run, `tmux list-panes -a | grep <run>` must show the pane gone afterwards, and `pgrep -f gopls` must not gain orphans.
4. **F5** — force a `processing` row (`UPDATE tasks SET status='processing'`), then `silicorism_gc(stuck=true, tasks=true)` must clear it.
5. **F6** — `silicorism_wait(timeout_s=60)` against a long-running queue must return `timed_out: true` with `elapsed_s ≈ 60`.

## Still open (do not fix blind)

- **F7** (node `model` may not be honoured): 2-node DAG, deliberately different models, each node instructed to log its resolved model into the artifact. Compare against the spec before touching resolution code.
- **F9** (agents inherit the operator's global plugins): run one node prompt twice, once with those plugins disabled for the agent, and compare whether command output is pasted verbatim.
