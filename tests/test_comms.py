"""What flows along a dep edge, who may write a file, and how mail arrives.

The DAG is already the graph and artifact hand-off already rides its edges, so
none of this adds a topology. It fixes what the edges carry: a live six-node
run spent 10,072 input tokens on a node whose job was to append one function,
because its parent's entire prose report was pasted in front of the task.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
import handlers  # noqa: E402
import silicorism_tools as st  # noqa: E402


# --- what an edge carries ---------------------------------------------------

REPORT = """I read calc.py and here is my full analysis.

""" + ("filler line that nobody downstream needs\n" * 200) + """
--- HANDOFF ---
files: calc.py
added: nothing yet
decisions: 4-space indent, functions at module level
open: none
"""


def test_only_the_handoff_block_crosses_the_edge():
    out = handlers.handoff(REPORT)
    assert "filler line" not in out
    assert "files: calc.py" in out and "4-space indent" in out


def test_the_last_handoff_block_wins():
    """An agent that restates the block after a correction meant the second."""
    text = ("--- HANDOFF ---\nfiles: old.py\n"
            "wait, that was wrong\n"
            "--- HANDOFF ---\nfiles: new.py\n")
    out = handlers.handoff(text)
    assert "new.py" in out and "old.py" not in out


def test_no_block_falls_back_to_a_bounded_tail():
    """Weak models forget the format. Truncating beats pasting 8k tokens, and
    the tail is where a run's conclusion lives."""
    out = handlers.handoff("x" * 9000 + "THE CONCLUSION", cap=200)
    assert "THE CONCLUSION" in out
    assert len(out) <= 200 + len(handlers.TRUNCATED_NOTE)


def test_a_short_artifact_without_a_block_passes_whole():
    assert handlers.handoff("done, nothing to report") == "done, nothing to report"


def test_context_uses_the_handoff_not_the_whole_artifact():
    prompt = handlers._with_context("do the thing", {1: REPORT})
    assert "filler line" not in prompt
    assert "files: calc.py" in prompt and "do the thing" in prompt


def test_the_deliverables_block_asks_for_the_handoff():
    """The format has to be requested or no model will produce it."""
    assert handlers.HANDOFF_MARK in st.DELIVERABLES


# --- who may write a file ---------------------------------------------------

def _dag(conn, dbp, nodes):
    return st.build_dag(conn, dbp, nodes)


def test_two_unordered_nodes_cannot_claim_the_same_file(tmp_path):
    """The real thing that happened: two builders appended to one calc.py in
    one worktree at the same time, and only luck kept both edits."""
    dbp = str(tmp_path / "w.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    nodes = [
        {"id": "scout", "prompt": "read"},
        {"id": "a", "prompt": "add subtract", "depends_on": ["scout"],
         "writes": ["calc.py"]},
        {"id": "b", "prompt": "add multiply", "depends_on": ["scout"],
         "writes": ["calc.py"]},
    ]
    try:
        _dag(conn, dbp, nodes)
    except ValueError as err:
        assert "calc.py" in str(err) and "a" in str(err) and "b" in str(err)
    else:
        raise AssertionError("concurrent writers to one file were accepted")
    conn.close()


def test_ordered_nodes_may_share_a_file(tmp_path):
    """b runs after a, so there is no race: the whole point of the dep graph."""
    dbp = str(tmp_path / "ok.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    out = _dag(conn, dbp, [
        {"id": "a", "prompt": "add subtract", "writes": ["calc.py"]},
        {"id": "b", "prompt": "add multiply", "depends_on": ["a"],
         "writes": ["calc.py"]},
    ])
    assert set(out["nodes"]) == {"a", "b"}
    conn.close()


def test_transitive_order_counts_as_ordered(tmp_path):
    dbp = str(tmp_path / "tr.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    out = _dag(conn, dbp, [
        {"id": "a", "prompt": "x", "writes": ["calc.py"]},
        {"id": "mid", "prompt": "y", "depends_on": ["a"]},
        {"id": "c", "prompt": "z", "depends_on": ["mid"], "writes": ["calc.py"]},
    ])
    assert len(out["nodes"]) == 3
    conn.close()


def test_different_files_never_conflict(tmp_path):
    dbp = str(tmp_path / "df.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    out = _dag(conn, dbp, [
        {"id": "a", "prompt": "x", "writes": ["calc.py"]},
        {"id": "b", "prompt": "y", "writes": ["util.py"]},
    ])
    assert len(out["nodes"]) == 2
    conn.close()


def test_an_undeclared_writer_is_not_rejected(tmp_path):
    """`writes` is a claim, not a discovery. A DAG that declares nothing keeps
    working exactly as before — this cannot become a wall in front of old
    plans."""
    dbp = str(tmp_path / "un.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    out = _dag(conn, dbp, [{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y"}])
    assert len(out["nodes"]) == 2
    conn.close()


def test_the_claim_reaches_the_agent(tmp_path):
    """Declaring it is worthless if the node is never told which files are its
    own to touch."""
    dbp = str(tmp_path / "cl.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    out = _dag(conn, dbp, [{"id": "a", "prompt": "x", "writes": ["calc.py"]}])
    payload = json.loads(conn.execute(
        "SELECT payload FROM tasks WHERE id=?",
        (out["nodes"]["a"],)).fetchone()["payload"])
    assert payload["writes"] == ["calc.py"]
    assert "calc.py" in handlers._prompt(payload, None, native=True)
    conn.close()


# --- how mail arrives -------------------------------------------------------

def test_pending_mail_is_pushed_into_the_prompt():
    """Polling was pull-only: an agent had to guess that mail existed and stop
    to check. Nothing downstream ever did."""
    data = {"prompt": "build it", "p2p": True, "agent_id": "builder",
            "inbox": ["scout: calc.py holds add(a, b)"]}
    out = handlers._prompt(data, None, native=True)
    assert "calc.py holds add(a, b)" in out
    assert "scout" in out


def test_an_empty_inbox_adds_nothing():
    plain = handlers._prompt({"prompt": "x", "p2p": True}, None, native=True)
    empty = handlers._prompt({"prompt": "x", "p2p": True, "inbox": []},
                             None, native=True)
    assert plain == empty


def test_the_worker_drains_the_inbox_into_the_payload(tmp_path):
    import worker
    dbp = str(tmp_path / "inbox.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", json.dumps({"prompt": "x", "agent_id": "b"}))
    db.send_inter_agent_message(conn, "scout", "b", "read calc.py first")
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()

    data = json.loads(worker._native_payload(task, conn=conn))
    assert any("read calc.py first" in m for m in data["inbox"])
    # drained, so a mid-run poll does not replay what the prompt already shows
    assert db.poll_inter_agent_messages(conn, "b") == []
    conn.close()
