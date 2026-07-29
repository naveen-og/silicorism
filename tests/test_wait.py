"""wait_for_settle turns the orchestrator's poll loop into one call."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

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


def test_a_doomed_run_settles_instead_of_waiting_out_the_timeout(tmp_path):
    """A node stranded behind a failure never runs; waiting on it is forever."""
    conn, _ = _conn(tmp_path)
    tid = db.add_task(conn, "fail", "boom", max_retries=0)
    db.add_task(conn, "echo", "downstream", depends_on=tid)
    db.claim_task(conn, "w")
    db.fail_task(conn, tid)
    out = st.wait_for_settle(conn, timeout_s=30, poll=0.01)
    assert out["settled"] is True and out["failures"]
    assert out["active"] == 0 and out["blocked"] == 1
    conn.close()


def test_an_old_failure_does_not_settle_a_fresh_wait(tmp_path):
    """Failed rows never clear — settling on them would spin the resubmit loop."""
    conn, _ = _conn(tmp_path)
    tid = db.add_task(conn, "fail", "boom", max_retries=0)
    db.add_task(conn, "echo", "unrelated work still to do")
    db.claim_task(conn, "w")
    db.fail_task(conn, tid)
    out = st.wait_for_settle(conn, timeout_s=1, poll=0.05)
    assert out["settled"] is False and out["active"] == 1
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


def test_the_wait_recovers_a_task_whose_worker_was_killed(tmp_path):
    """SIGKILL skips the worker's requeue, so the wait has to notice."""
    import time

    conn, _ = _conn(tmp_path)
    tid = db.add_task(conn, "echo", "hi")
    db.claim_task(conn, "killed-worker")
    db.heartbeat(conn, "killed-worker", "busy", tid)
    time.sleep(0.05)

    real = db.reap_stale  # the real reaper, with the 5-minute window shortened
    with patch.object(db, "reap_stale", lambda c, **kw: real(c, older_than_s=0.02)):
        out = st.wait_for_settle(conn, timeout_s=1, poll=0.05, stop=lambda: True)
    assert conn.execute("SELECT status FROM tasks WHERE id=?",
                        (tid,)).fetchone()["status"] == "pending"
    assert out["settled"] is False  # requeued, not finished
    conn.close()


def test_wait_says_when_it_timed_out(tmp_path):
    """A timeout used to be shape-identical to a real verdict; the only
    discriminator was settled=false, which is easy to skim past."""
    conn, _ = _conn(tmp_path)
    tid = db.add_task(conn, "sleep", "5")     # stays pending: never settles
    out = st.wait_for_settle(conn, timeout_s=1, poll=0.1)
    assert out["settled"] is False and out["timed_out"] is True
    assert out["elapsed_s"] >= 1

    db.complete_task(conn, tid, artifact="done")
    out2 = st.wait_for_settle(conn, timeout_s=5, poll=0.1)
    assert out2["settled"] is True and out2["timed_out"] is False
    conn.close()
