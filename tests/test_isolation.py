"""F9: an execution node runs on what the plan gave it, not on what happens to
be installed on the operator's machine.

Measured with a real `pi -p` on the same five-word prompt: 13,976 input tokens
with discovery on, 1,815 with it off. That difference is the operator's global
CLAUDE.md, their skills, their prompt templates and every installed extension,
paid on every turn of every node, and it varies per machine.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handlers  # noqa: E402


def _cmd(payload: dict) -> list[str]:
    built = handlers.native_command("pi", json.dumps(payload), None,
                                    cli_path="/x/cli.py")
    assert built is not None
    return shlex.split(built)


def test_a_node_does_not_inherit_the_operators_pi_setup():
    parts = _cmd({"prompt": "build it", "model": "kimi-k2.5"})
    for flag in ("-nc", "-ne", "-ns", "-np"):
        assert flag in parts, (flag, parts)


def test_the_nodes_own_extension_still_loads():
    """-ne disables discovery; an explicit -e path is unaffected, and autoexit
    is what gives the pane a deterministic exit code."""
    parts = _cmd({"prompt": "x"})
    assert "-e" in parts
    assert parts[parts.index("-e") + 1] == handlers.AUTOEXIT_EXT
    assert handlers.AUTOEXIT_EXT.endswith("autoexit.ts")


def test_the_orchestrators_own_tools_stay_out_of_a_workers_hands():
    """extensions/silicorism.ts registers silicorism_plan_and_submit. Discovered
    from ~/.pi/extensions, it let an execution node queue its own DAGs."""
    parts = _cmd({"prompt": "x"})
    assert "-ne" in parts
    assert not any("silicorism.ts" in p for p in parts)


def test_the_projects_own_context_file_is_added_back_by_path(tmp_path):
    """Dropping discovery must not cost a node its repo's conventions: those are
    part of the task. The operator's global rules are not."""
    (tmp_path / "AGENTS.md").write_text("run tests with `pytest -q`\n")
    parts = _cmd({"prompt": "x", "cwd": str(tmp_path)})
    assert "--append-system-prompt" in parts
    assert parts[parts.index("--append-system-prompt") + 1] \
        == str(tmp_path / "AGENTS.md")


def test_claude_md_is_used_when_there_is_no_agents_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("this repo uses tabs\n")
    parts = _cmd({"prompt": "x", "cwd": str(tmp_path)})
    assert parts[parts.index("--append-system-prompt") + 1] \
        == str(tmp_path / "CLAUDE.md")


def test_no_context_file_means_no_empty_flag(tmp_path):
    """pi treats --append-system-prompt as literal text when the path is not a
    file, so a missing file must not become the string 'AGENTS.md'."""
    # Checked by value, not by flag: splice's overlay uses the same flag, so
    # its absence is no longer the signal.
    def _appended(payload):
        parts = _cmd(payload)
        return [parts[i + 1] for i, p in enumerate(parts)
                if p == "--append-system-prompt"]

    assert not any(a.endswith(("AGENTS.md", "CLAUDE.md"))
                   for a in _appended({"prompt": "x", "cwd": str(tmp_path)}))
    # and an unset cwd must not reach into whatever directory the worker is in
    assert not any(a.endswith(("AGENTS.md", "CLAUDE.md"))
                   for a in _appended({"prompt": "x"}))


def test_a_claude_node_does_not_inherit_the_operators_setup():
    """The pi branch had this and the claude branch did not, so the same DAG
    was isolated or not depending on which harness a node was given."""
    built = handlers.native_command("claude", json.dumps({"prompt": "x"}))
    assert built is not None
    parts = shlex.split(built)
    assert parts[parts.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in parts
    assert "--no-session-persistence" in parts
    # --add-dir would widen access past the worktree the pane opened in
    assert "--add-dir" not in parts


def test_a_claude_node_still_gets_its_prompt_and_model_last():
    built = handlers.native_command("claude", json.dumps(
        {"prompt": "build it", "model": "claude-opus-5"}))
    assert built is not None
    parts = shlex.split(built)
    # Last argument, and it opens with the prompt: the default skill text is
    # appended to it, so an equality check would only be asserting the default.
    assert parts[-1].startswith("build it")
    assert parts[parts.index("--model") + 1] == "claude-opus-5"


def test_the_worker_tells_the_command_where_the_task_runs(tmp_path):
    """native_command can only test for a context file if it knows the cwd, and
    a worktree node carries its path on the row, not in the payload."""
    import worker
    task = {"id": 1, "task_type": "pi", "worktree_path": str(tmp_path),
            "payload": json.dumps({"prompt": "x"})}
    data = json.loads(worker._native_payload(task))
    assert data["cwd"] == str(tmp_path)
    # an explicit payload cwd wins: it is what the planner asked for
    task2 = dict(task, worktree_path=None,
                 payload=json.dumps({"prompt": "x", "cwd": "/explicit"}))
    assert json.loads(worker._native_payload(task2))["cwd"] == "/explicit"
