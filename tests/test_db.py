"""Concurrency proof for the WAL layer: many processes hammer the DB at once
and every write must land with zero SQLITE_BUSY and zero double-claims."""

from __future__ import annotations

import multiprocessing as mp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

N_PROCS = 8
PER_PROC = 50


def _writer(db_path, agent, n, err_q):
    try:
        conn = db.connect(db_path)
        for i in range(n):
            db.add_task(conn, "echo", f"{agent}-{i}", priority=i % 3)
            db.log(conn, None, agent, f"wrote {i}")
            db.heartbeat(conn, agent, "busy")
        conn.close()
    except Exception as e:  # any SQLITE_BUSY / lock error lands here
        err_q.put(f"{agent}: {e!r}")


def _claimer(db_path, agent, claimed_list, err_q):
    try:
        conn = db.connect(db_path)
        while True:
            task = db.claim_task(conn, agent)
            if task is None:
                break
            claimed_list.append(task["id"])
        conn.close()
    except Exception as e:
        err_q.put(f"{agent}: {e!r}")


def _run(procs, err_q):
    for p in procs:
        p.start()
    errors = []
    for p in procs:
        p.join(30)
    while not err_q.empty():
        errors.append(err_q.get_nowait())
    return errors


def test_concurrent_writes_no_busy(tmp_path):
    dbp = str(tmp_path / "concurrent.db")
    db.init_db(dbp)
    err_q = mp.Queue()
    procs = [mp.Process(target=_writer, args=(dbp, f"w{i}", PER_PROC, err_q))
             for i in range(N_PROCS)]
    errors = _run(procs, err_q)

    assert errors == [], f"concurrent writes errored: {errors}"
    conn = db.connect(dbp)
    assert db.counts(conn)["pending"] == N_PROCS * PER_PROC
    log_n = conn.execute("SELECT COUNT(*) n FROM execution_logs").fetchone()["n"]
    assert log_n == N_PROCS * PER_PROC
    conn.close()


def test_no_double_claim(tmp_path):
    dbp = str(tmp_path / "claim.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    total = 300
    for i in range(total):
        db.add_task(conn, "echo", str(i))
    conn.close()

    mgr = mp.Manager()
    claimed = mgr.list()
    err_q = mp.Queue()
    procs = [mp.Process(target=_claimer, args=(dbp, f"c{i}", claimed, err_q))
             for i in range(N_PROCS)]
    errors = _run(procs, err_q)

    assert errors == [], f"claiming errored: {errors}"
    ids = list(claimed)
    assert len(ids) == total, f"claimed {len(ids)} of {total}"
    assert len(set(ids)) == total, "a task was claimed by more than one worker"


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        test_concurrent_writes_no_busy(Path(d))
        test_no_double_claim(Path(d))
    print("test_db OK")
