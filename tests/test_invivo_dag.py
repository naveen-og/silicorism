"""In vivo: one real six-node DAG, real OSS models, real tmux panes.

Everything else in this suite mocks the agent away, which proves the plumbing
and nothing about whether an agent on a free model can actually finish a node.
This one runs the whole thing for real and asserts on the four claims the tool
makes: the nodes go green, the P2P channel carries traffic, the run's tokens
land in the DB, and the gate passes on work that exists rather than on an
agent's word for it.

Costs money-adjacent time and needs network, tmux and a logged-in pi, so it is
opt-in:

    SILICORISM_INVIVO=1 .venv/bin/python -m pytest tests/test_invivo_dag.py -s

It is deliberately not part of the default run: CI stays offline, free and
deterministic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
import handlers  # noqa: E402
import silicorism_tools as st  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("SILICORISM_INVIVO") != "1",
    reason="live run: set SILICORISM_INVIVO=1 (needs network, tmux, pi login)")

# Per-node wall clock. A free-tier model queues, so this is generous; the stall
# window is what actually catches a wedged pane.
NODE_TIMEOUT_S = 900.0
CALC = "def add(a, b):\n    return a + b\n"


def _git(args, cwd):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       timeout=60)
    assert p.returncode == 0, f"git {args[0]}: {p.stderr.strip()}"
    return p.stdout


def _sample_repo(root: Path) -> Path:
    """A real repo with one function, so the agents have something to read."""
    repo = root / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text(CALC)
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "invivo@test"], repo)
    _git(["config", "user.name", "invivo"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "initial"], repo)
    return repo


NODES = [
    # Reads first, so the builders inherit a description instead of guessing —
    # and proves the channel by having to use it.
    {"id": "scout", "model": "deepseek-v4-flash", "thinking": "max",
     "prompt": "Read calc.py in this directory. Then run exactly this command "
               "in your bash tool: silicorism-msg send builder_a 'calc.py "
               "holds add(a, b)'. Report the file's contents."},
    {"id": "builder_a", "model": "kimi-k2.5", "thinking": "high",
     "depends_on": ["scout"], "writes": ["calc.py"],
     "prompt": "Append a function `subtract(a, b)` returning a - b to calc.py. "
               "Change nothing else in the file."},
    # Ordered behind builder_a, not beside it. Both write calc.py, and running
    # them at once is exactly the lost-update race _check_write_conflicts now
    # refuses to queue — the first version of this test had them parallel and
    # both edits survived on luck.
    {"id": "builder_b", "model": "glm-5", "thinking": "high",
     "depends_on": ["builder_a"], "writes": ["calc.py"],
     "prompt": "Append a function `multiply(a, b)` returning a * b to calc.py. "
               "Change nothing else in the file."},
    {"id": "fixer", "model": "kimi-k2.5", "thinking": "high",
     "depends_on": ["builder_b"], "writes": ["calc.py", "test_calc.py"],
     "prompt": "calc.py should now define add, subtract and multiply. If any "
               "is missing or wrong, fix calc.py. Then write test_calc.py "
               "using the unittest module, with one test per function: "
               "add(2, 3) == 5, subtract(3, 1) == 2, multiply(3, 2) == 6."},
    # The only node that cannot lie: it runs the tests the fixer wrote.
    {"id": "verifier", "harness": "verify", "depends_on": ["fixer"],
     "test_command": "python3 -m unittest test_calc -v"},
]


def _wait_for_workers(pids, *, timeout_s=NODE_TIMEOUT_S):
    """Let the drained workers exit before the worktree is inspected."""
    end = time.monotonic() + timeout_s
    for pid in pids:
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(1.0)


def test_a_real_six_node_dag_finishes_on_free_models(tmp_path, monkeypatch):
    repo = _sample_repo(tmp_path)
    # worktree_create shells out to git with no cwd, so the worker's cwd is the
    # repo the branch comes off. Workers inherit this process's.
    monkeypatch.chdir(repo)

    dbp = str(tmp_path / "invivo.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    name = f"invivo-{os.getpid()}"
    nodes = [dict(n, timeout_s=NODE_TIMEOUT_S) for n in NODES]
    dag = st.build_dag(conn, dbp, nodes, name=name, base="main")
    assert len(dag["nodes"]) == 5 and dag["worktree"]   # six with the worktree

    pids = st.start_workers(dbp, 3, native=True, drain=True)
    verdict = st.wait_for_settle(conn, timeout_s=st.WAIT_CAP_S, poll=5.0)
    _wait_for_workers(pids)

    rows = {r["id"]: r for r in db.all_tasks(conn)}
    report = [(r["task_type"], json.loads(r["payload"] or "{}").get("agent_id"),
               r["status"], r["input_tokens"], r["output_tokens"])
              for r in rows.values()]
    print("\n".join(f"  {t:16} {a or '-':10} {s:10} "
                    f"in={i or 0:<8} out={o or 0}" for t, a, s, i, o in report))
    totals = db.usage_totals(conn)
    print(f"  TOTAL {totals}")

    # 1. every node green
    failed = [r for r in rows.values() if r["status"] != "completed"]
    assert not failed, [(r["id"], r["task_type"], r["output_artifact"])
                        for r in failed]
    assert verdict["satisfied"], verdict["failures"]

    # 2. the P2P channel carried real traffic
    msgs = db.recent_messages(conn, 20)
    assert msgs, "no agent used the P2P channel"

    # 3. the run's tokens are on the books
    assert totals["input_tokens"] > 0 and totals["output_tokens"] > 0
    assert totals["baseline_usd"] > 0     # the spend a single session would owe
    priced = [r for r in rows.values() if r["model_used"]]
    assert priced, "no node recorded which model ran it"

    # 4. the edge carried a handoff block, not the parent's whole transcript
    scout_row = next(r for r in rows.values()
                     if json.loads(r["payload"] or "{}").get("agent_id") == "scout")
    assert handlers.HANDOFF_MARK in (scout_row["output_artifact"] or ""), \
        scout_row["output_artifact"]

    # 5. the work itself survived, not just the agents' word for it
    src = _git(["show", f"{name}:calc.py"], repo)
    for fn in ("def add", "def subtract", "def multiply"):
        assert fn in src, src
    assert "class" in _git(["show", f"{name}:test_calc.py"], repo)
