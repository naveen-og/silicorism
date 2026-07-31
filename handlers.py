"""Task-type handlers. A worker looks up task_type here and runs it.

Payload is an opaque string per task. Handlers raise on failure; the worker
turns that into a retry/fail transition.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time

import db
import skills

WORKTREE_ROOT = "/tmp/worktrees"

# Task types that run as native CLI agents in a live tmux pane (SILICORISM_NATIVE).
NATIVE_AGENTS = ("pi", "claude")

# Injected into every agent node that does not state its own `skills`. The
# execution models are smaller than the planner, and this is the cheapest place
# to put the floor under them: read before editing, root cause over symptom,
# smallest correct diff, no "done" without pasted output.
DEFAULT_SKILLS = ("coding-excellence",)

DEFAULT_PI_MODEL = "bedrock-mantle/moonshotai.kimi-k2.5"

# autoexit.ts: lets a worker pane run the full pi TUI and still yield a
# deterministic exit code + artifact file (see extensions/autoexit.ts).
AUTOEXIT_EXT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "extensions", "autoexit.ts")

# Friendly canonical name -> full model id, so DAG nodes can say "glm-5".
# Full ids pass through untouched.
MODEL_ALIASES = {
    "qwen3-coder-480b": "bedrock-mantle/qwen.qwen3-coder-480b-a35b-instruct",
    "kimi-k2.5": "bedrock-mantle/moonshotai.kimi-k2.5",
    "glm-5": "bedrock-mantle/zai.glm-5",
    # The one opencode free model kept, and only at max thinking. The rest of
    # that tier (nemotron, laguna, ling, mimo, north-mini) was removed on
    # 2026-07-30: the live six-node run showed their output was not worth the
    # orchestration around it, and hy3 had already been dropped upstream.
    "deepseek-v4-flash": "opencode/deepseek-v4-flash-free",
}

# What a DAG node is allowed to run on. Everything else — including a full id
# typed by hand, which resolve_model passes through untouched — is rejected at
# submission rather than discovered when the pane dies.
ALLOWED_MODELS = {
    "bedrock-mantle/moonshotai.kimi-k2.5": ("medium", "high"),
    "bedrock-mantle/zai.glm-5": ("medium", "high"),
    # Only earns its place at max; below that it is not worth a node.
    "opencode/deepseek-v4-flash-free": ("max",),
}
DEFAULT_THINKING_FOR = {m: levels[-1] for m, levels in ALLOWED_MODELS.items()}


def check_model(model: str | None, thinking: str | None) -> tuple[str, str]:
    """Resolve a node's (model, thinking), or raise if it is off the roster.

    The roster is the point: a plan that names a model nobody vetted used to
    reach a pane and die there, and the operator read that as an orchestrator
    bug. Rejecting at submission puts the error where the mistake was made.
    """
    resolved = resolve_model(model) or DEFAULT_PI_MODEL
    levels = ALLOWED_MODELS.get(resolved)
    if levels is None:
        raise ValueError(
            f"model {model!r} is not on the roster; allowed: "
            + ", ".join(sorted(ALLOWED_MODELS)))
    level = thinking or DEFAULT_THINKING_FOR[resolved]
    if level not in levels:
        raise ValueError(
            f"model {resolved} runs at {'/'.join(levels)}, not {level!r}")
    return resolved, level

# Retry escalation: each failed attempt bumps a pi task to the next stronger
# model. OSS-only by design — a retry must never silently bill Claude tokens.
# qwen3-coder-480b is deliberately absent: it stays reachable by name through
# MODEL_ALIASES, but nothing routes onto it by default.
ESCALATION = [
    "bedrock-mantle/moonshotai.kimi-k2.5",
    "bedrock-mantle/zai.glm-5",
]

# USD per million tokens: (input, output, cache read, 5m cache write).
# Anthropic list prices read 2026-07-30 from
# platform.claude.com/docs/en/about-claude/pricing; Sonnet 5 is on the
# introductory rate that ends 2026-08-31. The OSS gateways are absent on
# purpose: bedrock-mantle reports usage.cost.total = 0 (it publishes no price
# metadata) and the opencode models are the free tier, so their real spend is
# nil. The number worth showing is not what the nodes cost, it is the Claude
# spend they avoided.
PRICES = {
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.50),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 0.10, 1.25),
}

# What the planner itself runs on, and therefore what a single-session run of
# the same work would have been billed at.
BASELINE_MODEL = "claude-opus-5"


def usage_cost(model: str, usage: dict) -> float:
    """USD for one agent's token usage, or 0.0 on a model with no published price.

    Zero means unpriced, not free-and-certain: guessing a rate for a gateway
    that publishes none would put a fabricated number on the dashboard.
    """
    price = PRICES.get(model)
    if not price:
        return 0.0
    pin, pout, pread, pwrite = price
    return (
        (usage.get("input") or 0) * pin
        + (usage.get("output") or 0) * pout
        + (usage.get("cacheRead") or 0) * pread
        + (usage.get("cacheWrite") or 0) * pwrite
    ) / 1e6


def baseline_cost(usage: dict) -> float:
    """What these tokens would have cost in one planner-model session."""
    return usage_cost(BASELINE_MODEL, usage)


def escalate_payload(task_type: str, payload: str) -> str | None:
    """Next-rung payload for a retried pi task, or None if nothing to change.

    Unknown/strongest model -> first rung not already used. Non-pi tasks and
    unparseable payloads are left alone.
    """
    if task_type != "pi":
        return None
    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            return None
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    current = resolve_model(data.get("model")) or DEFAULT_PI_MODEL
    try:
        nxt = ESCALATION[ESCALATION.index(current) + 1]
    except ValueError:  # not on the ladder: start it
        nxt = ESCALATION[0] if current != ESCALATION[0] else ESCALATION[1]
    except IndexError:  # already at the top rung
        return None
    # Keep what was asked for. Overwriting `model` in place made escalation
    # unfalsifiable from outside: a glm-5 pane for a node the plan sent to
    # kimi-k2.5 was indistinguishable from misrouting, which is what left F7
    # open for a week. setdefault, so a second rung still names the original.
    data.setdefault("model_requested", data.get("model") or "default")
    data["model"] = nxt
    return json.dumps(data)


def resolve_model(model: str | None) -> str | None:
    """Map a friendly canonical name to its opencode id; pass full ids through."""
    return MODEL_ALIASES.get(model, model) if model else model


# Splice (github: the local checkout) gives a node `read_scope_map` and
# `splice_edit`: AST-anchored patches behind an LSP delta gate, so an edit that
# introduces a new diagnostic is rejected instead of committed. That gate is
# the cheapest quality floor available for a weak model, whose worst failure is
# a confident whole-file rewrite.
SPLICE_ENV = "SILICORISM_SPLICE"
SPLICE_PROBES = ("~/.pi/agent/extensions/splice", "~/Projects/splice")


def splice_root() -> str | None:
    """Directory of the splice checkout, or None if it is not installed.

    Presence is checked by the extension file, not the directory: a stale env
    var pointing at an empty path must read as "no splice" rather than become
    a broken `-e` argument that kills every pane at startup.
    """
    # An operator who sets the variable meant it: a value that does not hold an
    # extension turns splice off rather than falling through to a probe, which
    # is the only way to run a node without it on a machine that has it.
    override = os.environ.get(SPLICE_ENV)
    candidates = (override,) if override else SPLICE_PROBES
    for cand in candidates:
        root = os.path.expanduser(cand)
        if os.path.isfile(os.path.join(root, "extensions", "splice.ts")):
            return root
    return None


# Checked in order; the first one present wins.
CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md")


def project_context(cwd: str | None) -> str | None:
    """Path to the repo's own context file in `cwd`, or None.

    Returns a path, never text: pi's --append-system-prompt takes either, so a
    name that is not a file would be appended as the literal string.
    """
    if not cwd:
        return None
    for name in CONTEXT_FILES:
        path = os.path.join(cwd, name)
        if os.path.isfile(path):
            return path
    return None

P2P_NOTE = (
    "\n\n--- Coordination ---\n"
    "You are one agent in a pool sharing a P2P channel. If architectural intent "
    "or expected behavior is unclear, a peer may message you for clarification; "
    "answer concisely and factually."
)


def _shell(payload: str, context=None) -> str:
    """Run payload as a shell command. Non-zero exit raises -> task fails."""
    proc = subprocess.run(
        payload, shell=True, capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        raise RuntimeError(f"exit {proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()[:500]


def _sleep(payload: str, context=None) -> str:
    """Sleep N seconds (payload = number). Used to simulate real work in tests."""
    secs = float(payload or "0")
    time.sleep(secs)
    return f"slept {secs}s"


def _echo(payload: str, context=None) -> str:
    return payload or ""


def _fail(payload: str, context=None) -> str:
    """Always raises. Exercises the retry/fail path."""
    raise RuntimeError(payload or "forced failure")


def _parse(payload: str, required=("prompt",)) -> dict:
    """Payload is JSON; a bare string falls back to {'prompt': <string>}."""
    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            data = {"prompt": str(data)}
    except (json.JSONDecodeError, ValueError):
        data = {"prompt": payload}
    for key in required:
        if not data.get(key):
            raise ValueError(f"missing required field {key!r}")
    return data


def _spawn(cmd: list[str], cwd, label: str) -> str:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, cwd=cwd, timeout=600
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{label} exit {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    return proc.stdout.strip()[:500]


HANDOFF_MARK = "--- HANDOFF ---"
TRUNCATED_NOTE = "\n[... earlier output trimmed ...]"
HANDOFF_CAP = 1500


def handoff(artifact: str, *, cap: int = HANDOFF_CAP) -> str:
    """The part of an artifact worth putting in front of a dependent node.

    Edges used to carry the parent's whole transcript: one live run spent
    10,072 input tokens on a node whose job was to append a single function,
    nearly all of it its parent's prose. The block is asked for in
    DELIVERABLES; the bounded tail is the fallback, because weak models forget
    formats and a truncated conclusion still beats eight thousand tokens of
    working notes. The tail, not the head — that is where a run's verdict is.
    """
    if not artifact:
        return ""
    head, mark, block = artifact.rpartition(HANDOFF_MARK)
    if mark:
        return block.strip()
    if len(artifact) <= cap:
        return artifact
    return TRUNCATED_NOTE + artifact[-cap:]


def _with_context(prompt: str, context) -> str:
    """Append completed-dependency hand-offs to a prompt (artifact hand-off)."""
    if not context:
        return prompt
    parts = [h for h in (handoff(str(v)) for v in context.values() if v) if h]
    if not parts:
        return prompt
    return prompt + "\n\n--- Context from prior tasks ---\n" + "\n".join(parts)


def _prompt(data: dict, context, *, native=False) -> str:
    """Build the agent prompt: base + dep artifacts + requested skills + P2P."""
    p = _with_context(data["prompt"], context)
    # Discipline is the floor, not an opt-in. A node only got coding-excellence
    # when the plan remembered to name it, so the built-in tiers — the path
    # taken whenever nobody hand-wrote a DAG — ran with none at all. Pass
    # "skills": [] to mean none; omitting the key means the default.
    requested = data["skills"] if "skills" in data else list(DEFAULT_SKILLS)
    injected = skills.load_skills(requested, cwd=data.get("cwd"))
    if injected:
        p += "\n\n" + injected
    # The lease, stated to the node that holds it. build_dag has already proved
    # no unordered sibling claims these, so "yours alone" is a fact here.
    if data.get("writes"):
        p += ("\n\n--- Files you own ---\n"
              + ", ".join(data["writes"])
              + "\nNo other node running now will touch them. Do not edit files "
                "outside this list without saying so in your handoff.")
    # Pushed, not polled: mail waiting at launch is put in front of the agent,
    # because nothing ever stopped mid-run to go and look for it.
    if data.get("inbox"):
        p += "\n\n--- Messages waiting for you ---\n" + "\n".join(data["inbox"])
    if data.get("p2p"):
        p += P2P_NOTE
        if native:
            p += ("\n\nYou can coordinate over the shell (bash tool):\n"
                  "  silicorism-msg send <agent_id> \"<text>\"   # message a peer\n"
                  "  silicorism-msg poll                         # read your inbox")
    return p


def native_command(task_type: str, payload: str, context=None,
                   *, cli_path="cli.py") -> str | None:
    """Full shell command to run an agent live in a pane, or None if in-process.

    Only pi/claude tasks run natively; the prompt carries dep artifacts + a
    `silicorism-msg` alias (backed by `python cli.py msg`) so the agent can use the
    P2P channel from its own bash tool. cwd is set by tmux, not here.
    """
    if task_type not in NATIVE_AGENTS:
        return None
    data = _parse(payload)
    prompt = _prompt(data, context, native=True)
    prelude = ""
    agent_id, dbp = data.get("agent_id"), data.get("db")
    if agent_id and dbp:
        # silicorism-msg reads SILICORISM_DB/SILICORISM_SELF, so the agent calls it argument-free.
        prelude = (f"export SILICORISM_DB={shlex.quote(dbp)}; "
                   f"export SILICORISM_SELF={shlex.quote(agent_id)}; "
                   f"silicorism-msg(){{ python {shlex.quote(cli_path)} msg \"$@\"; }}; ")
    if task_type == "pi":
        # Full interactive TUI in the pane; autoexit.ts ends the process when
        # the agent settles and writes the clean artifact to $SILICORISM_ARTIFACT.
        if data.get("artifact"):
            prelude += f"export SILICORISM_ARTIFACT={shlex.quote(data['artifact'])}; "
        # Only pi can report its own tokens. The extension writes them here on
        # settle; no path means no telemetry rather than a broken run.
        if data.get("usage"):
            prelude += f"export SILICORISM_USAGE={shlex.quote(data['usage'])}; "
        # An execution node runs on exactly what the plan gave it. Left to
        # discover, pi loads the operator's global CLAUDE.md, their skills,
        # their prompt templates and every installed extension: measured on one
        # five-word prompt at 13,976 input tokens against 1,815 isolated, paid
        # on every turn of every node, and different on every machine. Those
        # rules are also written for a conversational assistant, which fights
        # the deliverables block telling the node to paste output verbatim.
        # -ne matters twice over: extensions/silicorism.ts registers
        # silicorism_plan_and_submit, so a discovered copy let an execution node
        # queue its own DAGs. An explicit -e path still loads.
        parts = ["pi", "-e", AUTOEXIT_EXT, "--no-session",
                 "-nc", "-ne", "-ns", "-np",
                 "--model", resolve_model(data.get("model")) or DEFAULT_PI_MODEL]
        # The repo's own conventions are part of the task, so they go back in by
        # path rather than by discovery.
        project = project_context(data.get("cwd"))
        if project:
            parts += ["--append-system-prompt", project]
        # Registering tools is not enough: a weak model ignores a tool it was
        # never told to prefer, so splice's own operating manual goes in with
        # it. Preferred, not enforced — pi's write/edit stay available, so a
        # node blocked by a splice rejection can still finish.
        splice = splice_root()
        if splice:
            parts += ["-e", os.path.join(splice, "extensions", "splice.ts")]
            overlay = os.path.join(splice, "system-prompt-overlay.md")
            if os.path.isfile(overlay):
                parts += ["--append-system-prompt", overlay]
        if data.get("thinking"):
            parts += ["--thinking", data["thinking"]]
        parts.append(prompt)
    else:  # claude
        # Same contract as the pi branch, in this CLI's flags: the node runs on
        # the plan, not on the operator's install. --setting-sources project
        # keeps the repo's own .claude/settings.json and drops the user's
        # global settings, skills and hooks; --strict-mcp-config with no
        # --mcp-config leaves the node zero MCP servers, which is what stops it
        # reaching silicorism_plan_and_submit and queueing its own DAG (the
        # -ne case); --no-session-persistence keeps one-shot nodes out of the
        # resume history. File access is already bounded — tmux opens the pane
        # in the worktree and --add-dir is the only way to widen that, so it is
        # deliberately absent. Residual gap: no flag suppresses the user-level
        # CLAUDE.md, so this is narrower than pi's isolation, not equal to it.
        parts = ["claude", "-p", "--setting-sources", "project",
                 "--strict-mcp-config", "--no-session-persistence"]
        if data.get("model"):
            parts += ["--model", data["model"]]
        parts.append(prompt)
    return prelude + " ".join(shlex.quote(x) for x in parts)


def run_pi_agent(payload: str, context=None) -> str:
    """pi --model <model> [--thinking <thinking>] "<prompt>" (cwd optional)."""
    data = _parse(payload)
    cmd = ["pi", "-p", "--model", resolve_model(data.get("model")) or DEFAULT_PI_MODEL]
    if data.get("thinking"):
        cmd += ["--thinking", data["thinking"]]
    cmd.append(_prompt(data, context))
    return _spawn(cmd, data.get("cwd"), "pi")


def run_claude_agent(payload: str, context=None) -> str:
    """claude -p [--model <model>] "<prompt>" (cwd optional)."""
    data = _parse(payload)
    cmd = ["claude", "-p"]
    if data.get("model"):
        cmd += ["--model", data["model"]]
    cmd.append(_prompt(data, context))
    return _spawn(cmd, data.get("cwd"), "claude")


# --- workflow handlers ------------------------------------------------------

def _git(args: list[str], cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=cwd, timeout=120)


def _wt_state(db_path, path, state, *, branch=None) -> None:
    """Best-effort worktree state transition; a missing db just skips tracking."""
    if not db_path:
        return
    try:
        conn = db.connect(db_path)
        try:
            db.set_worktree(conn, path, state, branch=branch)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - GC state is advisory, never fail the task
        pass


def _default_base(repo: str | None) -> str:
    """The repo's own current branch, or "main" when git will not say.

    Defaulting to "main" is a guess about someone else's repository. Every DAG
    submitted against a repo on `master` used to fail at node one with
    `invalid reference: main`, which reads as an orchestrator bug.
    """
    proc = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    branch = proc.stdout.strip() if proc.returncode == 0 else ""
    return branch if branch and branch != "HEAD" else "main"


def worktree_create(payload: str, context=None) -> str:
    """git worktree add -b <branch> <root>/<branch> <base>. Returns the path.

    `repo` says which repository to add the worktree to. Without it git ran in
    whatever directory the worker was started from — for a worker launched from
    a home directory that is `fatal: not a git repository`, and the failed node
    then blocks every dependent forever.
    """
    data = _parse(payload, required=("branch",))
    branch = data["branch"]
    repo = data.get("repo")
    base = data.get("base") or _default_base(repo)
    path = os.path.join(WORKTREE_ROOT, branch)
    os.makedirs(WORKTREE_ROOT, exist_ok=True)
    _wt_state(data.get("db"), path, "allocated", branch=branch)
    proc = _git(["worktree", "add", "-b", branch, path, base], cwd=repo)
    if proc.returncode != 0:
        _wt_state(data.get("db"), path, "quarantined", branch=branch)
        raise RuntimeError(f"worktree add: {proc.stderr.strip()[:500]}")
    _wt_state(data.get("db"), path, "active", branch=branch)
    return path


def worktree_cleanup(payload: str, context=None) -> str:
    """Commit the work, remove the worktree, drop the branch only if merged.

    `--force` throws away the working tree, so anything the agent left
    uncommitted would go with it; commit first. And `branch -d` (not `-D`):
    with merge=False nothing has merged this branch yet, and force-deleting it
    would delete the only copy of the work the pipeline just produced.
    """
    data = _parse(payload, required=("worktree_path",))
    path, branch = data["worktree_path"], data.get("branch")
    repo = data.get("repo")
    _git(["add", "-A"], cwd=path)
    _git(["commit", "-m", f"silicorism: {branch or 'work'}"], cwd=path)  # noop if clean
    rm = _git(["worktree", "remove", "--force", path], cwd=repo)
    if rm.returncode != 0:
        raise RuntimeError(f"worktree remove: {rm.stderr.strip()[:500]}")
    kept = ""
    if branch:
        if _git(["branch", "-d", branch], cwd=repo).returncode != 0:
            kept = f"; kept branch {branch} (unmerged)"
    _wt_state(data.get("db"), path, "cleaned", branch=branch)
    return f"removed {path}{kept}"


def worktree_merge(payload: str, context=None) -> str:
    """Commit worktree changes, then merge the branch into base in the main repo.

    Payload: {worktree_path, branch, base?}. Conflict/dirty-base -> raise, so
    the task fails and the worktree stays quarantined for human review.
    """
    data = _parse(payload, required=("worktree_path", "branch"))
    path, branch = data["worktree_path"], data["branch"]
    repo = data.get("repo")
    base = data.get("base") or _default_base(repo)
    # Agents often leave work uncommitted; commit it so the merge sees it.
    _git(["add", "-A"], cwd=path)
    _git(["commit", "-m", f"silicorism: {branch}"], cwd=path)  # noop if clean
    sw = _git(["switch", base], cwd=repo)
    if sw.returncode != 0:
        raise RuntimeError(f"switch {base}: {sw.stderr.strip()[:300]}")
    mg = _git(["merge", "--no-ff", "-m", f"silicorism: merge {branch}", branch], cwd=repo)
    if mg.returncode != 0:
        _git(["merge", "--abort"], cwd=repo)
        _wt_state(data.get("db"), path, "quarantined", branch=branch)
        raise RuntimeError(f"merge conflict: {mg.stdout.strip()[:300]}")
    return f"merged {branch} into {base}"


def worktree_integrate(payload: str, context=None) -> str:
    """Merge one worktree's branch into another worktree, in place.

    Payload: {into, from_worktree, branch, db?}. `worktree_merge` cannot do
    this: it runs `git switch <base>` in the main repo, and git refuses to
    check out a branch that is already checked out in another worktree. Here
    the target branch is already checked out in `into`, so no switch happens.

    Contract differs from worktree_merge on purpose: a conflict does NOT abort.
    The conflicted tree is left in place and the conflicting paths are returned,
    so the integrator agent downstream has something concrete to resolve.
    """
    data = _parse(payload, required=("into", "from_worktree", "branch"))
    into, src, branch = data["into"], data["from_worktree"], data["branch"]
    # Agents leave work uncommitted on BOTH sides; commit each tree first. Git
    # refuses to merge into a dirty tree, and that refusal looks nothing like a
    # conflict — it silently drops the source's whole slice.
    for tree, label in ((src, branch), (into, "target")):
        _git(["add", "-A"], cwd=tree)
        _git(["commit", "-m", f"silicorism: {label}"], cwd=tree)  # noop if clean
    mg = _git(["merge", "--no-ff", "-m", f"silicorism: integrate {branch}",
               branch], cwd=into)
    if mg.returncode == 0:
        # "already up to date" means the source branch had no work of its own.
        return "clean (already up to date)" if "up to date" in mg.stdout else "clean"
    conflicted = _git(["diff", "--name-only", "--diff-filter=U"], cwd=into)
    files = [f for f in conflicted.stdout.splitlines() if f]
    if not files:  # refused outright, not conflicted — nothing to hand an agent
        raise RuntimeError(
            f"merge refused: {(mg.stderr or mg.stdout).strip()[:300]}")
    _wt_state(data.get("db"), into, "quarantined", branch=branch)
    return "conflicts: " + ", ".join(files)


def verify(payload: str, context=None) -> str:
    """Deterministic gate: run the test command; non-zero exit fails the task.

    Agents can claim success — this node cannot. Payload:
    {test_command, cwd, expect_fail?}.

    `expect_fail` inverts the gate, which is the only way a DAG can insist on
    red-green order: put one after the node that writes the tests and before
    the node that implements, and a test that passes against unwritten code —
    the classic vacuous assertion — fails the run at the point it was written
    instead of certifying it four nodes later.
    """
    data = _parse(payload, required=("test_command", "cwd"))
    proc = subprocess.run(data["test_command"], shell=True, capture_output=True,
                          text=True, cwd=data["cwd"], timeout=600)
    output = (proc.stdout + proc.stderr).strip()[:500]
    if data.get("expect_fail"):
        if proc.returncode == 0:
            raise RuntimeError(
                f"verify expected failure but the command passed: "
                f"{data['test_command']}\n"
                "A new test that passes before the code exists asserts nothing. "
                f"{output}")
        return f"verify failed as expected (exit {proc.returncode}): {data['test_command']}"
    if proc.returncode != 0:
        raise RuntimeError(
            f"verify failed (exit {proc.returncode}): {output}")
    return f"verify passed: {data['test_command']}"


class RequirementsUnmet(RuntimeError):
    """A node's deliverables are missing, whatever its tests said."""


def check_requires(spec: dict, cwd: str) -> list[str]:
    """Every unmet requirement in `spec`, as operator-readable strings.

    Tests catch code that does not work. They do not catch code that was never
    written: across the Splice runs every defect that survived a green gate was
    an absence — a list capped at 3 where the spec said 6, a function imported
    by the tests and never called, an auto-fix stubbed out as a no-op with its
    test deleted. A test suite has nothing to say about any of those.

    So the plan states what must exist and this checks it literally, in the
    worker, with no model in the loop:

        {"files":     ["src/auth.go"],
         "symbols":   {"src/auth.go": ["func ValidateJWT", "type Claims"]},
         "absent":    {"src/auth.go": ["TODO", "not implemented"]},
         "min_lines": {"tests/auth_test.go": 20}}

    Substrings, not parsing: it has to work on Go, Rust, Python, TSX and YAML
    without a grammar per language, and a plan that names `func ValidateJWT`
    is already saying the thing precisely enough.
    """
    problems: list[str] = []

    def read(rel: str) -> str | None:
        path = rel if os.path.isabs(rel) else os.path.join(cwd, rel)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    for rel in spec.get("files") or []:
        body = read(rel)
        if body is None:
            problems.append(f"{rel}: required file was not created")
        elif not body.strip():
            problems.append(f"{rel}: required file is empty")

    for rel, needles in (spec.get("symbols") or {}).items():
        body = read(rel)
        if body is None:
            problems.append(f"{rel}: required file was not created")
            continue
        for needle in needles:
            if needle not in body:
                problems.append(f"{rel}: required symbol {needle!r} is missing")

    for rel, needles in (spec.get("absent") or {}).items():
        body = read(rel)
        if body is None:
            continue  # its absence is already reported by files/symbols
        for needle in needles:
            if needle in body:
                problems.append(
                    f"{rel}: contains {needle!r}, which this node was told not to leave behind")

    for rel, minimum in (spec.get("min_lines") or {}).items():
        body = read(rel)
        if body is None:
            problems.append(f"{rel}: required file was not created")
            continue
        actual = len(body.splitlines())
        if actual < int(minimum):
            problems.append(f"{rel}: {actual} lines, expected at least {minimum}")

    return problems


def _agent_payload(prompt, model, thinking, cwd) -> str:
    d = {"prompt": prompt, "model": model, "cwd": cwd}
    if thinking:
        d["thinking"] = thinking
    return json.dumps(d)


def _ask_upstream(db_path, fixer_id, upstream, agent, model, thinking, cwd, error) -> str:
    """P2P clarification: fixer asks the upstream task, records both notes.

    Sends the question on the channel, invokes the upstream agent to answer,
    records the answer back to the fixer, and returns it for prompt injection.
    Entirely best-effort — any failure just yields no clarification.
    """
    question = (f"Tests keep failing:\n{error[:800]}\n"
                "What was the intended architecture / expected test behavior?")
    try:
        conn = db.connect(db_path)
        try:
            db.send_inter_agent_message(conn, fixer_id, upstream, question)
            reply = HANDLERS[agent](_agent_payload(question, model, thinking, cwd))
            db.send_inter_agent_message(conn, upstream, fixer_id, reply)
            return reply
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - clarification is advisory
        return ""


def fixer_loop(payload: str, context=None) -> str:
    """Run tests; on failure feed the log to an agent and retry up to N times.

    Payload: {test_command, agent_type, model, cwd, max_attempts,
              db, upstream, agent_id}. After two failed attempts the fixer
    queries the upstream task (Scout/Builder) over the P2P channel for
    architectural clarification before spending its remaining retries.
    """
    data = _parse(payload, required=("test_command", "cwd"))
    cwd = data["cwd"]
    agent = data.get("agent_type") or "pi"
    if agent not in ("pi", "claude"):
        raise ValueError(f"fixer_loop: bad agent_type {agent!r}")
    attempts = max(1, int(data.get("max_attempts", 3)))
    db_path = data.get("db")
    upstream = data.get("upstream")
    fixer_id = data.get("agent_id") or "fixer"
    model = data.get("model")
    thinking = data.get("thinking")

    last = ""
    clarification = ""
    asked = False
    for i in range(1, attempts + 1):
        proc = subprocess.run(data["test_command"], shell=True,
                              capture_output=True, text=True, cwd=cwd, timeout=600)
        if proc.returncode == 0:
            return f"tests passed on attempt {i}"
        last = (proc.stdout + proc.stderr).strip()[:2000]
        if i == attempts:
            break
        # After two failures, ask upstream once before burning remaining retries.
        if i >= 2 and not asked and db_path and upstream:
            asked = True
            clarification = _ask_upstream(
                db_path, fixer_id, upstream, agent, model, thinking, cwd, last)
        prompt = f"The tests failed with errors:\n{last}\nFix the files in the directory."
        if clarification:
            prompt += f"\n\nUpstream clarification:\n{clarification}"
        try:
            HANDLERS[agent](_agent_payload(prompt, model, thinking, cwd))
        except Exception:  # noqa: BLE001 - the next test run is the real verdict
            pass

    _wt_state(db_path, cwd, "quarantined")  # give up: leave worktree for review
    raise RuntimeError(
        f"fixer_loop: tests still failing after {attempts} attempts: {last[:300]}")


HANDLERS = {
    "shell": _shell,
    "sleep": _sleep,
    "echo": _echo,
    "fail": _fail,
    "pi": run_pi_agent,
    "claude": run_claude_agent,
    "worktree_create": worktree_create,
    "worktree_cleanup": worktree_cleanup,
    "worktree_merge": worktree_merge,
    "worktree_integrate": worktree_integrate,
    "verify": verify,
    "fixer_loop": fixer_loop,
}


def run(task_type: str, payload: str | None, context=None) -> str:
    """Dispatch to a handler. `context` is {dep_id: artifact} from prior tasks."""
    handler = HANDLERS.get(task_type)
    if handler is None:
        raise ValueError(f"no handler for task_type {task_type!r}")
    return handler(payload or "", context)


if __name__ == "__main__":
    # self-check: each handler behaves, unknown type + fail both raise
    assert run("echo", "hi") == "hi"
    assert run("sleep", "0") == "slept 0.0s"
    assert "hello" in run("shell", "echo hello")
    for bad in (("fail", "x"), ("nope", "x")):
        try:
            run(*bad)
        except Exception:
            pass
        else:
            raise AssertionError(f"{bad} should have raised")
    # verify gate: exit 0 passes, non-zero raises
    assert "passed" in verify(json.dumps({"test_command": "true", "cwd": "/tmp"}))
    try:
        verify(json.dumps({"test_command": "false", "cwd": "/tmp"}))
    except RuntimeError:
        pass
    else:
        raise AssertionError("failing verify must raise")
    # escalation ladder: kimi -> glm -> None; an off-ladder model joins at rung 1
    p1 = escalate_payload("pi", json.dumps({"prompt": "x", "model": "kimi-k2.5"}))
    assert json.loads(p1)["model"] == "bedrock-mantle/zai.glm-5"
    assert escalate_payload("pi", p1) is None
    off = escalate_payload("pi", json.dumps({"prompt": "x", "model": "hy3"}))
    assert json.loads(off)["model"] == "bedrock-mantle/moonshotai.kimi-k2.5"
    assert escalate_payload("shell", "echo hi") is None
    # native pi command: full TUI (no -p) + autoexit extension + artifact env
    cmd = native_command("pi", json.dumps(
        {"prompt": "go", "model": "glm-5", "artifact": "/tmp/a.txt"}))
    assert "autoexit.ts" in cmd and " -p " not in cmd, cmd
    assert "SILICORISM_ARTIFACT=/tmp/a.txt" in cmd, cmd
    assert "bedrock-mantle/zai.glm-5" in cmd, cmd
    print(json.dumps({"ok": True, "handlers": list(HANDLERS)}))
