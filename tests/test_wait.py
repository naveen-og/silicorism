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
