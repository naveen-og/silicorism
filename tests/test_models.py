"""F7: does a node's declared model reach the process that runs it?

The report was a node specified `kimi-k2.5` whose pane statusline read
`zai.glm-5`. This traces the model end to end — spec, queued payload, launched
command — and pins the one thing that legitimately changes it.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
import handlers  # noqa: E402
import silicorism_tools  # noqa: E402

KIMI = "bedrock-mantle/moonshotai.kimi-k2.5"
GLM = "bedrock-mantle/zai.glm-5"


def _launched_model(payload: str) -> str:
    """The --model the pane would actually be given for this payload."""
    cmd = handlers.native_command("pi", payload, None, cli_path="/x/cli.py")
    parts = shlex.split(cmd)
    return parts[parts.index("--model") + 1]


def test_each_node_is_launched_on_the_model_it_asked_for(tmp_path):
    """The F7 reproduction: two nodes, deliberately different models."""
    dbp = str(tmp_path / "m.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    try:
        out = silicorism_tools.build_dag(conn, dbp, [
            {"id": "a", "prompt": "first", "harness": "pi", "model": "kimi-k2.5"},
            {"id": "b", "prompt": "second", "harness": "pi", "model": "glm-5",
             "depends_on": ["a"]},
        ], cwd=str(tmp_path))
        want = {"a": KIMI, "b": GLM}
        rows = {r["id"]: r["payload"] for r in db.all_tasks(conn)}
        assert len(out["nodes"]) == 2, out
        for node_id, tid in out["nodes"].items():
            payload = rows[tid]
            # The payload keeps whatever the planner wrote; resolution to a full
            # id happens once, at launch. Both ends have to agree.
            spec = json.loads(payload)["model"]
            assert handlers.resolve_model(spec) == want[node_id], (node_id, spec)
            assert _launched_model(payload) == want[node_id], (node_id, payload)
    finally:
        conn.close()


def test_a_full_id_and_a_friendly_name_reach_the_same_place():
    for spec in ("kimi-k2.5", KIMI):
        assert _launched_model(json.dumps({"prompt": "x", "model": spec})) == KIMI
    # An unknown name is passed through rather than silently swapped, so a typo
    # surfaces as the model that ran instead of looking like the default.
    assert _launched_model(json.dumps({"prompt": "x", "model": "hy3"})) \
        == "opencode/hy3-free"
    assert _launched_model(json.dumps({"prompt": "x", "model": "nope-9"})) == "nope-9"


def test_no_model_means_the_default_not_an_empty_flag():
    assert _launched_model(json.dumps({"prompt": "x"})) == handlers.DEFAULT_PI_MODEL
    assert handlers.DEFAULT_PI_MODEL == KIMI


def test_a_retry_is_the_one_thing_that_changes_a_nodes_model():
    """This is what F7 saw. A kimi node that failed once retries on glm-5 by
    design, so a glm pane for a kimi node means attempt 2, not misrouting."""
    first = json.dumps({"prompt": "x", "model": "kimi-k2.5"})
    assert _launched_model(first) == KIMI
    second = handlers.escalate_payload("pi", first)
    assert second is not None and _launched_model(second) == GLM
    # And it stops there rather than wandering onto an unvetted model.
    assert handlers.escalate_payload("pi", second) is None


def test_the_escalated_model_is_recorded_where_the_operator_looks(tmp_path):
    """F7 was unfalsifiable from the outside: the payload's model was rewritten
    in place, so nothing distinguished 'ran on glm-5' from 'asked for glm-5'."""
    dbp = str(tmp_path / "esc.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    try:
        tid = db.add_task(conn, "pi", json.dumps(
            {"prompt": "x", "model": "kimi-k2.5"}))
        db.set_payload(conn, tid, handlers.escalate_payload(
            "pi", json.dumps({"prompt": "x", "model": "kimi-k2.5"})))
        payload = json.loads(conn.execute(
            "SELECT payload FROM tasks WHERE id=?", (tid,)).fetchone()["payload"])
        assert payload["model"] == GLM
        assert payload["model_requested"] == "kimi-k2.5"
    finally:
        conn.close()


def test_the_built_in_tiers_use_the_documented_models(tmp_path):
    dbp = str(tmp_path / "t.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    try:
        silicorism_tools.build_pipeline(conn, dbp, "feat", "do a thing")
        seen = {}
        for row in db.all_tasks(conn):
            if row["task_type"] != "pi":
                continue
            data = json.loads(row["payload"])
            seen[data.get("agent_id", "?")] = _launched_model(row["payload"])
        assert seen, "the standard tier queued no pi nodes"
        for agent, model in seen.items():
            assert model in (KIMI, GLM), (agent, model)
            assert "qwen" not in model, (agent, model)
        scouts = [m for a, m in seen.items() if a.startswith("scout")]
        assert scouts == [GLM], seen
    finally:
        conn.close()
