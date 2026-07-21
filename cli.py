"""Orchestrator CLI: init the DB, enqueue tasks, run a worker pool, watch status.

    python cli.py init --db silicorism.db
    python cli.py add  --db silicorism.db --type sleep --payload 0.2 --priority 5
    python cli.py run  --db silicorism.db --workers 4 --drain
    python cli.py status --db silicorism.db --watch
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import re
import shutil
import signal
import subprocess
import time

import db
import handlers
import silicorism_tools
import tmux_orchestrator as tmux
from worker import run_worker

_POOL: list[mp.Process] = []


def _git_root(cwd: str | None = None) -> str | None:
    """Top level of the git repo containing cwd, or None if not in one."""
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, cwd=cwd, timeout=10)
    except OSError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def default_db(cwd: str | None = None) -> str:
    """Per-repo DB path so `silicorism` works from any CWD without --db.

    Inside a git repo: <root>/.git/silicorism.db (kept out of the work tree).
    Otherwise: ~/.config/silicorism/repos/<cwd-slug>/silicorism.db.
    """
    root = _git_root(cwd)
    if root:
        return os.path.join(root, ".git", "silicorism.db")
    slug = re.sub(r"[^a-z0-9]+", "-", (cwd or os.getcwd()).lower()).strip("-") or "repo"
    d = os.path.expanduser(os.path.join("~/.config/silicorism/repos", slug))
    return os.path.join(d, "silicorism.db")


def cmd_init(args) -> None:
    db.init_db(args.db)
    print(f"initialized {args.db}")


def cmd_add(args) -> None:
    conn = db.connect(args.db)
    try:
        tid = db.add_task(conn, args.type, args.payload,
                          priority=args.priority, max_retries=args.max_retries)
        print(f"task {tid} queued ({args.type})")
    finally:
        conn.close()


def _spawn(db_path, agent_id, drain):
    # each worker is its own OS process with its own SQLite connection
    max_idle = 3 if drain else 0
    p = mp.Process(target=run_worker, args=(db_path, agent_id),
                   kwargs={"max_idle_loops": max_idle}, name=agent_id)
    p.start()
    return p


def _shutdown(_signum=None, _frame=None):
    for p in _POOL:
        if p.is_alive():
            os.kill(p.pid, signal.SIGTERM)


def cmd_run(args) -> None:
    db.init_db(args.db)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    for i in range(args.workers):
        _POOL.append(_spawn(args.db, f"worker-{i}", args.drain))
    print(f"spawned {args.workers} workers on {args.db} "
          f"({'drain' if args.drain else 'persistent'} mode)")

    conn = db.connect(args.db)
    try:
        while any(p.is_alive() for p in _POOL):
            if not args.quiet:
                _print_status(conn, inline=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        _shutdown()
    finally:
        for p in _POOL:
            p.join()
        conn.close()
    if not args.quiet:
        print()
    print("all workers stopped")


def _print_status(conn, *, inline=False) -> None:
    c = db.counts(conn)
    line = (f"pending={c['pending']} processing={c['processing']} "
            f"completed={c['completed']} failed={c['failed']}")
    if inline:
        print(f"\r{line}   ", end="", flush=True)
    else:
        print(line)


def cmd_status(args) -> None:
    conn = db.connect(args.db)
    try:
        while True:
            c = db.counts(conn)
            hbs = db.heartbeats(conn)
            if args.watch:
                print("\033[2J\033[H", end="")  # clear screen
            print(f"tasks  pending={c['pending']}  processing={c['processing']}  "
                  f"completed={c['completed']}  failed={c['failed']}")
            print(f"{'agent':<12} {'status':<9} {'task':<6} last_seen")
            for h in hbs:
                print(f"{h['agent_id']:<12} {h['status']:<9} "
                      f"{str(h['current_task_id'] or '-'):<6} {h['last_seen']}")
            if not args.watch:
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


def cmd_submit_feature(args) -> None:
    """Enqueue a 5-task DAG: worktree -> scout -> builder -> fixer -> cleanup."""
    db.init_db(args.db)
    conn = db.connect(args.db)
    try:
        p = silicorism_tools.build_pipeline(
            conn, args.db, args.name, args.prompt, base=args.base,
            test_command=args.test_command, max_attempts=args.max_attempts)
    finally:
        conn.close()
    t = p["tasks"]
    print(f"feature '{args.name}' queued: worktree={t['worktree']} "
          f"scout={t['scout']} builder={t['builder']} fixer={t['fixer']} "
          f"cleanup={t['cleanup']}")


def cmd_supervise(args) -> None:
    """Window 0 = orchestrator agent (pane) + live dashboard (split pane)."""
    db.init_db(args.db)
    ext = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "extensions", "silicorism.ts")
    tmux.supervisor_layout(args.db, agent=args.agent,
                           extension=ext if os.path.exists(ext) else None,
                           launch=args.launch)
    print(f"tmux session '{tmux.SESSION}' up (window 0: {args.agent} + dashboard).\n"
          f"  tmux attach -t {tmux.SESSION}\n"
          "Run workers with SILICORISM_NATIVE=1 for a live pi/claude pane per task.")


def _dashboard_frame(conn) -> str:
    c = db.counts(conn)
    lines = ["\033[2J\033[H", "== silicorism supervisor ==",
             f"tasks  pending={c['pending']}  processing={c['processing']}  "
             f"completed={c['completed']}  failed={c['failed']}", "",
             "recent P2P messages:"]
    msgs = db.recent_messages(conn, 8)
    if not msgs:
        lines.append("  (none)")
    for m in reversed(msgs):
        body = (m["content"] or "").replace("\n", " ")[:70]
        lines.append(f"  [{m['status']:<6}] {m['sender_id']}->{m['recipient_id']}: {body}")
    return "\n".join(lines)


def cmd_dashboard(args) -> None:
    """Polling status + P2P message view (runs inside supervisor window 0)."""
    conn = db.connect(args.db)
    try:
        while True:
            print(_dashboard_frame(conn), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


def cmd_logs(args) -> None:
    """Print a task's execution logs; --follow tails new rows (for tmux panes)."""
    conn = db.connect(args.db)
    last_id = 0
    try:
        while True:
            rows = conn.execute(
                "SELECT id, level, message, timestamp FROM execution_logs "
                "WHERE task_id=? AND id>? ORDER BY id",
                (args.task, last_id)).fetchall()
            for r in rows:
                last_id = r["id"]
                print(f"{r['timestamp']} [{r['level']}] {r['message']}", flush=True)
            if not args.follow:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


def cmd_gc(args) -> None:
    """Reclaim worktrees whose tasks are done. --failed also clears quarantined."""
    conn = db.connect(args.db)
    try:
        res = silicorism_tools.gc_worktrees(conn, args.db, failed=args.failed)
    finally:
        conn.close()
    for p, why in res["cleaned"]:
        print(f"cleaned {p} ({why})")
    for p, why in res["kept"]:
        print(f"kept    {p} ({why})")
    print(f"gc done: {len(res['cleaned'])} cleaned, {len(res['kept'])} kept")


def cmd_msg(args) -> None:
    """P2P channel from a native agent's shell: `msg send <to> <text>` / `msg poll`.

    Recipient/self and db come from flags or SILICORISM_SELF / SILICORISM_DB env, so the
    injected `silicorism-msg` shell alias needs no arguments beyond the verb.
    """
    dbp = args.db or os.environ.get("SILICORISM_DB")
    me = args.self_id or os.environ.get("SILICORISM_SELF")
    if not dbp or not me:
        raise SystemExit("msg: need --db/SILICORISM_DB and --self/SILICORISM_SELF")
    conn = db.connect(dbp)
    try:
        if args.action == "send":
            mid = db.send_inter_agent_message(conn, me, args.target, args.text)
            print(f"sent #{mid} -> {args.target}")
        else:  # poll
            for m in db.poll_inter_agent_messages(conn, me):
                print(f"[{m['created_at']}] {m['sender_id']}: {m['content']}")
    finally:
        conn.close()


def cmd_verify(args) -> None:
    """Check that required binaries are reachable on PATH."""
    print(f"{'binary':<10} {'status':<9} path")
    ok = True
    for binary in ("pi", "claude", "git", "python3"):
        where = shutil.which(binary)
        status = "ok" if where else "MISSING"
        ok = ok and where is not None
        print(f"{binary:<10} {status:<9} {where or '-'}")
    print("all present" if ok else "some binaries missing")


def cmd_reset(args) -> None:
    """Re-arm stuck 'processing' tasks (e.g. after a hard crash)."""
    conn = db.connect(args.db)
    try:
        with db.immediate(conn) as x:
            cur = x.execute(
                "UPDATE tasks SET status='pending', agent_id=NULL, updated_at=? "
                "WHERE status='processing'", (db.now(),))
        print(f"requeued {cur.rowcount} stuck task(s)")
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="silicorism")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_db(sp):
        sp.add_argument("--db", default=None,
                        help="SQLite path (default: <repo>/.git/silicorism.db)")
        return sp

    with_db(sub.add_parser("init")).set_defaults(fn=cmd_init)

    a = with_db(sub.add_parser(
        "add",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "AI task examples:\n"
            "  python cli.py add --db silicorism.db --type pi \\\n"
            "    --payload '{\"model\": \"opencode/deepseek-v4-flash-free\", "
            "\"thinking\": \"high\", \"prompt\": \"Scan repo and build CONTEXT.md\"}'\n"
            "  python cli.py add --db silicorism.db --type claude \\\n"
            "    --payload '{\"prompt\": \"Review git diff and summarize changes\"}'"
        ),
    ))
    a.add_argument("--type", required=True, choices=sorted(handlers.HANDLERS),
                   metavar="TYPE")
    a.add_argument("--payload")
    a.add_argument("--priority", type=int, default=0)
    a.add_argument("--max-retries", type=int, default=3)
    a.set_defaults(fn=cmd_add)

    r = with_db(sub.add_parser("run"))
    r.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    r.add_argument("--drain", action="store_true",
                   help="exit once the queue is empty instead of running forever")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(fn=cmd_run)

    s = with_db(sub.add_parser("status"))
    s.add_argument("--watch", action="store_true")
    s.set_defaults(fn=cmd_status)

    with_db(sub.add_parser("reset")).set_defaults(fn=cmd_reset)

    sf = with_db(sub.add_parser(
        "submit-feature",
        help="enqueue a full worktree->scout->builder->fixer->cleanup pipeline"))
    sf.add_argument("--name", required=True, help="feature/branch name")
    sf.add_argument("--prompt", required=True, help="what to build")
    sf.add_argument("--base", default="main", help="base branch for the worktree")
    sf.add_argument("--test-command", default="pytest -q")
    sf.add_argument("--max-attempts", type=int, default=3)
    sf.set_defaults(fn=cmd_submit_feature)

    sub.add_parser("verify", help="check pi/claude/git/python3 on PATH").set_defaults(
        fn=cmd_verify)

    sv = with_db(sub.add_parser("supervise", help="tmux session: orchestrator + dashboard"))
    sv.add_argument("--agent", choices=("pi", "claude"), default="pi")
    sv.add_argument("--launch", action="store_true",
                    help="actually start the orchestrator agent (else just lay out panes)")
    sv.set_defaults(fn=cmd_supervise)

    m = sub.add_parser("msg", help="P2P channel (uses SILICORISM_DB/SILICORISM_SELF env)")
    m.add_argument("action", choices=("send", "poll"))
    m.add_argument("target", nargs="?")
    m.add_argument("text", nargs="?")
    m.add_argument("--db")
    m.add_argument("--self", dest="self_id")
    m.set_defaults(fn=cmd_msg)

    d = with_db(sub.add_parser("dashboard", help="polling status/message view"))
    d.add_argument("--interval", type=float, default=1.0)
    d.set_defaults(fn=cmd_dashboard)

    lg = with_db(sub.add_parser("logs", help="print/tail a task's logs"))
    lg.add_argument("--task", type=int, required=True)
    lg.add_argument("--follow", "-f", action="store_true")
    lg.set_defaults(fn=cmd_logs)

    g = with_db(sub.add_parser("gc", help="reclaim finished/failed worktrees"))
    g.add_argument("--failed", action="store_true",
                   help="also remove quarantined worktrees")
    g.set_defaults(fn=cmd_gc)

    args = p.parse_args()
    # Resolve the per-repo default DB for every command except `msg`, which
    # has its own --db/SILICORISM_DB env fallback.
    if args.cmd != "msg" and getattr(args, "db", None) is None:
        args.db = default_db()
    args.fn(args)


if __name__ == "__main__":
    main()
