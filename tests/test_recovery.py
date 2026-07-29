"""There has to be a way out of a wedged pipeline that is not "throw the DB away"."""

import json

import db
import silicorism_tools

OLD = "2020-01-01T00:00:00.000Z"


def _stamp(conn, tid, column, value):
    conn.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, tid))


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


def test_gc_stuck_then_prune_clears_the_queue(tmp_path):
    """The whole escape hatch through the MCP surface, as an operator runs it."""
    import silicorism_mcp as mcp
    dbp = str(tmp_path / "mcp.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", json.dumps({"prompt": "x"}))
    db.claim_task(conn, "w0")
    db.heartbeat(conn, "w0", "busy", tid)
    _stamp(conn, tid, "last_progress_at", OLD)

    out = json.loads(mcp.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "silicorism_gc",
                   "arguments": {"db": dbp, "stuck": True, "tasks": True}},
    })["result"]["content"][0]["text"])
    assert out["stuck"] == [tid] and out["tasks"]["deleted"] == 1
    assert db.counts(conn)["processing"] == 0
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
