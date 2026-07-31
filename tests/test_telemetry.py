"""Token and cost accounting: what the DAG burnt, and what it avoided.

The premise of the tool is that a cheap model does the work an expensive one
planned. Nothing measured that, so nobody could answer whether a run was
cheaper than doing it in one Claude session. These tests pin the numbers.
"""

import json
import sqlite3
from unittest.mock import patch

import db
import handlers


# --- pricing (pure) ---------------------------------------------------------

def test_priced_model_costs_what_the_table_says():
    # 1M input at $5 + 1M output at $25 = $30 exactly.
    cost = handlers.usage_cost("claude-opus-5",
                               {"input": 1_000_000, "output": 1_000_000})
    assert cost == 30.0


def test_cache_reads_are_a_tenth_of_input():
    read = handlers.usage_cost("claude-opus-5", {"cacheRead": 1_000_000})
    plain = handlers.usage_cost("claude-opus-5", {"input": 1_000_000})
    assert read == plain / 10


def test_unpriced_gateway_is_zero_not_a_guess():
    """bedrock-mantle reports usage.cost.total = 0 and publishes no price."""
    assert handlers.usage_cost("bedrock-mantle/moonshotai.kimi-k2.5",
                               {"input": 500_000, "output": 500_000}) == 0.0


def test_missing_fields_do_not_crash_the_cost():
    assert handlers.usage_cost("claude-opus-5", {}) == 0.0
    assert handlers.usage_cost("claude-opus-5", {"input": None}) == 0.0


def test_baseline_prices_every_token_at_the_planner_rate():
    """The arbitrage number: what these tokens would have cost in one session."""
    usage = {"input": 200_000, "output": 20_000}
    assert handlers.baseline_cost(usage) == handlers.usage_cost(
        handlers.BASELINE_MODEL, usage)
    assert handlers.baseline_cost(usage) > 0


# --- schema -----------------------------------------------------------------

def test_columns_are_added_to_an_existing_db(tmp_path):
    """A DB created before this change must gain the columns, not error."""
    dbp = str(tmp_path / "old.db")
    old = sqlite3.connect(dbp)
    # The shape before this change: everything the claim index needs, none of
    # the telemetry columns.
    old.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "task_type TEXT NOT NULL, payload TEXT, "
                "status TEXT NOT NULL DEFAULT 'pending', "
                "priority INTEGER NOT NULL DEFAULT 0, "
                "created_at TEXT NOT NULL DEFAULT '')")
    old.commit()
    old.close()
    db.init_db(dbp)
    conn = db.connect(dbp)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert {"input_tokens", "output_tokens", "cost_usd", "model_used"} <= cols


def test_record_usage_accumulates_across_retries(tmp_path):
    """An escalated retry burns tokens twice and the bill has to say so."""
    dbp = str(tmp_path / "acc.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", "{}")
    db.record_usage(conn, tid, input_tokens=100, output_tokens=10,
                    cost_usd=0.5, model_used="bedrock-mantle/moonshotai.kimi-k2.5")
    db.record_usage(conn, tid, input_tokens=300, output_tokens=20,
                    cost_usd=1.5, model_used="bedrock-mantle/zai.glm-5")
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["input_tokens"] == 400
    assert row["output_tokens"] == 30
    assert row["cost_usd"] == 2.0
    # The last model to run is the one that produced the artifact.
    assert row["model_used"] == "bedrock-mantle/zai.glm-5"


def test_totals_sum_the_whole_dag(tmp_path):
    dbp = str(tmp_path / "tot.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    for i in (1, 2):
        tid = db.add_task(conn, "pi", "{}")
        db.record_usage(conn, tid, input_tokens=1000 * i, output_tokens=100 * i,
                        cost_usd=0.0, model_used="m")
    t = db.usage_totals(conn)
    assert t["input_tokens"] == 3000
    assert t["output_tokens"] == 300
    assert t["cost_usd"] == 0.0
    # Priced at the planner's rate, this is the spend the run avoided.
    assert t["baseline_usd"] == handlers.baseline_cost(
        {"input": 3000, "output": 300})


def test_totals_on_an_untouched_db_are_zero_not_none(tmp_path):
    dbp = str(tmp_path / "empty.db")
    db.init_db(dbp)
    t = db.usage_totals(db.connect(dbp))
    assert t == {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                 "baseline_usd": 0.0}


# --- what the operator sees -------------------------------------------------

def _frame_text(**usage):
    import dashboard
    row = {"id": 1, "task_type": "pi", "status": "completed", "payload": "{}",
           "depends_on": None, "started_at": None, "updated_at": None,
           "pane_target": None, "agent_id": None, "last_progress_at": None,
           "retry_count": 0, **usage}
    return " ".join(dashboard.flatten(dashboard.build_frame(
        [row], [], {"pending": 0, "processing": 0, "completed": 1, "failed": 0},
        width=120)))


def test_the_dashboard_shows_tokens_and_the_avoided_spend():
    text = _frame_text(input_tokens=2000, output_tokens=500, cost_usd=0.0)
    assert "2.0k in / 500 out" in text   # thousands abbreviate, hundreds do not
    saved = handlers.baseline_cost({"input": 2000, "output": 500})
    assert f"saved ${saved:.2f}" in text


def test_the_dashboard_says_nothing_when_no_node_reported_usage():
    assert "saved" not in _frame_text(input_tokens=None, output_tokens=None,
                                      cost_usd=None)


def test_the_dashboard_survives_a_row_from_before_the_columns_existed():
    assert "saved" not in _frame_text()


def test_status_reports_the_totals(tmp_path):
    import silicorism_tools
    dbp = str(tmp_path / "st.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", "{}")
    db.record_usage(conn, tid, input_tokens=5000, output_tokens=200,
                    cost_usd=0.0, model_used="bedrock-mantle/zai.glm-5")
    assert silicorism_tools.get_status(conn)["usage"] == db.usage_totals(conn)


# --- the launch side --------------------------------------------------------

def test_pi_command_exports_the_usage_path():
    """autoexit.ts writes usage only when the worker names a file for it."""
    cmd = handlers.native_command("pi", json.dumps(
        {"prompt": "go", "usage": "/tmp/u.json"}))
    assert "SILICORISM_USAGE=/tmp/u.json" in cmd


def test_pi_command_without_a_usage_path_exports_nothing():
    cmd = handlers.native_command("pi", json.dumps({"prompt": "go"}))
    assert "SILICORISM_USAGE" not in cmd


def test_worker_injects_the_usage_path_next_to_the_artifact():
    import worker
    task = {"id": 7, "task_type": "pi", "payload": json.dumps({"prompt": "go"}),
            "worktree_path": None}
    data = json.loads(worker._native_payload(task))
    assert data["usage"] == worker._usage_path(7)
    assert data["usage"] != data["artifact"]


# --- the recording side -----------------------------------------------------

def _pane_writes_usage(path, code, *, model="moonshotai.kimi-k2.5",
                       provider="bedrock-mantle", inp=1200, out=340, body=None):
    """wait_for_exit stand-in: the file appears while the pane runs, as in life.

    Writing it before _run_native would not survive _clear_capture, which wipes
    a stale usage file so an escalated retry cannot be billed for attempt one.
    """
    def _exit(*_a, **_kw):
        with open(path, "w") as fh:
            if body is not None:
                fh.write(body)
            else:
                json.dump({"input": inp, "output": out, "cacheRead": 0,
                           "cacheWrite": 0, "provider": provider,
                           "model": model}, fh)
        return code
    return _exit


@patch("worker.tmux")
def test_a_completed_native_run_records_its_tokens(mock_tmux, tmp_path):
    import worker
    dbp = str(tmp_path / "rec.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", json.dumps({"prompt": "go"}))
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    mock_tmux.sentinel_path.side_effect = lambda t: str(tmp_path / f"{t}.exit")
    mock_tmux.log_path.side_effect = lambda t: str(tmp_path / f"{t}.log")
    mock_tmux.grid_pane.return_value = ("agents", "%1")
    mock_tmux.read_log_tail.return_value = "done"
    mock_tmux.wait_for_exit.side_effect = _pane_writes_usage(
        worker._usage_path(tid), 0)

    worker._run_native(conn, task, "agent-0", "true")

    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "completed"
    assert row["input_tokens"] == 1200
    assert row["output_tokens"] == 340
    assert row["model_used"] == "bedrock-mantle/moonshotai.kimi-k2.5"


@patch("worker.tmux")
def test_a_failed_native_run_still_records_its_tokens(mock_tmux, tmp_path):
    """Tokens a failed node burnt are real money; skipping them understates."""
    import worker
    dbp = str(tmp_path / "fail.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", json.dumps({"prompt": "go"}))
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    mock_tmux.sentinel_path.side_effect = lambda t: str(tmp_path / f"{t}.exit")
    mock_tmux.log_path.side_effect = lambda t: str(tmp_path / f"{t}.log")
    mock_tmux.grid_pane.return_value = ("agents", "%1")
    mock_tmux.wait_for_exit.side_effect = _pane_writes_usage(
        worker._usage_path(tid), 3, inp=900, out=50)

    try:
        worker._run_native(conn, task, "agent-0", "false")
    except RuntimeError:
        pass
    else:
        raise AssertionError("exit 3 must raise")

    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["input_tokens"] == 900
    assert row["output_tokens"] == 50


@patch("worker.tmux")
def test_a_missing_usage_file_is_not_an_error(mock_tmux, tmp_path):
    """Older extension, crashed pane, claude harness: absent is normal."""
    import worker
    dbp = str(tmp_path / "none.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", json.dumps({"prompt": "go"}))
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    mock_tmux.sentinel_path.side_effect = lambda t: str(tmp_path / f"{t}.exit")
    mock_tmux.log_path.side_effect = lambda t: str(tmp_path / f"{t}.log")
    mock_tmux.grid_pane.return_value = ("agents", "%1")
    mock_tmux.read_log_tail.return_value = "done"
    mock_tmux.wait_for_exit.return_value = 0  # pane leaves no usage file

    worker._run_native(conn, task, "agent-0", "true")

    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "completed"
    assert row["input_tokens"] is None


@patch("worker.tmux")
def test_a_corrupt_usage_file_is_not_an_error(mock_tmux, tmp_path):
    import worker
    dbp = str(tmp_path / "junk.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", json.dumps({"prompt": "go"}))
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    mock_tmux.sentinel_path.side_effect = lambda t: str(tmp_path / f"{t}.exit")
    mock_tmux.log_path.side_effect = lambda t: str(tmp_path / f"{t}.log")
    mock_tmux.grid_pane.return_value = ("agents", "%1")
    mock_tmux.read_log_tail.return_value = "done"
    mock_tmux.wait_for_exit.side_effect = _pane_writes_usage(
        worker._usage_path(tid), 0, body="not json{{{")

    worker._run_native(conn, task, "agent-0", "true")

    assert conn.execute("SELECT status FROM tasks WHERE id=?",
                        (tid,)).fetchone()["status"] == "completed"
