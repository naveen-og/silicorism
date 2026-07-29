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


def test_beat_stamps_progress_only_when_files_change(tmp_path, monkeypatch):
    """Counted, not timestamped: db.now() has millisecond resolution and three
    polls of a two-file tree land inside the same millisecond."""
    import worker
    dbp = str(tmp_path / "beat.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("1")
    tid, _ = _task(conn, {"prompt": "x", "cwd": str(work)})
    db.claim_task(conn, "w0")

    stamps = []
    real = db.touch_progress
    monkeypatch.setattr(db, "touch_progress",
                        lambda c, t: (stamps.append(t), real(c, t))[1])

    poll = worker._stop_and_beat(conn, "w0", tid, str(work), every=0.0)
    assert poll() is False
    assert stamps == [tid], "the first tick establishes the baseline"
    assert conn.execute("SELECT last_progress_at FROM tasks WHERE id=?",
                        (tid,)).fetchone()["last_progress_at"] is not None

    assert poll() is False
    assert stamps == [tid], "an unchanged tree must not look like progress"

    (work / "b.txt").write_text("2")
    assert poll() is False
    assert stamps == [tid, tid], "a new file is progress"
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
