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
