"""DAG dependencies, artifact hand-off, the fixer_loop, and `cli.py verify`."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import db  # noqa: E402
import handlers  # noqa: E402


def _proc(returncode=0, stdout="ok", stderr=""):
    m = MagicMock()
    m.returncode, m.stdout, m.stderr = returncode, stdout, stderr
    return m


# --- dependency blocking ----------------------------------------------------

def test_dependent_task_blocked_until_parent_completes(tmp_path):
    dbp = str(tmp_path / "dag.db")
    db.init_db(dbp)
    conn = db.connect(dbp)

    parent = db.add_task(conn, "echo", "a")
    child = db.add_task(conn, "echo", "b", depends_on=parent)

    # child is parked while parent is pending; only parent is claimable.
    first = db.claim_task(conn, "w0")
    assert first["id"] == parent
    assert db.claim_task(conn, "w0") is None, "child claimed before parent done"

    db.complete_task(conn, parent, artifact="A")
    now_claimable = db.claim_task(conn, "w0")
    assert now_claimable is not None and now_claimable["id"] == child
    conn.close()


def test_multi_dep_needs_all_parents(tmp_path):
    dbp = str(tmp_path / "multi.db")
    db.init_db(dbp)
    conn = db.connect(dbp)

    p1 = db.add_task(conn, "echo", "1")
    p2 = db.add_task(conn, "echo", "2")
    child = db.add_task(conn, "echo", "c", depends_on=[p1, p2])

    db.claim_task(conn, "w")          # p1 or p2
    db.claim_task(conn, "w")          # the other
    db.complete_task(conn, p1)
    assert db.claim_task(conn, "w") is None, "child ran with one dep still open"
    db.complete_task(conn, p2)
    assert db.claim_task(conn, "w")["id"] == child
    conn.close()


def test_failed_dep_blocks_child_forever(tmp_path):
    dbp = str(tmp_path / "faildep.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    p = db.add_task(conn, "fail", "x")
    child = db.add_task(conn, "echo", "c", depends_on=p)
    db.claim_task(conn, "w")
    with db.immediate(conn) as c:
        c.execute("UPDATE tasks SET status='failed' WHERE id=?", (p,))
    assert db.claim_task(conn, "w") is None
    conn.close()


# --- artifact pass-through --------------------------------------------------

def test_artifact_passes_parent_to_child(tmp_path):
    dbp = str(tmp_path / "art.db")
    db.init_db(dbp)
    conn = db.connect(dbp)

    scout = db.add_task(conn, "pi", "scout")
    builder = db.add_task(conn, "pi", "build", depends_on=scout)
    db.complete_task(conn, scout, artifact="# CONTEXT.md\nuse module X")

    passed = db.dep_artifacts(conn, builder)
    assert passed == {scout: "# CONTEXT.md\nuse module X"}
    conn.close()


@patch("handlers.subprocess.run")
def test_context_injected_into_agent_prompt(run):
    run.return_value = _proc(stdout="done")
    handlers.run("pi", json.dumps({"prompt": "build"}),
                 {1: "# CONTEXT.md payload"})
    sent = run.call_args.args[0][-1]  # last arg is the prompt
    assert "build" in sent and "# CONTEXT.md payload" in sent


# --- fixer_loop -------------------------------------------------------------

@patch("handlers.subprocess.run")
def test_fixer_passes_immediately(run):
    run.return_value = _proc(returncode=0)
    out = handlers.run("fixer_loop", json.dumps(
        {"test_command": "pytest", "cwd": "/w", "max_attempts": 3}))
    assert "attempt 1" in out
    assert run.call_count == 1  # tests only, no agent call


@patch("handlers.subprocess.run")
def test_fixer_retries_then_succeeds(run):
    # test fails, agent runs, test fails, agent runs, test passes -> attempt 3
    run.side_effect = [
        _proc(returncode=1, stdout="FAIL"), _proc(returncode=0),   # try 1 + fix
        _proc(returncode=1, stdout="FAIL"), _proc(returncode=0),   # try 2 + fix
        _proc(returncode=0),                                       # try 3 passes
    ]
    out = handlers.run("fixer_loop", json.dumps(
        {"test_command": "pytest", "agent_type": "pi", "cwd": "/w",
         "max_attempts": 3}))
    assert "attempt 3" in out
    assert run.call_count == 5


@patch("handlers.subprocess.run")
def test_fixer_exhausts_and_raises(run):
    run.return_value = _proc(returncode=1, stdout="still broken")
    try:
        handlers.run("fixer_loop", json.dumps(
            {"test_command": "pytest", "cwd": "/w", "max_attempts": 2}))
    except RuntimeError as e:
        assert "after 2 attempts" in str(e)
    else:
        raise AssertionError("exhausted fixer_loop should raise")
    # 2 test runs + 1 agent fix between them
    assert run.call_count == 3


# --- worktree handlers ------------------------------------------------------

@patch("handlers.subprocess.run")
def test_worktree_create_returns_path(run):
    run.return_value = _proc(returncode=0)
    path = handlers.run("worktree_create",
                        json.dumps({"branch": "feat-x", "base": "main"}))
    assert path == "/tmp/worktrees/feat-x"
    assert run.call_args.args[0][:3] == ["git", "worktree", "add"]


# --- WAL claim concurrency (threaded workers) -------------------------------

def test_wal_claim_no_double_claim_threaded(tmp_path):
    dbp = str(tmp_path / "wal.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    total = 200
    for i in range(total):
        db.add_task(conn, "echo", str(i))
    conn.close()

    claimed, errors = [], []
    lock = threading.Lock()

    def worker(agent):
        try:
            c = db.connect(dbp)   # each thread its own connection
            while True:
                t = db.claim_task(c, agent)
                if t is None:
                    break
                with lock:
                    claimed.append(t["id"])
            c.close()
        except Exception as e:  # a lock error would land here
            errors.append(f"{agent}: {e!r}")

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert errors == [], errors
    assert len(claimed) == total, f"claimed {len(claimed)} of {total}"
    assert len(set(claimed)) == total, "a task was double-claimed under WAL"


# --- P2P inter-agent messaging ----------------------------------------------

def test_send_and_poll_messages(tmp_path):
    dbp = str(tmp_path / "msg.db")
    db.init_db(dbp)
    conn = db.connect(dbp)

    db.send_inter_agent_message(conn, "scout", "builder", "context ready")
    db.send_inter_agent_message(conn, "scout", "builder", "used module X")
    db.send_inter_agent_message(conn, "scout", "fixer", "not for builder")

    inbox = db.poll_inter_agent_messages(conn, "builder")
    assert [m["content"] for m in inbox] == ["context ready", "used module X"]
    # polling marks them read, so a second poll is empty.
    assert db.poll_inter_agent_messages(conn, "builder") == []
    # other recipient's mail is untouched.
    assert len(db.poll_inter_agent_messages(conn, "fixer")) == 1
    conn.close()


@patch("handlers.subprocess.run")
def test_fixer_queries_upstream_after_two_fails(run, tmp_path):
    # test command (a str) always fails; agent/git calls (a list) succeed.
    run.side_effect = lambda *a, **k: (
        _proc(returncode=0, stdout="clarified")
        if isinstance(a[0], list)
        else _proc(returncode=1, stdout="assertion error"))
    dbp = str(tmp_path / "p2p.db")
    db.init_db(dbp)
    payload = json.dumps({
        "test_command": "pytest", "agent_type": "pi", "cwd": "/w",
        "max_attempts": 3, "db": dbp, "upstream": "builder-x",
        "agent_id": "fixer-x",
    })
    try:
        handlers.run("fixer_loop", payload)
    except RuntimeError:
        pass
    else:
        raise AssertionError("fixer should exhaust and raise")

    conn = db.connect(dbp)
    # fixer asked builder, builder answered fixer -> two channel rows.
    to_builder = conn.execute(
        "SELECT * FROM messages WHERE sender_id='fixer-x' AND recipient_id='builder-x'"
    ).fetchall()
    to_fixer = conn.execute(
        "SELECT * FROM messages WHERE sender_id='builder-x' AND recipient_id='fixer-x'"
    ).fetchall()
    assert len(to_builder) == 1, "fixer never queried upstream"
    assert len(to_fixer) == 1, "upstream reply not recorded"
    # exhausted fixer quarantines its worktree for review.
    wt = conn.execute("SELECT state FROM worktrees WHERE path='/w'").fetchone()
    assert wt and wt["state"] == "quarantined"
    conn.close()


# --- worktree GC: quarantine vs auto-clean ----------------------------------

@patch("handlers.subprocess.run")
def test_gc_cleans_passed_keeps_quarantined(run, tmp_path):
    run.return_value = _proc(returncode=0)  # git worktree remove "succeeds"
    dbp = str(tmp_path / "gc.db")
    db.init_db(dbp)
    conn = db.connect(dbp)

    # passed worktree: one completed task, state active.
    good = "/tmp/worktrees/good"
    db.set_worktree(conn, good, "active", branch="good")
    gt = db.add_task(conn, "echo", "x", worktree_path=good)
    db.complete_task(conn, gt)

    # failed worktree: a failed task, state quarantined.
    bad = "/tmp/worktrees/bad"
    db.set_worktree(conn, bad, "quarantined", branch="bad")
    bt = db.add_task(conn, "fail", "x", worktree_path=bad)
    with db.immediate(conn) as c:
        c.execute("UPDATE tasks SET status='failed' WHERE id=?", (bt,))
    conn.close()

    # default gc: clean passed, keep quarantined.
    class A:  # tiny args stand-in
        db = dbp
        failed = False
    from cli import cmd_gc
    cmd_gc(A())

    conn = db.connect(dbp)
    assert db.worktrees(conn, "cleaned")[0]["path"] == good
    assert [w["path"] for w in db.worktrees(conn, "quarantined")] == [bad]
    conn.close()

    # gc --failed: now the quarantined one goes too.
    A.failed = True
    cmd_gc(A())
    conn = db.connect(dbp)
    assert {w["path"] for w in db.worktrees(conn, "cleaned")} == {good, bad}
    conn.close()


# --- native tmux-pane execution ---------------------------------------------

def test_native_command_pi_with_p2p():
    cmd = handlers.native_command("pi", json.dumps({
        "prompt": "build it", "model": "hy3", "agent_id": "fixer-x",
        "db": "/tmp/x.db", "p2p": True}))
    assert cmd.startswith("export SILICORISM_DB=")
    assert "SILICORISM_SELF=fixer-x" in cmd
    assert "silicorism-msg()" in cmd
    # full TUI (no -p) with the autoexit extension driving exit + artifact
    assert "autoexit.ts --no-session --model opencode/hy3-free" in cmd
    assert " -p " not in cmd
    assert "build it" in cmd


def test_native_command_claude_and_nonnative():
    c = handlers.native_command("claude", json.dumps({"prompt": "go"}))
    assert "claude -p" in c and "go" in c
    # no agent_id/db -> no P2P prelude
    assert "silicorism-msg()" not in c
    # non-agent task types stay in-process
    assert handlers.native_command("shell", "echo hi") is None
    assert handlers.native_command("worktree_create", "{}") is None


def test_native_command_claude_custom_model():
    c = handlers.native_command("claude", json.dumps({
        "prompt": "review", "model": "opus-4.8"}))
    assert "claude -p --model opus-4.8" in c


def test_skill_injection_into_prompt(tmp_path):
    import skills
    sk = tmp_path / ".claude" / "skills"
    sk.mkdir(parents=True)
    (sk / "tdd.md").write_text("WRITE THE TEST FIRST")
    cmd = handlers.native_command("pi", json.dumps({
        "prompt": "build it", "cwd": str(tmp_path), "skills": ["tdd"]}))
    assert "WRITE THE TEST FIRST" in cmd
    assert "--- Skills ---" in cmd
    # unresolved skills are silently dropped, prompt still builds
    assert skills.load_skills(["ghost"], cwd=str(tmp_path)) == ""


@patch("handlers.subprocess.run")
def test_run_task_in_pane_captures_exit(run, tmp_path):
    import tmux_orchestrator as tmux
    run.return_value = _proc(returncode=0)
    sent = str(tmp_path / "t.exit")
    name = tmux.run_task_in_pane(1, "pi", "/w", "pi -p 'x'", sent)
    assert name == "task-1-pi"
    sends = [c.args[0] for c in run.call_args_list if "send-keys" in c.args[0]]
    # the shell is handed a script path, never the command itself
    script = [s[-2].split()[-1] for s in sends if s[-2].endswith(".sh")]
    assert script, sends
    body = Path(script[0]).read_text()
    assert "echo $? >" in body and "| tee " not in body, body
    assert any("pipe-pane" in " ".join(c.args[0]) for c in run.call_args_list)
    Path(script[0]).unlink()


def test_wait_for_exit_reads_sentinel(tmp_path):
    import tmux_orchestrator as tmux
    sent = str(tmp_path / "s.exit")
    Path(sent).write_text("0\n")
    assert tmux.wait_for_exit(sent, timeout=1) == 0
    Path(sent).write_text("2\n")
    assert tmux.wait_for_exit(sent, timeout=1) == 2
    Path(sent).unlink()
    assert tmux.wait_for_exit(sent, timeout=0.2) is None


@patch("worker.tmux")
def test_worker_native_completes_and_fails(mock_tmux, tmp_path):
    import worker
    dbp = str(tmp_path / "nat.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    mock_tmux.sentinel_path.side_effect = lambda tid: str(tmp_path / f"{tid}.exit")
    mock_tmux.log_path.side_effect = lambda tid: str(tmp_path / f"{tid}.log")
    mock_tmux.run_task_in_pane.return_value = "task-1-pi"
    mock_tmux.read_log_tail.return_value = "scout wrote CONTEXT.md"

    tid = db.add_task(conn, "pi", json.dumps({"prompt": "x", "cwd": "/w"}))
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    mock_tmux.wait_for_exit.return_value = 0
    worker._run_native(conn, task, "w0", "pi -p 'x'")
    assert db.counts(conn)["completed"] == 1
    # fallback window is named task-<id>-<type>, so it must be renamed by name
    mock_tmux.mark_window_done.assert_called_with("task-1-pi", failed=False)
    # captured log tail becomes the artifact for downstream deps
    art = conn.execute("SELECT output_artifact FROM tasks WHERE id=?",
                       (tid,)).fetchone()["output_artifact"]
    assert art == "scout wrote CONTEXT.md"

    tid2 = db.add_task(conn, "pi", json.dumps({"prompt": "y", "cwd": "/w"}))
    task2 = conn.execute("SELECT * FROM tasks WHERE id=?", (tid2,)).fetchone()
    mock_tmux.wait_for_exit.return_value = 3  # non-zero -> raise
    try:
        worker._run_native(conn, task2, "w0", "pi -p 'y'")
    except RuntimeError as e:
        assert "exit 3" in str(e)
    else:
        raise AssertionError("non-zero pane exit must raise")
    conn.close()


# --- silicorism_tools bridge -----------------------------------------------------

def test_build_pipeline_wires_deps_and_p2p(tmp_path):
    import silicorism_tools
    dbp = str(tmp_path / "bp.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    p = silicorism_tools.build_pipeline(conn, dbp, "feat", "do x")
    t = p["tasks"]
    dep = json.loads(conn.execute(
        "SELECT depends_on FROM tasks WHERE id=?", (t["fixer"],)).fetchone()["depends_on"])
    assert dep == [t["builder"]]
    fp = json.loads(conn.execute(
        "SELECT payload FROM tasks WHERE id=?", (t["fixer"],)).fetchone()["payload"])
    assert fp["upstream"] == "builder-feat" and fp["db"] == dbp
    conn.close()


def test_start_workers_spawn_injectable(tmp_path):
    import silicorism_tools
    calls = []

    def fake_spawn(db_path, agent, *, native, drain):
        calls.append((agent, native, drain))
        return 4242

    pids = silicorism_tools.start_workers(str(tmp_path / "s.db"), 3, _spawn=fake_spawn)
    assert pids == [4242, 4242, 4242]
    assert calls[0] == ("worker-0", True, True)


# --- P2P routing from the native agent's shell ------------------------------

def test_cli_msg_routes_via_env(tmp_path):
    import os
    from cli import cmd_msg
    dbp = str(tmp_path / "msg.db")
    db.init_db(dbp)

    class Send:
        action, target, text, db, self_id = "send", "builder", "need spec", None, None

    os.environ["SILICORISM_DB"], os.environ["SILICORISM_SELF"] = dbp, "fixer"
    try:
        cmd_msg(Send())
        conn = db.connect(dbp)
        rows = db.poll_inter_agent_messages(conn, "builder")
        assert len(rows) == 1 and rows[0]["sender_id"] == "fixer"
        assert rows[0]["content"] == "need spec"
        conn.close()
    finally:
        del os.environ["SILICORISM_DB"], os.environ["SILICORISM_SELF"]


# --- dynamic DAG + verify -----------------------------------------------------

def test_build_dag_wires_deps_and_attrs(tmp_path):
    import silicorism_tools
    dbp = str(tmp_path / "dag.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    nodes = [
        {"id": "scout", "prompt": "recon", "model": "opencode/deepseek-v4-flash-free",
         "thinking": "high", "skills": ["tdd"]},
        {"id": "build", "prompt": "impl", "depends_on": ["scout"],
         "harness": "claude"},
    ]
    out = silicorism_tools.build_dag(conn, dbp, nodes)
    assert set(out["nodes"]) == {"scout", "build"}
    # build depends on scout's db id
    dep = json.loads(conn.execute("SELECT depends_on FROM tasks WHERE id=?",
                                  (out["nodes"]["build"],)).fetchone()["depends_on"])
    assert dep == [out["nodes"]["scout"]]
    # per-node attrs land in the payload
    sp = json.loads(conn.execute("SELECT payload FROM tasks WHERE id=?",
                                 (out["nodes"]["scout"],)).fetchone()["payload"])
    assert sp["model"] == "opencode/deepseek-v4-flash-free"
    assert sp["thinking"] == "high" and sp["skills"] == ["tdd"]
    # harness becomes the task_type
    bt = conn.execute("SELECT task_type FROM tasks WHERE id=?",
                      (out["nodes"]["build"],)).fetchone()["task_type"]
    assert bt == "claude"
    conn.close()


def test_build_dag_worktree_wrap(tmp_path):
    import silicorism_tools
    dbp = str(tmp_path / "w.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    out = silicorism_tools.build_dag(
        conn, dbp, [{"id": "only", "prompt": "go"}], name="feat")
    # root node waits on the worktree; cleanup waits on the leaf
    assert "worktree" in out and "cleanup" in out
    root_dep = json.loads(conn.execute("SELECT depends_on FROM tasks WHERE id=?",
                                       (out["nodes"]["only"],)).fetchone()["depends_on"])
    assert root_dep == [out["worktree"]]
    clean_dep = json.loads(conn.execute("SELECT depends_on FROM tasks WHERE id=?",
                                        (out["cleanup"],)).fetchone()["depends_on"])
    assert clean_dep == [out["nodes"]["only"]]
    conn.close()


def test_build_dag_rejects_cycle_and_bad_dep(tmp_path):
    import silicorism_tools
    dbp = str(tmp_path / "c.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    for bad in (
        [{"id": "a", "prompt": "x", "depends_on": ["b"]},
         {"id": "b", "prompt": "y", "depends_on": ["a"]}],           # cycle
        [{"id": "a", "prompt": "x", "depends_on": ["ghost"]}],       # unknown dep
        [{"id": "a", "prompt": "x", "harness": "bogus"}],            # bad harness
    ):
        try:
            silicorism_tools.build_dag(conn, dbp, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad} should have raised")
    conn.close()


def test_verify_status_transitions(tmp_path):
    import silicorism_tools
    dbp = str(tmp_path / "vs.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    # pending work -> not satisfied
    t = db.add_task(conn, "echo", "hi")
    v = silicorism_tools.verify_status(conn)
    assert v["satisfied"] is False and v["active"] == 1
    # all complete -> satisfied
    db.complete_task(conn, t, artifact="ok")
    assert silicorism_tools.verify_status(conn)["satisfied"] is True
    # a failed task surfaces with its last error
    t2 = db.add_task(conn, "fail", "boom")
    for _ in range(4):
        db.fail_task(conn, t2)  # exhaust retries -> failed
    db.log(conn, t2, "w0", "error: boom", level="error")
    v3 = silicorism_tools.verify_status(conn)
    assert v3["satisfied"] is False
    assert v3["failures"][0]["id"] == t2 and "boom" in v3["failures"][0]["error"]
    conn.close()


# --- cli default_db resolution ----------------------------------------------

def test_default_db_git_vs_nongit(tmp_path):
    import cli
    # inside a git repo -> <root>/.git/silicorism.db
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
    got = cli.default_db(str(repo))
    assert got == str((repo / ".git" / "silicorism.db"))
    # outside git -> ~/.config/silicorism/repos/<slug>/silicorism.db
    plain = tmp_path / "Plain Dir"
    plain.mkdir()
    got2 = cli.default_db(str(plain))
    assert got2.endswith("/silicorism.db")
    assert "/.config/silicorism/repos/" in got2
    assert ".git" not in got2


# --- cli verify -------------------------------------------------------------

def test_cli_verify_lists_binaries():
    out = subprocess.run(
        [sys.executable, str(ROOT / "cli.py"), "verify"],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    for binary in ("git", "python3", "pi", "claude"):
        assert binary in out.stdout


if __name__ == "__main__":
    import inspect
    import tempfile
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            # unwrap @patch so we can see the real signature's params
            wants_tmp = "tmp_path" in inspect.signature(
                getattr(fn, "__wrapped__", fn)).parameters
            if wants_tmp:
                # keyword so @patch's appended mock still fills the first param
                with tempfile.TemporaryDirectory() as d:
                    fn(tmp_path=Path(d))
            else:
                fn()
    print("test_workflow OK")


def test_a_drain_worker_waits_while_work_is_still_in_flight(tmp_path):
    """An empty poll means 'nothing claimable', not 'nothing left to do'.

    A worker that exits while a long scout is running leaves its fan-out to be
    executed serially by whoever is left.
    """
    import time

    import worker

    dbp = str(tmp_path / "drain.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    parent = db.add_task(conn, "pi", "{}")
    db.add_task(conn, "pi", "{}", depends_on=parent)
    db.claim_task(conn, "other-agent")  # parent is processing, child blocked

    with patch("signal.signal"):  # run_worker installs handlers; not in a thread
        t = threading.Thread(
            target=worker.run_worker, args=(dbp, "drainer"), daemon=True,
            kwargs={"idle_sleep": 0.01, "max_idle_loops": 1})
        t.start()
        time.sleep(0.3)
        alive_while_busy = t.is_alive()
        worker._STOP = True  # end the loop without running a real agent
        t.join(timeout=5)
        worker._STOP = False

    assert alive_while_busy, "worker exited while a task was still processing"
    assert not t.is_alive()
    conn.close()


def test_a_drain_worker_exits_once_the_queue_is_empty(tmp_path):
    import worker

    dbp = str(tmp_path / "drain2.db")
    db.init_db(dbp)
    with patch("signal.signal"):
        t = threading.Thread(
            target=worker.run_worker, args=(dbp, "drainer"), daemon=True,
            kwargs={"idle_sleep": 0.01, "max_idle_loops": 1})
        t.start()
        t.join(timeout=5)
    assert not t.is_alive(), "drain worker hung on an empty queue"
