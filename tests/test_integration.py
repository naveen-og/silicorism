"""End-to-end: enqueue a mixed workload, run a real worker pool via the CLI,
and assert the queue drains with no lock errors and correct terminal states."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import db  # noqa: E402


def _cli(*args):
    return subprocess.run(
        [sys.executable, str(ROOT / "cli.py"), *args],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )


def test_pool_drains_without_locks(tmp_path):
    dbp = str(tmp_path / "integ.db")
    assert _cli("init", "--db", dbp).returncode == 0

    n_echo, n_sleep, n_fail = 30, 5, 5
    conn = db.connect(dbp)
    for i in range(n_echo):
        db.add_task(conn, "echo", f"e{i}", priority=i % 4)
    for i in range(n_sleep):
        db.add_task(conn, "sleep", "0.05")
    for i in range(n_fail):
        db.add_task(conn, "fail", "boom", max_retries=2)
    conn.close()

    run = _cli("run", "--db", dbp, "--workers", "4", "--drain")
    output = run.stdout + run.stderr
    assert run.returncode == 0, output
    for bad in ("SQLITE_BUSY", "database is locked", "Traceback"):
        assert bad not in output, f"found {bad!r} in run output:\n{output}"

    conn = db.connect(dbp)
    c = db.counts(conn)
    conn.close()
    assert c["pending"] == 0, c
    assert c["processing"] == 0, c
    assert c["completed"] == n_echo + n_sleep, c
    assert c["failed"] == n_fail, c


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        test_pool_drains_without_locks(Path(d))
    print("test_integration OK")
