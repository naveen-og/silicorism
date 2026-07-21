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
        parts = ["pi", "-p", "--model", data.get("model") or "opencode/deepseek-v4-flash-free"]
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
    cmd = ["pi", "--model", data.get("model") or "opencode/deepseek-v4-flash-free"]
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
    """git worktree remove --force <path>, then delete the temp branch."""
    data = _parse(payload, required=("worktree_path",))
    path = data["worktree_path"]
    rm = _git(["worktree", "remove", "--force", path])
    if rm.returncode != 0:
        raise RuntimeError(f"worktree remove: {rm.stderr.strip()[:500]}")
    if data.get("branch"):
        _git(["branch", "-D", data["branch"]])  # best-effort; ok if absent
    _wt_state(data.get("db"), path, "cleaned", branch=data.get("branch"))
    return f"removed {path}"


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
    print(json.dumps({"ok": True, "handlers": list(HANDLERS)}))
