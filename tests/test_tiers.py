"""Tier shapes: how many agents a request gets, and whether it needs a worktree."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
import silicorism_tools as st  # noqa: E402


def _conn(tmp_path, name="t.db"):
    dbp = str(tmp_path / name)
    db.init_db(dbp)
    return db.connect(dbp), dbp


def _payload(conn, task_id):
    return json.loads(conn.execute("SELECT payload FROM tasks WHERE id=?",
                                   (task_id,)).fetchone()["payload"])


def test_simple_is_one_agent_and_no_worktree(tmp_path):
    conn, dbp = _conn(tmp_path)
    out = st.build_pipeline(conn, dbp, "game", "build a python game",
                            complexity="simple")
    assert list(out["tasks"]) == ["solo"]
    types = [r["task_type"] for r in conn.execute(
        "SELECT task_type FROM tasks ORDER BY id")]
    assert types == ["pi"]  # no worktree_create, no cleanup
    conn.close()


def test_simple_pins_kimi(tmp_path):
    conn, dbp = _conn(tmp_path)
    out = st.build_pipeline(conn, dbp, "game", "build a python game",
                            complexity="simple")
    assert _payload(conn, out["tasks"]["solo"])["model"] == (
        "bedrock-mantle/moonshotai.kimi-k2.5")
    conn.close()


def test_simple_adds_verify_only_when_a_test_command_is_given(tmp_path):
    conn, dbp = _conn(tmp_path)
    out = st.build_pipeline(conn, dbp, "game", "build it", complexity="simple",
                            test_command="pytest -q")
    assert list(out["tasks"]) == ["solo", "verify"]
    row = conn.execute("SELECT depends_on FROM tasks WHERE id=?",
                       (out["tasks"]["verify"],)).fetchone()
    assert json.loads(row["depends_on"]) == [out["tasks"]["solo"]]
    conn.close()


def test_standard_is_unchanged_and_is_the_default(tmp_path):
    conn, dbp = _conn(tmp_path)
    default = st.build_pipeline(conn, dbp, "a", "add auth")
    explicit = st.build_pipeline(conn, dbp, "b", "add auth", complexity="standard")
    assert list(default["tasks"]) == ["worktree", "scout", "builder", "fixer",
                                      "verify", "cleanup"]
    assert list(explicit["tasks"]) == list(default["tasks"])
    conn.close()


def test_unknown_tier_falls_back_to_standard(tmp_path):
    """A typo in a planning hint must not fail a submit."""
    conn, dbp = _conn(tmp_path)
    out = st.build_pipeline(conn, dbp, "c", "add auth", complexity="medium")
    assert list(out["tasks"]) == ["worktree", "scout", "builder", "fixer",
                                  "verify", "cleanup"]
    conn.close()


def test_complex_forks_two_builders_from_the_scout(tmp_path):
    conn, dbp = _conn(tmp_path)
    out = st.build_pipeline(conn, dbp, "big", "rewrite the parser",
                            complexity="complex")
    t = out["tasks"]
    assert set(t) >= {"worktree_a", "worktree_b", "scout", "builder_a",
                      "builder_b", "integrate", "integrator", "fixer",
                      "verify", "cleanup_a", "cleanup_b"}

    def deps(key):
        row = conn.execute("SELECT depends_on FROM tasks WHERE id=?",
                           (t[key],)).fetchone()
        return json.loads(row["depends_on"] or "[]")

    # Both builders hang off the scout - that is the fan-out.
    assert deps("builder_a") == [t["scout"]]
    assert deps("builder_b") == [t["scout"]]
    # Integration waits for both.
    assert set(deps("integrate")) == {t["builder_a"], t["builder_b"]}
    assert deps("integrator") == [t["integrate"]]
    conn.close()


def test_complex_gives_each_builder_its_own_worktree(tmp_path):
    conn, dbp = _conn(tmp_path)
    out = st.build_pipeline(conn, dbp, "big", "rewrite the parser",
                            complexity="complex")
    a = _payload(conn, out["tasks"]["builder_a"])["cwd"]
    b = _payload(conn, out["tasks"]["builder_b"])["cwd"]
    assert a != b, "concurrent builders must not share a worktree"


def test_complex_cleans_up_both_worktrees_last(tmp_path):
    conn, dbp = _conn(tmp_path)
    out = st.build_pipeline(conn, dbp, "big", "rewrite", complexity="complex")
    t = out["tasks"]
    for key in ("cleanup_a", "cleanup_b"):
        row = conn.execute("SELECT depends_on FROM tasks WHERE id=?",
                           (t[key],)).fetchone()
        # Cleanup trails the last real work node, so a failed run keeps its
        # worktree (and branch) intact for post-mortem.
        assert json.loads(row["depends_on"])[0] >= t["verify"]
    conn.close()


def test_claude_harness_is_coerced_to_pi(tmp_path):
    """A node asking for the claude harness still runs on pi: execution never
    bills or routes to a Claude model."""
    dbp = str(tmp_path / "coerce.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    st.build_dag(conn, dbp, [
        {"id": "a", "prompt": "x", "harness": "claude", "model": "glm-5"},
    ])
    rows = conn.execute("SELECT task_type FROM tasks").fetchall()
    assert [r["task_type"] for r in rows] == ["pi"]
    conn.close()
