"""Splice: the models edit through an AST-anchored, diagnostic-gated patcher.

A weak model's dominant failure is a botched whole-file rewrite. Splice makes
the edit targeted (`read_scope_map` -> anchor -> `splice_edit`) and puts an LSP
delta gate in front of it, so an edit that introduces a new diagnostic is
rejected rather than committed. This wires it into the pane without giving the
node back the discovery it is deliberately denied.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handlers  # noqa: E402


def _fake_splice(root: Path, *, overlay=True) -> Path:
    (root / "extensions").mkdir(parents=True)
    (root / "extensions" / "splice.ts").write_text("// splice\n")
    if overlay:
        (root / "system-prompt-overlay.md").write_text("call read_scope_map first\n")
    return root


def _cmd(payload=None) -> list[str]:
    built = handlers.native_command("pi", json.dumps(payload or {"prompt": "x"}))
    assert built is not None
    return shlex.split(built)


def test_splice_is_found_by_env_before_any_probe(tmp_path, monkeypatch):
    root = _fake_splice(tmp_path / "splice")
    monkeypatch.setenv(handlers.SPLICE_ENV, str(root))
    monkeypatch.setattr(handlers, "SPLICE_PROBES", ())
    assert handlers.splice_root() == str(root)


def test_a_root_without_the_extension_is_not_splice(tmp_path, monkeypatch):
    """A stale env var must read as 'not installed', not as a broken -e path
    that kills every pane at startup."""
    monkeypatch.setenv(handlers.SPLICE_ENV, str(tmp_path))
    monkeypatch.setattr(handlers, "SPLICE_PROBES", ())
    assert handlers.splice_root() is None


def test_a_node_gets_the_splice_tools_by_explicit_path(tmp_path, monkeypatch):
    """-ne stays on: the node still cannot discover the operator's extensions.
    Splice comes in the same way autoexit does, by a path the worker chose."""
    root = _fake_splice(tmp_path / "splice")
    monkeypatch.setenv(handlers.SPLICE_ENV, str(root))
    monkeypatch.setattr(handlers, "SPLICE_PROBES", ())
    parts = _cmd()
    assert "-ne" in parts
    loaded = [parts[i + 1] for i, p in enumerate(parts) if p == "-e"]
    assert handlers.AUTOEXIT_EXT in loaded
    assert str(root / "extensions" / "splice.ts") in loaded


def test_the_operator_overlay_teaches_the_workflow(tmp_path, monkeypatch):
    """Registering the tools is not enough — a weak model ignores tools it was
    never told to prefer. The overlay is splice's own operating manual."""
    root = _fake_splice(tmp_path / "splice")
    monkeypatch.setenv(handlers.SPLICE_ENV, str(root))
    monkeypatch.setattr(handlers, "SPLICE_PROBES", ())
    parts = _cmd()
    appended = [parts[i + 1] for i, p in enumerate(parts)
                if p == "--append-system-prompt"]
    assert str(root / "system-prompt-overlay.md") in appended


def test_a_missing_overlay_is_not_a_missing_extension(tmp_path, monkeypatch):
    root = _fake_splice(tmp_path / "splice", overlay=False)
    monkeypatch.setenv(handlers.SPLICE_ENV, str(root))
    monkeypatch.setattr(handlers, "SPLICE_PROBES", ())
    parts = _cmd()
    assert str(root / "extensions" / "splice.ts") in parts
    assert not any(p.endswith("system-prompt-overlay.md") for p in parts)


def test_without_splice_the_pane_still_launches(monkeypatch):
    """The orchestrator has to work on a machine that has no splice checkout."""
    monkeypatch.delenv(handlers.SPLICE_ENV, raising=False)
    monkeypatch.setattr(handlers, "SPLICE_PROBES", ())
    parts = _cmd()
    assert parts[0] == "pi" and handlers.AUTOEXIT_EXT in parts
    assert not any("splice" in p for p in parts)


def test_the_projects_own_context_file_still_wins_a_slot(tmp_path, monkeypatch):
    """Two --append-system-prompt uses, not one overwriting the other: the repo's
    conventions and splice's manual are both part of the job."""
    root = _fake_splice(tmp_path / "splice")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("use tabs\n")
    monkeypatch.setenv(handlers.SPLICE_ENV, str(root))
    monkeypatch.setattr(handlers, "SPLICE_PROBES", ())
    parts = _cmd({"prompt": "x", "cwd": str(repo)})
    appended = [parts[i + 1] for i, p in enumerate(parts)
                if p == "--append-system-prompt"]
    assert str(repo / "AGENTS.md") in appended
    assert str(root / "system-prompt-overlay.md") in appended


def test_a_deliberate_env_value_turns_splice_off(tmp_path, monkeypatch):
    """The only way to run without splice on a machine that has it installed —
    and what makes an A/B token measurement possible at all."""
    _fake_splice(tmp_path / "real")
    monkeypatch.setattr(handlers, "SPLICE_PROBES", (str(tmp_path / "real"),))
    monkeypatch.setenv(handlers.SPLICE_ENV, "off")
    assert handlers.splice_root() is None
    # and with the variable unset the probe still finds it
    monkeypatch.delenv(handlers.SPLICE_ENV)
    assert handlers.splice_root() == str(tmp_path / "real")
