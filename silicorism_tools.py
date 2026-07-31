"""Harness-agnostic bridge: the operations the Pi extension / Claude tools and
the CLI both call. Pure functions over a db path so they are trivially testable
and identical whether driven from `cli.py`, `pi -e extensions/silicorism.ts`, or a
bare shell.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone

import db
import handlers
import tmux_orchestrator as tmux

WORKTREE_ROOT = handlers.WORKTREE_ROOT

# Default per-role models: the OSS pair on bedrock-mantle, matched to role
# strengths — glm-5 reasons (scout), kimi-k2.5 builds, reviews and fixes.
# qwen3-coder-480b is not a default anywhere by operator instruction.
DEFAULT_MODELS = {
    "scout": "bedrock-mantle/zai.glm-5",
    "builder": "bedrock-mantle/moonshotai.kimi-k2.5",
    "fixer": "bedrock-mantle/moonshotai.kimi-k2.5",
}
DEFAULT_THINKING = "high"

# `simple` runs one agent on the coder of the pair; no scout to read a codebase
# that may not exist yet, no fixer loop for a task this size.
SIMPLE_MODEL = "bedrock-mantle/moonshotai.kimi-k2.5"


def _build_standard(conn, db_path, name, prompt, *, base="main",
                    test_command=None, max_attempts=3,
                    merge=False) -> dict:
    """Insert the DAG: worktree->scout->builder->fixer->verify[->merge]->cleanup.

    `verify` re-runs the tests deterministically — cleanup is unreachable unless
    they exit 0. `merge=True` adds a merge-back-to-base node after the gate.
    Returns {"name","worktree_path","tasks":{...}}.
    """
    test_command = test_command or "pytest -q"
    path = os.path.join(WORKTREE_ROOT, name)
    t1 = db.add_task(conn, "worktree_create",
                     json.dumps({"branch": name, "base": base, "db": db_path}),
                     worktree_path=path)
    t2 = db.add_task(conn, "pi", json.dumps({
        "model": DEFAULT_MODELS["scout"], "thinking": DEFAULT_THINKING,
        # db is what makes p2p real: without it native_command emits no
        # silicorism-msg prelude and the agent is told to use a tool it lacks.
        "cwd": path, "p2p": True, "agent_id": f"scout-{name}", "db": db_path,
        "prompt": f"Scout the repo for: {prompt}. Write CONTEXT.md.",
    }), depends_on=t1, worktree_path=path)
    t3 = db.add_task(conn, "pi", json.dumps({
        "model": DEFAULT_MODELS["builder"], "thinking": DEFAULT_THINKING,
        "cwd": path, "p2p": True, "agent_id": f"builder-{name}", "db": db_path,
        "prompt": f"Builder: implement using the context. {prompt}",
    }), depends_on=t2, worktree_path=path)
    t4 = db.add_task(conn, "fixer_loop", json.dumps({
        "test_command": test_command, "agent_type": "pi",
        "model": DEFAULT_MODELS["fixer"], "thinking": DEFAULT_THINKING,
        "cwd": path, "max_attempts": max_attempts, "db": db_path,
        "upstream": f"builder-{name}", "agent_id": f"fixer-{name}",
    }), depends_on=t3, worktree_path=path)
    t5 = db.add_task(conn, "verify",
                     json.dumps({"test_command": test_command, "cwd": path}),
                     depends_on=t4, worktree_path=path, max_retries=0)
    tasks = {"worktree": t1, "scout": t2, "builder": t3, "fixer": t4,
             "verify": t5}
    last = t5
    if merge:
        last = db.add_task(conn, "worktree_merge",
                           json.dumps({"worktree_path": path, "branch": name,
                                       "base": base, "db": db_path}),
                           depends_on=last, worktree_path=path, max_retries=0)
        tasks["merge"] = last
    tasks["cleanup"] = db.add_task(
        conn, "worktree_cleanup",
        json.dumps({"worktree_path": path, "branch": name, "db": db_path}),
        depends_on=last, worktree_path=path)
    return {"name": name, "worktree_path": path, "tasks": tasks}


def _build_simple(conn, db_path, name, prompt, *, test_command=None,
                  cwd=None) -> dict:
    """One agent in the current directory; verify only if there is a command.

    No worktree: a fresh scratch project has no git repo to branch from. No
    unconditional verify gate: it would fail a project that has no tests yet.
    """
    work = cwd or os.getcwd()
    # worktree_path is set even though no worktree is created: gc_worktrees
    # keys "is anyone still working here?" off it, and would otherwise be free
    # to remove a directory this agent is writing to.
    solo = db.add_task(conn, "pi", json.dumps({
        "model": SIMPLE_MODEL, "thinking": DEFAULT_THINKING,
        "cwd": work, "p2p": False, "agent_id": f"solo-{name}", "db": db_path,
        "prompt": prompt,
    }), worktree_path=work)
    tasks = {"solo": solo}
    if test_command:
        tasks["verify"] = db.add_task(
            conn, "verify",
            json.dumps({"test_command": test_command, "cwd": work}),
            depends_on=solo, max_retries=0, worktree_path=work)
    return {"name": name, "worktree_path": work, "tasks": tasks}


SPLIT_NOTE = (
    "Partition the work into exactly TWO slices that touch DISJOINT files. "
    "Write CONTEXT.md, then end your reply with:\n"
    "SLICE A: <files and what to build>\n"
    "SLICE B: <files and what to build>\n"
    "Overlapping slices cause merge conflicts downstream — keep them disjoint."
)


def _build_complex(conn, db_path, name, prompt, *, base="main",
                   test_command=None, max_attempts=3, merge=False) -> dict:
    """Two builders in separate worktrees, joined by a merge + integrator agent.

    The scout partitions the work; each builder receives that partition through
    the existing artifact hand-off (dep_artifacts -> _with_context), so
    builder-b needs no file from worktree-a.
    """
    test_command = test_command or "pytest -q"
    name_a, name_b = f"{name}-a", f"{name}-b"
    path_a = os.path.join(WORKTREE_ROOT, name_a)
    path_b = os.path.join(WORKTREE_ROOT, name_b)
    wt_a = db.add_task(conn, "worktree_create",
                       json.dumps({"branch": name_a, "base": base, "db": db_path}),
                       worktree_path=path_a)
    wt_b = db.add_task(conn, "worktree_create",
                       json.dumps({"branch": name_b, "base": base, "db": db_path}),
                       worktree_path=path_b)
    scout = db.add_task(conn, "pi", json.dumps({
        "model": DEFAULT_MODELS["scout"], "thinking": DEFAULT_THINKING,
        "cwd": path_a, "p2p": True, "agent_id": f"scout-{name}", "db": db_path,
        "prompt": f"Scout the repo for: {prompt}. {SPLIT_NOTE}",
    }), depends_on=[wt_a, wt_b], worktree_path=path_a)
    builder_a = db.add_task(conn, "pi", json.dumps({
        "model": DEFAULT_MODELS["builder"], "thinking": DEFAULT_THINKING,
        "cwd": path_a, "p2p": True, "agent_id": f"builder-a-{name}", "db": db_path,
        "prompt": f"Builder A: implement SLICE A only. {prompt}",
    }), depends_on=scout, worktree_path=path_a)
    builder_b = db.add_task(conn, "pi", json.dumps({
        "model": DEFAULT_MODELS["builder"], "thinking": DEFAULT_THINKING,
        "cwd": path_b, "p2p": True, "agent_id": f"builder-b-{name}", "db": db_path,
        "prompt": f"Builder B: implement SLICE B only. {prompt}",
    }), depends_on=scout, worktree_path=path_b)
    integrate = db.add_task(conn, "worktree_integrate", json.dumps({
        "into": path_a, "from_worktree": path_b, "branch": name_b, "db": db_path,
    }), depends_on=[builder_a, builder_b], worktree_path=path_a, max_retries=0)
    integrator = db.add_task(conn, "pi", json.dumps({
        "model": DEFAULT_MODELS["fixer"], "thinking": DEFAULT_THINKING,
        "cwd": path_a, "p2p": True, "agent_id": f"integrator-{name}", "db": db_path,
        "prompt": ("Integration step. The prior task's artifact says either "
                   "'clean' or 'conflicts: <files>'. If clean, reply 'nothing "
                   "to do' and stop. Otherwise resolve every conflict marker in "
                   "those files, keeping BOTH slices' behaviour, then "
                   "`git add -A && git commit`."),
    }), depends_on=integrate, worktree_path=path_a)
    fixer = db.add_task(conn, "fixer_loop", json.dumps({
        "test_command": test_command, "agent_type": "pi",
        "model": DEFAULT_MODELS["fixer"], "thinking": DEFAULT_THINKING,
        "cwd": path_a, "max_attempts": max_attempts, "db": db_path,
        "upstream": f"integrator-{name}", "agent_id": f"fixer-{name}",
    }), depends_on=integrator, worktree_path=path_a)
    verify_id = db.add_task(conn, "verify",
                            json.dumps({"test_command": test_command, "cwd": path_a}),
                            depends_on=fixer, worktree_path=path_a, max_retries=0)
    tasks = {"worktree_a": wt_a, "worktree_b": wt_b, "scout": scout,
             "builder_a": builder_a, "builder_b": builder_b,
             "integrate": integrate, "integrator": integrator,
             "fixer": fixer, "verify": verify_id}
    last = verify_id
    if merge:
        last = db.add_task(conn, "worktree_merge",
                           json.dumps({"worktree_path": path_a, "branch": name_a,
                                       "base": base, "db": db_path}),
                           depends_on=last, worktree_path=path_a, max_retries=0)
        tasks["merge"] = last
    # Both cleanups trail the last work node: a failed run keeps its worktrees
    # and branches for post-mortem.
    tasks["cleanup_a"] = db.add_task(
        conn, "worktree_cleanup",
        json.dumps({"worktree_path": path_a, "branch": name_a, "db": db_path}),
        depends_on=last, worktree_path=path_a)
    tasks["cleanup_b"] = db.add_task(
        conn, "worktree_cleanup",
        json.dumps({"worktree_path": path_b, "branch": name_b, "db": db_path}),
        depends_on=last, worktree_path=path_b)
    return {"name": name, "worktree_path": path_a, "tasks": tasks}


def build_pipeline(conn, db_path, name, prompt, *, base="main",
                   test_command=None, max_attempts=3, merge=False,
                   complexity="standard", cwd=None) -> dict:
    """Build a DAG sized to the request. Tiers:

      simple    one agent (kimi-k2.5) in cwd, verify iff test_command
      standard  worktree -> scout -> builder -> fixer -> verify [-> merge] -> cleanup
      complex   parallel builders in separate worktrees joined by an integrator

    An unknown tier degrades to `standard` — a typo in a planning hint must
    not fail a submit.
    """
    if complexity == "simple":
        return _build_simple(conn, db_path, name, prompt,
                             test_command=test_command, cwd=cwd)
    if complexity == "complex":
        return _build_complex(conn, db_path, name, prompt, base=base,
                              test_command=test_command,
                              max_attempts=max_attempts, merge=merge)
    return _build_standard(conn, db_path, name, prompt, base=base,
                           test_command=test_command,
                           max_attempts=max_attempts, merge=merge)


def _toposort(nodes: list[dict]) -> list[str]:
    """Dependency-first order of node ids; raises ValueError on a cycle."""
    graph = {n["id"]: list(n.get("depends_on") or []) for n in nodes}
    state: dict[str, int] = {}  # 0 = visiting, 1 = done
    order: list[str] = []

    def visit(v: str) -> None:
        if state.get(v) == 1:
            return
        if state.get(v) == 0:
            raise ValueError(f"cycle detected at node {v!r}")
        state[v] = 0
        for dep in graph[v]:
            visit(dep)
        state[v] = 1
        order.append(v)

    for v in graph:
        visit(v)
    return order


def _ancestors(nodes: list[dict]) -> dict[str, set]:
    """{node id: every node that must finish before it}, transitively."""
    direct = {n["id"]: set(n.get("depends_on") or []) for n in nodes}
    out: dict[str, set] = {}

    def walk(nid: str) -> set:
        if nid in out:
            return out[nid]
        out[nid] = set()          # cycles are rejected elsewhere; this is a guard
        seen = set(direct[nid])
        for parent in direct[nid]:
            seen |= walk(parent)
        out[nid] = seen
        return seen

    for n in nodes:
        walk(n["id"])
    return out


def _check_write_conflicts(nodes: list[dict]) -> None:
    """Reject a DAG where two nodes that can run at once write the same file.

    Two builders once appended to one calc.py in one worktree simultaneously
    and both edits survived by luck; nothing in the queue would have noticed a
    lost one. Ordering is the fix the graph already offers, so this only has to
    prove the order exists. A node that declares no `writes` is not policed:
    the claim is a declaration, not a discovery, and old plans must keep
    working.
    """
    claims = [(n["id"], set(n.get("writes") or [])) for n in nodes]
    claims = [(nid, files) for nid, files in claims if files]
    if len(claims) < 2:
        return
    anc = _ancestors(nodes)
    for i, (a, fa) in enumerate(claims):
        for b, fb in claims[i + 1:]:
            shared = fa & fb
            if shared and a not in anc[b] and b not in anc[a]:
                raise ValueError(
                    f"nodes {a!r} and {b!r} both write {sorted(shared)} and "
                    "neither runs before the other; add a depends_on edge")


# Appended to every pi node prompt. Each line answers something observed: an
# agent that reported a green suite while its own test failed, one that silently
# dropped a config value the prompt told it to choose, and one that summarised
# command output instead of pasting it.
DELIVERABLES = (
    "\n\n--- Required deliverables ---\n"
    "1. Paste the verbatim output of every command that proves the acceptance "
    "criteria. No summaries, no paraphrase.\n"
    "2. State every value this prompt asked you to choose, and why you chose it.\n"
    "3. Never report a command as passing without its pasted output. If you did "
    "not run it, say so.\n"
    # Everything above is for the operator reading the pane. This last line is
    # for the next node: it is the only part of your output that is put in
    # front of whoever depends on you, so anything they need has to be here.
    f"4. End your final message with this block, and nothing after it:\n"
    f"{handlers.HANDOFF_MARK}\n"
    "files: <paths you changed, comma separated, or none>\n"
    "added: <symbols or behaviour you added, or none>\n"
    "decisions: <choices a later node must not contradict>\n"
    "open: <what you could not finish, or none>"
)


def _assert_git_repo(path: str) -> None:
    """Fail the submit, not the first node, when there is no repo to branch from."""
    proc = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=path,
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise ValueError(
            f"cwd {path!r} is not inside a git repository, so a named DAG cannot "
            "create its worktree. Pass cwd= the repo you mean, or omit name= to "
            "run the nodes in place.")


# The shape check for `requires`, which the worker enforces literally after a
# node exits. Rejecting a malformed spec at submit matters more here than
# elsewhere: a requirement that silently does nothing is worse than none, since
# the plan is written believing it is checked.
_REQUIRES_LIST_KEYS = ("files",)
_REQUIRES_MAP_KEYS = ("symbols", "absent", "min_lines")


def _check_requires_spec(nid: str, spec) -> None:
    if not isinstance(spec, dict):
        raise ValueError(f"node {nid!r}: requires must be an object")
    unknown = set(spec) - set(_REQUIRES_LIST_KEYS) - set(_REQUIRES_MAP_KEYS)
    if unknown:
        raise ValueError(
            f"node {nid!r}: unknown requires keys {sorted(unknown)}; "
            f"allowed: {sorted(_REQUIRES_LIST_KEYS + _REQUIRES_MAP_KEYS)}")
    for key in _REQUIRES_LIST_KEYS:
        if key in spec and not isinstance(spec[key], list):
            raise ValueError(f"node {nid!r}: requires.{key} must be a list of paths")
    for key in _REQUIRES_MAP_KEYS:
        if key in spec and not isinstance(spec[key], dict):
            raise ValueError(f"node {nid!r}: requires.{key} must be an object keyed by path")
    for path, needles in (spec.get("symbols") or {}).items():
        if not isinstance(needles, list) or not all(isinstance(s, str) for s in needles):
            raise ValueError(f"node {nid!r}: requires.symbols[{path!r}] must be a list of strings")
    for path, needles in (spec.get("absent") or {}).items():
        if not isinstance(needles, list) or not all(isinstance(s, str) for s in needles):
            raise ValueError(f"node {nid!r}: requires.absent[{path!r}] must be a list of strings")
    for path, minimum in (spec.get("min_lines") or {}).items():
        if not isinstance(minimum, int):
            raise ValueError(f"node {nid!r}: requires.min_lines[{path!r}] must be an integer")


def build_dag(conn, db_path, nodes, *, name=None, base=None, cwd=None) -> dict:
    """Submit an arbitrary agent DAG. Each node is a dict:

        {id, prompt, depends_on?, harness?("pi"|"verify"), model?, thinking?,
         skills?, p2p?, writes?}

    `writes` is the files a node claims. Two nodes claiming the same file with
    no dependency edge between them are rejected: they would run at once, in
    one worktree, and a lost update would be silent. `model`/`thinking` are
    checked against handlers.ALLOWED_MODELS here rather than in the pane.

    A node with harness "verify" is a test gate instead of an agent: it takes
    `test_command` in place of `prompt` and fails the DAG when the tests do.

    If `name` is given the DAG is wrapped in a git worktree (worktree_create ->
    nodes -> worktree_cleanup) and every node runs in it; otherwise nodes run in
    `cwd` (default: the current working directory). Returns {"nodes": {id: task_id}}.
    """
    if not nodes:
        raise ValueError("nodes must be a non-empty list")
    ids = [n["id"] for n in nodes]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate node id in DAG")
    idset = set(ids)
    for n in nodes:
        for dep in n.get("depends_on") or []:
            if dep not in idset:
                raise ValueError(f"node {n['id']!r} depends on unknown node {dep!r}")
    order = _toposort(nodes)  # also raises on cycles
    _check_write_conflicts(nodes)
    node_by_id = {n["id"]: n for n in nodes}

    # Absolute so tmux `-c <work_path>` opens panes in the target repo regardless
    # of where the workers were spawned from; a literal "." is CWD-dependent.
    repo = os.path.abspath(cwd or os.getcwd())
    work_path = repo
    wt_task = None
    if name:
        # A named DAG means a git worktree, and a worktree needs a repository to
        # come from. Checked here because the alternative is a worktree_create
        # node that fails deep in the queue and blocks every dependent forever,
        # with `silicorism_gc` unable to clear the pending rows behind it.
        _assert_git_repo(repo)
        work_path = os.path.join(WORKTREE_ROOT, name)
        # base stays absent unless asked for: an explicit "main" here is a guess
        # about someone else's repo, and it silently beats the handler's default
        # of that repo's own current branch.
        wt_payload = {"branch": name, "db": db_path, "repo": repo}
        if base:
            wt_payload["base"] = base
        wt_task = db.add_task(conn, "worktree_create", json.dumps(wt_payload),
                              worktree_path=work_path)

    id_map: dict[str, int] = {}
    for nid in order:
        n = node_by_id[nid]
        harness = n.get("harness") or "pi"
        if harness not in ("pi", "claude", "verify"):
            raise ValueError(
                f"node {nid!r}: harness must be 'pi' or 'verify'")
        # Execution is pi-only. "claude" is accepted for back-compat and coerced:
        # the OSS friendly names resolve on the pi branch alone (handlers.py:192),
        # so a claude node with model "glm-5" dies at CLI startup instead of running.
        if harness == "claude":
            harness = "pi"
        deps = [id_map[d] for d in (n.get("depends_on") or [])]
        if wt_task and not deps:
            deps = [wt_task]  # root nodes wait for the worktree to exist
        if harness == "verify":
            # The gate a plan-derived DAG would otherwise lack: an agent can
            # claim its task is done, this node runs the tests and cannot.
            if not n.get("test_command"):
                raise ValueError(f"node {nid!r}: verify needs a test_command")
            payload = {"test_command": n["test_command"], "cwd": work_path}
            if n.get("expect_fail"):
                payload["expect_fail"] = True
        else:
            if not n.get("prompt"):
                raise ValueError(f"node {nid!r}: {harness} node needs a prompt")
            # Off-roster models used to reach a pane and die there, which reads
            # as an orchestrator bug rather than a bad plan. Reject on submit.
            model, thinking = handlers.check_model(n.get("model"),
                                                   n.get("thinking"))
            payload = {"prompt": n["prompt"] + DELIVERABLES, "cwd": work_path,
                       "p2p": n.get("p2p", True), "agent_id": nid, "db": db_path,
                       "model": model, "thinking": thinking}
            # test_command turns this node into its own gate: the worker runs
            # it after the agent exits (worker._gate_command), so the node
            # cannot report a pass its tests do not support.
            # model/thinking are already set above, resolved and checked; a
            # raw copy here would put the unresolved alias back.
            if n.get("requires"):
                _check_requires_spec(nid, n["requires"])
            for key in ("skills", "test_command", "writes", "requires",
                        "timeout_s", "stall_timeout_s"):
                if n.get(key):
                    payload[key] = n[key]
        id_map[nid] = db.add_task(conn, harness, json.dumps(payload),
                                  depends_on=deps or None, worktree_path=work_path)

    result = {"nodes": id_map}
    if name:
        depended = {d for n in nodes for d in (n.get("depends_on") or [])}
        leaves = [id_map[nid] for nid in ids if nid not in depended]
        result["cleanup"] = db.add_task(
            conn, "worktree_cleanup",
            json.dumps({"worktree_path": work_path, "branch": name, "db": db_path,
                        "repo": repo}),
            depends_on=leaves, worktree_path=work_path)
        result["worktree"] = wt_task
        result["worktree_path"] = work_path
    return result


def _dead_task_ids(rows) -> set:
    """Pending tasks that can never run: a dependency failed, or is itself dead.

    Without this the queue never settles after a failure — a failed node's
    dependents stay `pending` for ever, so `active` never reaches 0 and the
    orchestrator's verify loop has no exit condition.
    """
    status = {r["id"]: r["status"] for r in rows}
    deps = {}
    for r in rows:
        try:
            d = json.loads(r["depends_on"] or "[]")
        except (json.JSONDecodeError, ValueError, TypeError):
            d = []
        deps[r["id"]] = d if isinstance(d, list) else []
    doomed = {i for i, s in status.items() if s == "failed"}
    changed = True
    while changed:  # transitive: a dependent of a dead task is dead too
        changed = False
        for tid, ds in deps.items():
            if (tid not in doomed and status.get(tid) == "pending"
                    and any(d in doomed for d in ds)):
                doomed.add(tid)
                changed = True
    return {i for i in doomed if status.get(i) == "pending"}


def verify_status(conn) -> dict:
    """Verdict for the orchestrator loop: is the goal met, and if not, why.

    satisfied = nothing pending/processing, no failed tasks, at least one done.
    failures carry each failed task's artifact + last error log so the caller
    can generate a corrective DAG. `active` excludes tasks stranded behind a
    failure — they will never run, so waiting on them is waiting for ever.
    """
    counts = db.counts(conn)
    blocked = _dead_task_ids(db.all_tasks(conn))
    # counts and all_tasks are separate reads; a row inserted between them
    # must not drive this negative and hide "nothing left to do".
    active = max(counts["pending"] + counts["processing"] - len(blocked), 0)
    failures = []
    for r in conn.execute(
            "SELECT id, task_type, output_artifact FROM tasks "
            "WHERE status='failed' ORDER BY id").fetchall():
        err = conn.execute(
            "SELECT message FROM execution_logs WHERE task_id=? AND level='error' "
            "ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
        failures.append({"id": r["id"], "task_type": r["task_type"],
                         "artifact": r["output_artifact"],
                         "error": err["message"] if err else None})
    return {
        "satisfied": active == 0 and counts["failed"] == 0 and counts["completed"] > 0,
        "tasks": counts,
        "active": active,
        "blocked": len(blocked),
        "failures": failures,
        "quarantined": [w["path"] for w in db.worktrees(conn, "quarantined")],
    }


# Capped below the 30-minute idle window an MCP stdio client allows a tool
# call that sends no progress notifications.
WAIT_CAP_S = 1800.0


def wait_for_settle(conn, *, timeout_s=600.0, poll=1.0, stop=None) -> dict:
    """Block until the queue settles, then return the verdict once.

    Settled = nothing runnable is left, OR a task failed *during this wait*
    (waiting out a doomed run costs the orchestrator a turn for nothing).
    Failures already on the books at entry do not count: they never clear, so
    settling on them would turn every later wait into an instant return.

    This replaces the poll loop: one Claude turn per DAG, not one per poll.
    """
    started = time.monotonic()
    deadline = started + min(max(float(timeout_s), 1.0), WAIT_CAP_S)
    already_failed = {f["id"] for f in verify_status(conn)["failures"]}
    while True:
        # A worker killed hard never requeues its task, and everything behind
        # it waits for ever; the wait is where that gets noticed.
        db.reap_stale(conn)
        verdict = verify_status(conn)
        fresh = [f for f in verdict["failures"] if f["id"] not in already_failed]
        if verdict["active"] == 0 or fresh:
            verdict.update(settled=True, timed_out=False,
                           elapsed_s=round(time.monotonic() - started, 1))
            return verdict
        if (stop and stop()) or time.monotonic() >= deadline:
            # A timeout was shape-identical to a real verdict, so an
            # orchestrator could read "no failures" out of "nothing finished".
            verdict.update(settled=False,
                           timed_out=time.monotonic() >= deadline,
                           elapsed_s=round(time.monotonic() - started, 1))
            return verdict
        time.sleep(poll)


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


def prune_tasks(conn) -> dict:
    """Delete completed/failed tasks and their logs. Pending and processing rows
    are never touched, so a live pipeline cannot be pruned out from under itself.
    A completed/failed task is deleted only when NO surviving task with status
    'pending' or 'processing' lists that task's id in its depends_on.
    """
    with db.immediate(conn) as c:
        # Find completed/failed tasks that are NOT dependencies of any pending/processing task
        ids = [r["id"] for r in c.execute("""
            SELECT id FROM tasks
            WHERE status IN ('completed','failed')
              AND id NOT IN (
                  SELECT DISTINCT d.value
                  FROM tasks t
                  JOIN json_each(COALESCE(t.depends_on,'[]')) d
                  WHERE t.status IN ('pending','processing')
              )
        """)]
        if ids:
            marks = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM execution_logs WHERE task_id IN ({marks})", ids)
            # agent_heartbeats.current_task_id is a real FK: a worker still
            # pointing at the task it last ran would make the delete fail with
            # "FOREIGN KEY constraint failed" and prune nothing at all.
            c.execute("UPDATE agent_heartbeats SET current_task_id=NULL "
                      f"WHERE current_task_id IN ({marks})", ids)
            c.execute(f"DELETE FROM tasks WHERE id IN ({marks})", ids)
    return {"deleted": len(ids)}


def _stalled(conn, *, idle_s: float = 300.0) -> list[dict]:
    """Processing tasks whose files have not changed for `idle_s` seconds.

    `busy` with a fresh heartbeat looked exactly like healthy progress for an
    hour of wall clock; this is the row that makes the difference readable
    without attaching to a pane.
    """
    out = []
    for r in conn.execute(
            "SELECT id, agent_id, last_progress_at, started_at FROM tasks "
            "WHERE status='processing'").fetchall():
        stamp = r["last_progress_at"] or r["started_at"]
        if not stamp:
            continue
        try:
            seen = datetime.strptime(
                stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        idle = (datetime.now(timezone.utc) - seen).total_seconds()
        if idle >= idle_s:
            out.append({"id": r["id"], "agent_id": r["agent_id"],
                        "last_progress_at": stamp, "idle_s": round(idle)})
    return out


def cancel_task(conn, task_id, *, _kill=None) -> dict:
    """Fail a named task and close its pane. `_kill` is injected by tests."""
    pane = db.cancel_task(conn, task_id)
    if pane is None:
        return {"cancelled": False, "reason": f"no task {task_id}"}
    db.log(conn, task_id, "operator", "cancelled by operator", level="error")
    killed = False
    if pane:
        # pane_target is "<window>.<%pane>" on the grid path, a bare window
        # name on the fallback path.
        target = pane.rsplit(".", 1)[-1] if "%" in pane else pane
        kill = _kill or (tmux.kill_pane if "%" in pane else tmux.kill_window)
        try:
            kill(target)
            killed = True
        except Exception:  # noqa: BLE001 - the pane may already be gone
            pass
    return {"cancelled": True, "task_id": task_id, "pane_killed": killed}


def get_status(conn) -> dict:
    """Live DAG + P2P snapshot for the orchestrator context.

    Includes the verify verdict (satisfied? + failed-task artifacts/errors) so a
    single status call tells the orchestrator whether to re-loop.
    """
    verdict = verify_status(conn)
    return {
        "tasks": verdict["tasks"],
        "satisfied": verdict["satisfied"],
        "failures": verdict["failures"],
        # A busy agent that has written nothing for minutes: the one reading
        # that separates a working run from a wedged one.
        "stalled": _stalled(conn),
        # Tokens burnt, USD spent, and the planner-rate spend the run avoided.
        "usage": db.usage_totals(conn),
        "agents": [dict(h) for h in db.heartbeats(conn)],
        # Trimmed hard: every row here is orchestrator context the user pays
        # for, and a successful task's artifact tells the planner nothing.
        "messages": [dict(m) for m in db.recent_messages(conn, 5)],
        "logs": [dict(r) for r in db.recent_logs(conn, 5)],
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
        os.environ["SILICORISM_NATIVE"] = "1"  # child inherits; enables pane exec
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
        assert list(p["tasks"]) == ["worktree", "scout", "builder", "fixer",
                                    "verify", "cleanup"]
        assert p["tasks"]["cleanup"] == 6
        # default models are the bedrock-mantle OSS trio with high thinking
        sp = json.loads(conn.execute("SELECT payload FROM tasks WHERE id=?",
                                     (p["tasks"]["builder"],)).fetchone()["payload"])
        assert sp["model"] == "bedrock-mantle/moonshotai.kimi-k2.5"
        assert sp["thinking"] == "high"
        # merge=True wires fixer->verify->merge->cleanup
        pm = build_pipeline(conn, dbp, "demo2", "add auth", merge=True)
        assert list(pm["tasks"]) == ["worktree", "scout", "builder", "fixer",
                                     "verify", "merge", "cleanup"]
        st = get_status(conn)
        assert st["tasks"]["pending"] == 13 and st["satisfied"] is False
        assert st["messages"] == [] and st["worktrees"] == []
        db.send_inter_agent_message(conn, "a", "b", "hi")
        assert get_status(conn)["messages"][0]["content"] == "hi"
        assert gc_worktrees(conn, dbp) == {"cleaned": [], "kept": []}
        conn.close()

        # dynamic DAG: deps wire, cycle rejected
        dbp2 = f"{d}/dag.db"
        db.init_db(dbp2)
        conn = db.connect(dbp2)
        dag = build_dag(conn, dbp2, [
            {"id": "a", "prompt": "x"},
            {"id": "b", "prompt": "y", "depends_on": ["a"], "harness": "claude"}])
        assert set(dag["nodes"]) == {"a", "b"}
        assert [r["task_type"] for r in
                conn.execute("SELECT task_type FROM tasks ORDER BY id")] == ["pi", "pi"]
        assert verify_status(conn)["satisfied"] is False
        try:
            build_dag(conn, dbp2, [{"id": "a", "prompt": "x", "depends_on": ["a"]}])
        except ValueError:
            pass
        else:
            raise AssertionError("self-cycle must raise")
        conn.close()
    print("silicorism_tools OK")
