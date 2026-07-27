"""SQLite WAL state layer for the orchestrator.

All state-modifying work goes through `immediate()` which runs BEGIN IMMEDIATE
under an exponential-backoff retry, so concurrent worker processes serialize
their writes without ever surfacing SQLITE_BUSY to callers.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATUSES = ("pending", "processing", "completed", "failed")

_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA foreign_keys = ON",
    "PRAGMA journal_size_limit = 67108864",
    "PRAGMA temp_store = MEMORY",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type   TEXT NOT NULL,
    payload     TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','processing','completed','failed')),
    agent_id    TEXT,
    priority    INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    depends_on      TEXT,            -- JSON array of prerequisite task ids
    output_artifact TEXT,            -- stdout/return value of a completed task
    worktree_path   TEXT,            -- dedicated git worktree for this task
    pane_target     TEXT,            -- tmux "<window>.<pane_id>" showing this task
    started_at      TEXT,            -- stamped when a worker claims the task
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_claim
    ON tasks (status, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS agent_heartbeats (
    agent_id        TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    current_task_id INTEGER REFERENCES tasks(id),
    last_seen       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS execution_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   INTEGER REFERENCES tasks(id),
    agent_id  TEXT,
    level     TEXT NOT NULL DEFAULT 'info',
    message   TEXT NOT NULL,
    metadata  TEXT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- P2P inter-agent channel: one agent leaves a note for another.
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id    TEXT,
    recipient_id TEXT,
    content      TEXT,
    status       TEXT NOT NULL DEFAULT 'unread'
                 CHECK (status IN ('unread','read')),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_msg_inbox
    ON messages (recipient_id, status, id);

-- Worktree GC state machine: allocated -> active -> quarantined | cleaned.
CREATE TABLE IF NOT EXISTS worktrees (
    path       TEXT PRIMARY KEY,
    branch     TEXT,
    state      TEXT NOT NULL DEFAULT 'allocated'
               CHECK (state IN ('allocated','active','quarantined','cleaned')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


def now() -> str:
    """UTC timestamp matching SQLite's strftime('%Y-%m-%dT%H:%M:%fZ') exactly.

    Python's %f is microseconds; SQLite's is SS.SSS. Formatting seconds
    explicitly and trimming to milliseconds is what makes the two agree, so
    created_at (SQLite default) and updated_at (this function) are comparable.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with all PRAGMAs applied and autocommit control.

    isolation_level=None hands transaction control to us so BEGIN IMMEDIATE
    behaves exactly as written instead of the driver's implicit BEGIN.
    """
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


# Columns added after the original schema; ALTER them onto pre-existing DBs.
_MIGRATIONS = (
    ("depends_on", "TEXT"),
    ("output_artifact", "TEXT"),
    ("worktree_path", "TEXT"),
    ("pane_target", "TEXT"),
    ("started_at", "TEXT"),
)


def _retry_busy(fn, *, attempts: int = 8, base: float = 0.02):
    """Call fn(), retrying SQLITE_BUSY with the same backoff immediate() uses."""
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as err:
            if not _is_busy(err) or i == attempts - 1:
                raise
            time.sleep(base * (2 ** i) + random.random() * base)


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    for col, typ in _MIGRATIONS:
        if col in have:
            continue
        try:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError as err:
            # A racing process added it between our read and our write; that is
            # the outcome we wanted either way.
            if "duplicate column" not in str(err).lower():
                raise


def init_db(db_path: str | Path) -> None:
    """Create or migrate the schema. Safe to call from several processes at once.

    Every MCP tool call starts with this, so the DDL has to serialise like the
    rest of the writes — otherwise two workers ALTER the same column and one of
    them dies with "duplicate column name".
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        # executescript commits any open transaction, so it cannot run inside
        # immediate(); every statement in _SCHEMA is IF NOT EXISTS, so the only
        # concurrent failure mode is the DDL lock, which is worth retrying.
        _retry_busy(lambda: conn.executescript(_SCHEMA))
        with immediate(conn) as c:
            _migrate(c)
    finally:
        conn.close()


def _is_busy(err: sqlite3.OperationalError) -> bool:
    msg = str(err).lower()
    return "locked" in msg or "busy" in msg


@contextmanager
def immediate(conn: sqlite3.Connection, *, attempts: int = 8, base: float = 0.02):
    """Run a write transaction as BEGIN IMMEDIATE with exponential backoff.

    Retries the whole transaction on SQLITE_BUSY/locked. busy_timeout already
    absorbs most contention; this covers the tail where the 5s timer expires.
    """
    for i in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as err:
            if _is_busy(err) and i < attempts - 1:
                time.sleep(base * (2 ** i) + random.random() * base)
                continue
            raise
        try:
            yield conn
            conn.execute("COMMIT")
            return
        except sqlite3.OperationalError as err:
            conn.execute("ROLLBACK")
            if _is_busy(err) and i < attempts - 1:
                time.sleep(base * (2 ** i) + random.random() * base)
                continue
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise


# --- task ops ---------------------------------------------------------------

def _dep_json(depends_on) -> str | None:
    """Normalize None / int / list-of-ids into a JSON-array string (or None)."""
    if depends_on is None:
        return None
    ids = depends_on if isinstance(depends_on, (list, tuple)) else [depends_on]
    return json.dumps([int(i) for i in ids]) if ids else None


def add_task(conn, task_type, payload=None, *, priority=0, max_retries=3,
             depends_on=None, worktree_path=None) -> int:
    with immediate(conn) as c:
        cur = c.execute(
            "INSERT INTO tasks (task_type, payload, priority, max_retries, "
            "depends_on, worktree_path) VALUES (?,?,?,?,?,?)",
            (task_type, payload, priority, max_retries,
             _dep_json(depends_on), worktree_path),
        )
        return cur.lastrowid


# A pending task is claimable only if every id in depends_on is 'completed'.
# A dep id that is missing, failed, or still running counts as unmet, so the
# task stays parked until its whole prerequisite set finishes.
_CLAIM_SQL = """
SELECT * FROM tasks t
WHERE t.status='pending'
  AND NOT EXISTS (
      SELECT 1 FROM json_each(COALESCE(t.depends_on,'[]')) d
      LEFT JOIN tasks p ON p.id = d.value
      WHERE COALESCE(p.status,'missing') != 'completed'
  )
ORDER BY t.priority DESC, t.created_at ASC LIMIT 1
"""


def claim_task(conn, agent_id) -> sqlite3.Row | None:
    """Atomically grab the highest-priority claimable pending task.

    Claimable = pending AND all depends_on prerequisites completed.
    """
    claimed = {"row": None}
    with immediate(conn) as c:
        row = c.execute(_CLAIM_SQL).fetchone()
        if row is None:
            return None
        c.execute(
            "UPDATE tasks SET status='processing', agent_id=?, started_at=?, "
            "updated_at=? WHERE id=?",
            (agent_id, now(), now(), row["id"]),
        )
        claimed["row"] = row
    return claimed["row"]


def complete_task(conn, task_id, artifact: str | None = None) -> None:
    with immediate(conn) as c:
        c.execute(
            "UPDATE tasks SET status='completed', output_artifact=?, "
            "updated_at=? WHERE id=?",
            (artifact, now(), task_id),
        )


def set_pane_target(conn, task_id, target: str) -> None:
    """Record the tmux window.pane showing this task (display metadata only)."""
    with immediate(conn) as c:
        c.execute("UPDATE tasks SET pane_target=? WHERE id=?", (target, task_id))


def all_tasks(conn) -> list[sqlite3.Row]:
    """Every task in id order — the dashboard's read model."""
    return conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()


def dep_artifacts(conn, task_id) -> dict[int, str]:
    """Return {dep_task_id: output_artifact} for a task's completed deps.

    This is the artifact hand-off: a child task reads what its parents produced
    (e.g. the Scout's CONTEXT.md flowing into the Builder).
    """
    row = conn.execute(
        "SELECT depends_on FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None or not row["depends_on"]:
        return {}
    ids = json.loads(row["depends_on"])
    if not ids:
        return {}
    qs = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, output_artifact FROM tasks WHERE id IN ({qs})", ids
    ).fetchall()
    return {r["id"]: r["output_artifact"] for r in rows
            if r["output_artifact"] is not None}


# --- P2P inter-agent messaging ----------------------------------------------

def send_inter_agent_message(conn, sender, recipient, content) -> int:
    """Leave a note for another agent. Returns the message id."""
    with immediate(conn) as c:
        cur = c.execute(
            "INSERT INTO messages (sender_id, recipient_id, content) "
            "VALUES (?,?,?)",
            (sender, recipient, content),
        )
        return cur.lastrowid


def poll_inter_agent_messages(conn, recipient, *, mark_read=True) -> list[sqlite3.Row]:
    """Return this recipient's unread messages (oldest first); mark them read."""
    with immediate(conn) as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE recipient_id=? AND status='unread' "
            "ORDER BY id ASC",
            (recipient,),
        ).fetchall()
        if rows and mark_read:
            ids = [r["id"] for r in rows]
            qs = ",".join("?" * len(ids))
            c.execute(f"UPDATE messages SET status='read' WHERE id IN ({qs})", ids)
    return rows


def recent_messages(conn, limit=10) -> list[sqlite3.Row]:
    """Newest messages for the supervisor dashboard (read or unread)."""
    return conn.execute(
        "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


# --- worktree GC state machine ----------------------------------------------

def set_worktree(conn, path, state, *, branch=None) -> None:
    """Upsert a worktree row to `state` (allocated|active|quarantined|cleaned)."""
    with immediate(conn) as c:
        c.execute(
            "INSERT INTO worktrees (path, branch, state, updated_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET state=excluded.state, "
            "branch=COALESCE(excluded.branch, worktrees.branch), "
            "updated_at=excluded.updated_at",
            (path, branch, state, now()),
        )


def worktrees(conn, state=None) -> list[sqlite3.Row]:
    if state:
        return conn.execute(
            "SELECT * FROM worktrees WHERE state=? ORDER BY path", (state,)
        ).fetchall()
    return conn.execute("SELECT * FROM worktrees ORDER BY path").fetchall()


def worktree_task_status(conn, path) -> dict[str, int]:
    """Status histogram of the tasks bound to a worktree path (for GC decisions)."""
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM tasks WHERE worktree_path=? GROUP BY status",
        (path,),
    ).fetchall()
    out = {s: 0 for s in STATUSES}
    for r in rows:
        out[r["status"]] = r["n"]
    return out


def fail_task(conn, task_id) -> str:
    """Mark a task failed; requeue as pending if retries remain.

    Returns the resulting status ('pending' or 'failed').
    """
    result = {"status": "failed"}
    with immediate(conn) as c:
        row = c.execute(
            "SELECT retry_count, max_retries FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            return "failed"
        if row["retry_count"] < row["max_retries"]:
            c.execute(
                "UPDATE tasks SET status='pending', retry_count=retry_count+1, "
                "agent_id=NULL, updated_at=? WHERE id=?",
                (now(), task_id),
            )
            result["status"] = "pending"
        else:
            c.execute(
                "UPDATE tasks SET status='failed', updated_at=? WHERE id=?",
                (now(), task_id),
            )
    return result["status"]


def set_payload(conn, task_id, payload: str) -> None:
    """Rewrite a task's payload (used by the retry model-escalation ladder)."""
    with immediate(conn) as c:
        c.execute("UPDATE tasks SET payload=?, updated_at=? WHERE id=?",
                  (payload, now(), task_id))


def requeue_agent_tasks(conn, agent_id) -> None:
    """On shutdown, hand a crashing agent's in-flight work back to the queue."""
    with immediate(conn) as c:
        c.execute(
            "UPDATE tasks SET status='pending', agent_id=NULL, updated_at=? "
            "WHERE status='processing' AND agent_id=?",
            (now(), agent_id),
        )


def reap_stale(conn, *, older_than_s: float = 300.0) -> int:
    """Requeue tasks whose worker died without requeuing them. Returns the count.

    A worker killed with SIGKILL (pane closed, reboot) never reaches its
    `finally`, so its task sits in `processing` for ever and every dependent
    waits behind it. Only tasks whose agent has stopped heartbeating for
    `older_than_s` are touched — a live agent must never have its work
    handed to someone else.
    """
    # Same format as now(), so the string comparison is a time comparison.
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_s)
              ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    with immediate(conn) as c:
        cur = c.execute(
            "UPDATE tasks SET status='pending', agent_id=NULL, updated_at=? "
            "WHERE status='processing' AND agent_id IN ("
            "  SELECT agent_id FROM agent_heartbeats WHERE last_seen < ?)",
            (now(), cutoff))
    return cur.rowcount


# --- observability ----------------------------------------------------------

def log(conn, task_id, agent_id, message, *, level="info", metadata=None) -> None:
    with immediate(conn) as c:
        c.execute(
            "INSERT INTO execution_logs (task_id, agent_id, level, message, metadata) "
            "VALUES (?,?,?,?,?)",
            (task_id, agent_id, level, message, metadata),
        )


def heartbeat(conn, agent_id, status, current_task_id=None) -> None:
    with immediate(conn) as c:
        c.execute(
            "INSERT INTO agent_heartbeats (agent_id, status, current_task_id, last_seen) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "status=excluded.status, current_task_id=excluded.current_task_id, "
            "last_seen=excluded.last_seen",
            (agent_id, status, current_task_id, now()),
        )


def counts(conn) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM tasks GROUP BY status"
    ).fetchall()
    out = {s: 0 for s in STATUSES}
    for r in rows:
        out[r["status"]] = r["n"]
    return out


def heartbeats(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM agent_heartbeats ORDER BY agent_id"
    ).fetchall()


def recent_logs(conn, limit=20) -> list[sqlite3.Row]:
    """Newest execution-log rows across all tasks (for the status aggregation)."""
    return conn.execute(
        "SELECT task_id, agent_id, level, message, timestamp FROM execution_logs "
        "ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def checkpoint(conn) -> None:
    """Non-blocking WAL truncation for idle loops."""
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
