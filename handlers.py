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
    "deepseek-v4-flash": "opencode/deepseek-v4-flash-free",
    "nemotron-3-ultra": "opencode/nemotron-3-ultra-free",
    "hy3": "opencode/hy3-free",
    "mimo-2.5": "opencode/mimo-v2.5-free",
    "mimo-v2.5": "opencode/mimo-v2.5-free",
    "north-mini-code": "opencode/north-mini-code-free",
}

# Retry escalation: each failed attempt bumps a pi task to the next stronger
# model. OSS-only by design — a retry must never silently bill Claude tokens.
# qwen3-coder-480b is deliberately absent: it stays reachable by name through
# MODEL_ALIASES, but nothing routes onto it by default.
ESCALATION = [
    "bedrock-mantle/moonshotai.kimi-k2.5",
    "bedrock-mantle/zai.glm-5",
]


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
    data["model"] = nxt
    return json.dumps(data)


def resolve_model(model: str | None) -> str | None:
    """Map a friendly canonical name to its opencode id; pass full ids through."""
    return MODEL_ALIASES.get(model, model) if model else model

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


def _with_context(prompt: str, context) -> str:
    """Append completed-dependency artifacts to a prompt (artifact hand-off)."""
    if not context:
        return prompt
    parts = [str(v) for v in context.values() if v]
    if not parts:
        return prompt
    return prompt + "\n\n--- Context from prior tasks ---\n" + "\n".join(parts)


def _prompt(data: dict, context, *, native=False) -> str:
    """Build the agent prompt: base + dep artifacts + requested skills + P2P."""
    p = _with_context(data["prompt"], context)
    injected = skills.load_skills(data.get("skills"), cwd=data.get("cwd"))
    if injected:
        p += "\n\n" + injected
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
        parts = ["pi", "-e", AUTOEXIT_EXT, "--no-session",
                 "--model", resolve_model(data.get("model")) or DEFAULT_PI_MODEL]
        if data.get("thinking"):
            parts += ["--thinking", data["thinking"]]
        parts.append(prompt)
    else:  # claude
        parts = ["claude", "-p"]
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


def worktree_create(payload: str, context=None) -> str:
    """git worktree add -b <branch> <root>/<branch> <base>. Returns the path."""
    data = _parse(payload, required=("branch",))
    branch = data["branch"]
    base = data.get("base") or "main"
    path = os.path.join(WORKTREE_ROOT, branch)
    os.makedirs(WORKTREE_ROOT, exist_ok=True)
    _wt_state(data.get("db"), path, "allocated", branch=branch)
    proc = _git(["worktree", "add", "-b", branch, path, base])
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
    _git(["add", "-A"], cwd=path)
    _git(["commit", "-m", f"silicorism: {branch or 'work'}"], cwd=path)  # noop if clean
    rm = _git(["worktree", "remove", "--force", path])
    if rm.returncode != 0:
        raise RuntimeError(f"worktree remove: {rm.stderr.strip()[:500]}")
    kept = ""
    if branch:
        if _git(["branch", "-d", branch]).returncode != 0:
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
    base = data.get("base") or "main"
    # Agents often leave work uncommitted; commit it so the merge sees it.
    _git(["add", "-A"], cwd=path)
    _git(["commit", "-m", f"silicorism: {branch}"], cwd=path)  # noop if clean
    sw = _git(["switch", base])
    if sw.returncode != 0:
        raise RuntimeError(f"switch {base}: {sw.stderr.strip()[:300]}")
    mg = _git(["merge", "--no-ff", "-m", f"silicorism: merge {branch}", branch])
    if mg.returncode != 0:
        _git(["merge", "--abort"])
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

    Agents can claim success — this node cannot. Payload: {test_command, cwd}.
    """
    data = _parse(payload, required=("test_command", "cwd"))
    proc = subprocess.run(data["test_command"], shell=True, capture_output=True,
                          text=True, cwd=data["cwd"], timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(
            f"verify failed (exit {proc.returncode}): "
            f"{(proc.stdout + proc.stderr).strip()[:500]}")
    return f"verify passed: {data['test_command']}"


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
