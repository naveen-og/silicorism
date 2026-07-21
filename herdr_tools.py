"""Harness-agnostic bridge: the operations the Pi extension / Claude tools and
the CLI both call. Pure functions over a db path so they are trivially testable
and identical whether driven from `cli.py`, `pi -e extensions/herdr.ts`, or a
bare shell.
"""

from __future__ import annotations

import json
import os

import db
import handlers

WORKTREE_ROOT = handlers.WORKTREE_ROOT


def build_pipeline(conn, db_path, name, prompt, *, base="main",
                   test_command="pytest -q", max_attempts=3) -> dict:
    """Insert the 5-task DAG (worktree->scout->builder->fixer->cleanup).

    Returns {"name","worktree_path","tasks":{worktree,scout,builder,fixer,cleanup}}.
    """
    path = os.path.join(WORKTREE_ROOT, name)
    t1 = db.add_task(conn, "worktree_create",
                     json.dumps({"branch": name, "base": base, "db": db_path}),
                     worktree_path=path)
    t2 = db.add_task(conn, "pi", json.dumps({
        "model": "deepseek-v4-flash", "cwd": path, "p2p": True,
        "agent_id": f"scout-{name}",
        "prompt": f"Scout the repo for: {prompt}. Write CONTEXT.md.",
    }), depends_on=t1, worktree_path=path)
    t3 = db.add_task(conn, "pi", json.dumps({
        "model": "nemotron", "cwd": path, "p2p": True,
        "agent_id": f"builder-{name}",
        "prompt": f"Builder: implement using the context. {prompt}",
    }), depends_on=t2, worktree_path=path)
    t4 = db.add_task(conn, "fixer_loop", json.dumps({
        "test_command": test_command, "agent_type": "pi", "model": "hy3",
        "cwd": path, "max_attempts": max_attempts, "db": db_path,
        "upstream": f"builder-{name}", "agent_id": f"fixer-{name}",
    }), depends_on=t3, worktree_path=path)
    t5 = db.add_task(conn, "worktree_cleanup",
                     json.dumps({"worktree_path": path, "branch": name,
                                 "db": db_path}),
                     depends_on=t4, worktree_path=path)
    return {"name": name, "worktree_path": path,
            "tasks": {"worktree": t1, "scout": t2, "builder": t3,
                      "fixer": t4, "cleanup": t5}}


def gc_worktrees(conn, db_path, *, failed=False) -> dict:
    """Reclaim worktrees whose tasks are done. `failed` also clears quarantined.

    Returns {"cleaned":[(path,why)], "kept":[(path,why)]}.
    """
    cleaned, kept = [], []
    for wt in db.worktrees(conn):
        path, branch, state = wt["path"], wt["branch"], wt["state"]
        if state == "cleaned":
            continue
        st = db.worktree_task_status(conn, path)
        if st["pending"] + st["processing"] > 0:
            kept.append((path, "in-use"))
            continue
        bad = st["failed"] > 0 or state == "quarantined"
        if bad and not failed:
            kept.append((path, "quarantined"))
            continue
        try:
            handlers.worktree_cleanup(json.dumps(
                {"worktree_path": path, "branch": branch, "db": db_path}))
            cleaned.append((path, "quarantined" if bad else "passed"))
        except Exception as e:  # noqa: BLE001 - dir may already be gone
            db.set_worktree(conn, path, "cleaned", branch=branch)
            cleaned.append((path, f"forced ({e})"))
    return {"cleaned": cleaned, "kept": kept}


def get_status(conn) -> dict:
    """Live DAG + P2P snapshot for the orchestrator context."""
    return {
        "tasks": db.counts(conn),
        "agents": [dict(h) for h in db.heartbeats(conn)],
        "messages": [dict(m) for m in db.recent_messages(conn, 20)],
        "worktrees": [dict(w) for w in db.worktrees(conn)],
    }


def start_workers(db_path, count, *, native=True, drain=True, _spawn=None) -> list[int]:
    """Launch `count` worker processes (native pane mode by default).

    `_spawn` is injectable for tests; real callers get multiprocessing workers.
    Returns the list of pids.
    """
    db.init_db(db_path)
    if _spawn is None:
        _spawn = _spawn_worker_process
    pids = []
    for i in range(count):
        pids.append(_spawn(db_path, f"worker-{i}", native=native, drain=drain))
    return pids


def _spawn_worker_process(db_path, agent_id, *, native, drain) -> int:
    import multiprocessing as mp

    from worker import run_worker
    if native:
        os.environ["HERDR_NATIVE"] = "1"  # child inherits; enables pane exec
    p = mp.Process(target=run_worker, args=(db_path, agent_id),
                   kwargs={"max_idle_loops": 3 if drain else 0}, name=agent_id)
    p.start()
    return p.pid


if __name__ == "__main__":
    # self-check: pipeline shape + gc/status over a temp db, no external procs.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        dbp = f"{d}/t.db"
        db.init_db(dbp)
        conn = db.connect(dbp)
        p = build_pipeline(conn, dbp, "demo", "add auth")
        assert list(p["tasks"]) == ["worktree", "scout", "builder", "fixer", "cleanup"]
        assert p["tasks"]["cleanup"] == 5
        st = get_status(conn)
        assert st["tasks"]["pending"] == 5
        assert st["messages"] == [] and st["worktrees"] == []
        db.send_inter_agent_message(conn, "a", "b", "hi")
        assert get_status(conn)["messages"][0]["content"] == "hi"
        assert gc_worktrees(conn, dbp) == {"cleaned": [], "kept": []}
        conn.close()
    print("herdr_tools OK")
