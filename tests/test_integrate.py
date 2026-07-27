"""worktree_integrate against real git — a merge asserted only against command
strings proves nothing about whether the merge works."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handlers  # noqa: E402


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    """A repo on 'main' with two worktrees branched from it."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-b", "main"], root)
    _git(["config", "user.email", "t@t"], root)
    _git(["config", "user.name", "t"], root)
    (root / "base.txt").write_text("base\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "init"], root)
    wt_a, wt_b = tmp_path / "wt-a", tmp_path / "wt-b"
    _git(["worktree", "add", "-b", "feat-a", str(wt_a), "main"], root)
    _git(["worktree", "add", "-b", "feat-b", str(wt_b), "main"], root)
    for wt in (wt_a, wt_b):
        _git(["config", "user.email", "t@t"], wt)
        _git(["config", "user.name", "t"], wt)
    return root, wt_a, wt_b


def test_disjoint_changes_merge_cleanly(repo):
    _, wt_a, wt_b = repo
    (wt_a / "a.py").write_text("A\n")
    _git(["add", "-A"], wt_a)
    _git(["commit", "-m", "a"], wt_a)
    (wt_b / "b.py").write_text("B\n")

    out = handlers.worktree_integrate(json.dumps(
        {"into": str(wt_a), "from_worktree": str(wt_b), "branch": "feat-b"}))

    assert out == "clean"
    assert (wt_a / "b.py").read_text() == "B\n"  # b's work landed in a


def test_overlapping_changes_report_conflicts_and_leave_the_tree_conflicted(repo):
    _, wt_a, wt_b = repo
    (wt_a / "same.py").write_text("from A\n")
    _git(["add", "-A"], wt_a)
    _git(["commit", "-m", "a"], wt_a)
    (wt_b / "same.py").write_text("from B\n")

    out = handlers.worktree_integrate(json.dumps(
        {"into": str(wt_a), "from_worktree": str(wt_b), "branch": "feat-b"}))

    assert out.startswith("conflicts:") and "same.py" in out
    # Left conflicted on purpose so the integrator agent has something to fix.
    assert "<<<<<<<" in (wt_a / "same.py").read_text()
    status = _git(["status", "--porcelain"], wt_a).stdout
    assert "UU" in status or "AA" in status


def test_missing_required_field_raises():
    with pytest.raises(ValueError):
        handlers.worktree_integrate(json.dumps({"into": "/tmp/x"}))


def test_uncommitted_work_in_the_target_does_not_swallow_the_source(repo):
    """Builders leave work uncommitted; git refuses to merge into a dirty tree."""
    _, wt_a, wt_b = repo
    (wt_a / "a.py").write_text("A\n")  # never committed
    (wt_b / "b.py").write_text("B\n")

    out = handlers.worktree_integrate(json.dumps(
        {"into": str(wt_a), "from_worktree": str(wt_b), "branch": "feat-b"}))

    assert out == "clean"
    assert (wt_a / "b.py").read_text() == "B\n"  # b's slice actually landed
    assert (wt_a / "a.py").read_text() == "A\n"  # a's own work survived


def test_an_empty_source_branch_is_reported_not_silently_clean(repo):
    _, wt_a, wt_b = repo
    (wt_a / "a.py").write_text("A\n")
    _git(["add", "-A"], wt_a)
    _git(["commit", "-m", "a"], wt_a)

    out = handlers.worktree_integrate(json.dumps(
        {"into": str(wt_a), "from_worktree": str(wt_b), "branch": "feat-b"}))

    assert out == "clean (already up to date)"
