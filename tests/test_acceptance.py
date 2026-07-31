"""The two gates a test suite cannot be: absence, and red-before-green.

Every defect that survived a green verify gate in the July Splice runs was an
absence — a list capped at 3 where the spec said 6, a function imported by the
tests and never called, an auto-fix stubbed as a no-op with its test deleted.
Tests only fail on what they cover, so none of those could ever have been
caught by running them. These check the machinery that does catch them.
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import handlers
import silicorism_tools as st
import worker


# ── requires: what the plan said must exist ──────────────────────────────────

def test_missing_file_is_reported(tmp_path):
    unmet = handlers.check_requires({"files": ["src/auth.go"]}, str(tmp_path))
    assert len(unmet) == 1
    assert "src/auth.go" in unmet[0] and "not created" in unmet[0]


def test_empty_file_does_not_count_as_created(tmp_path):
    (tmp_path / "auth.go").write_text("   \n\n")
    unmet = handlers.check_requires({"files": ["auth.go"]}, str(tmp_path))
    assert "empty" in unmet[0]


def test_present_file_passes(tmp_path):
    (tmp_path / "auth.go").write_text("package auth\n")
    assert handlers.check_requires({"files": ["auth.go"]}, str(tmp_path)) == []


def test_missing_symbol_is_reported(tmp_path):
    (tmp_path / "auth.go").write_text("package auth\n\nfunc Login() {}\n")
    unmet = handlers.check_requires(
        {"symbols": {"auth.go": ["func Login", "func ValidateJWT"]}}, str(tmp_path))
    assert len(unmet) == 1
    assert "func ValidateJWT" in unmet[0]


def test_stub_markers_are_rejected(tmp_path):
    """The exact July failure: a feature 'implemented' as a TODO."""
    (tmp_path / "fix.py").write_text("def auto_fix():\n    pass  # TODO: not critical\n")
    unmet = handlers.check_requires(
        {"absent": {"fix.py": ["TODO", "not critical"]}}, str(tmp_path))
    assert len(unmet) == 2


def test_absent_check_ignores_a_file_that_does_not_exist(tmp_path):
    """files/symbols already report the absence; two errors for one cause is noise."""
    assert handlers.check_requires({"absent": {"gone.py": ["TODO"]}}, str(tmp_path)) == []


def test_min_lines_catches_a_token_test_file(tmp_path):
    (tmp_path / "auth_test.go").write_text("package auth\n")
    unmet = handlers.check_requires({"min_lines": {"auth_test.go": 20}}, str(tmp_path))
    assert "1 lines, expected at least 20" in unmet[0]


def test_everything_at_once_reports_every_problem(tmp_path):
    (tmp_path / "a.py").write_text("x = 1  # TODO\n")
    unmet = handlers.check_requires({
        "files": ["b.py"],
        "symbols": {"a.py": ["def go"]},
        "absent": {"a.py": ["TODO"]},
        "min_lines": {"a.py": 10},
    }, str(tmp_path))
    assert len(unmet) == 4, unmet


# ── the worker enforces it, not the agent ────────────────────────────────────

def _task(payload: dict, task_type="pi"):
    return {"id": 1, "task_type": task_type, "payload": json.dumps(payload),
            "worktree_path": None}


def test_worker_fails_a_node_whose_deliverables_are_missing(tmp_path):
    task = _task({"cwd": str(tmp_path), "requires": {"files": ["api.go"]}})
    with pytest.raises(handlers.RequirementsUnmet) as err:
        worker._apply_gate(task, "the agent said it was done")
    assert "api.go" in str(err.value)


def test_worker_passes_a_node_that_delivered(tmp_path):
    (tmp_path / "api.go").write_text("package api\n\nfunc Serve() {}\n")
    task = _task({"cwd": str(tmp_path),
                  "requires": {"symbols": {"api.go": ["func Serve"]}}})
    out = worker._apply_gate(task, "done")
    assert "all declared deliverables present" in out


def test_requires_is_checked_before_the_tests(tmp_path):
    """A green suite must not rescue a node that never wrote the file."""
    task = _task({"cwd": str(tmp_path), "requires": {"files": ["api.go"]},
                  "test_command": "true"})
    with pytest.raises(handlers.RequirementsUnmet):
        worker._apply_gate(task, "done")


def test_a_node_without_requires_is_unaffected(tmp_path):
    assert worker._apply_gate(_task({"cwd": str(tmp_path)}), "done") == "done"


# ── expect_fail: red before green ────────────────────────────────────────────

def test_expect_fail_passes_when_the_command_fails(tmp_path):
    out = handlers.verify(json.dumps(
        {"test_command": "false", "cwd": str(tmp_path), "expect_fail": True}))
    assert "failed as expected" in out


def test_expect_fail_rejects_a_test_that_already_passes(tmp_path):
    with pytest.raises(RuntimeError) as err:
        handlers.verify(json.dumps(
            {"test_command": "true", "cwd": str(tmp_path), "expect_fail": True}))
    assert "asserts nothing" in str(err.value)


def test_normal_verify_is_unchanged(tmp_path):
    assert "verify passed" in handlers.verify(json.dumps(
        {"test_command": "true", "cwd": str(tmp_path)}))
    with pytest.raises(RuntimeError):
        handlers.verify(json.dumps({"test_command": "false", "cwd": str(tmp_path)}))


# ── submit-time validation ───────────────────────────────────────────────────

def _conn(tmp_path):
    path = str(tmp_path / "s.db")
    db.init_db(path)
    return db.connect(path), path


def test_requires_reaches_the_payload(tmp_path):
    conn, path = _conn(tmp_path)
    spec = {"files": ["a.go"], "symbols": {"a.go": ["func A"]}}
    st.build_dag(conn, path, [{"id": "build", "prompt": "write it",
                               "model": "kimi-k2.5", "requires": spec}],
                 cwd=str(tmp_path))
    row = conn.execute("SELECT payload FROM tasks WHERE task_type = 'pi'").fetchone()
    assert json.loads(row["payload"])["requires"] == spec


def test_expect_fail_reaches_the_payload(tmp_path):
    conn, path = _conn(tmp_path)
    st.build_dag(conn, path, [{"id": "red", "harness": "verify",
                               "test_command": "pytest -q", "expect_fail": True}],
                 cwd=str(tmp_path))
    row = conn.execute("SELECT payload FROM tasks WHERE task_type = 'verify'").fetchone()
    assert json.loads(row["payload"])["expect_fail"] is True


@pytest.mark.parametrize("spec", [
    "files: a.go",                                  # not an object
    {"fils": ["a.go"]},                             # typo in a key
    {"symbols": ["a.go"]},                          # list where a map belongs
    {"symbols": {"a.go": "func A"}},                # string where a list belongs
    {"min_lines": {"a.go": "20"}},                  # string where an int belongs
])
def test_a_malformed_requires_is_rejected_at_submit(tmp_path, spec):
    """A requirement that silently does nothing is worse than none: the plan is
    written believing it is checked."""
    conn, path = _conn(tmp_path)
    with pytest.raises(ValueError):
        st.build_dag(conn, path, [{"id": "build", "prompt": "x",
                                   "model": "kimi-k2.5", "requires": spec}],
                     cwd=str(tmp_path))


# ── worktree: the repo, not the worker's cwd ─────────────────────────────────

def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_a_named_dag_outside_a_repo_is_rejected_at_submit(tmp_path):
    """Previously this submitted, then failed inside worktree_create — and the
    pending nodes behind it could not be cleared."""
    conn, path = _conn(tmp_path)
    with pytest.raises(ValueError) as err:
        st.build_dag(conn, path, [{"id": "b", "prompt": "x", "model": "kimi-k2.5"}],
                     name="feat", cwd=str(tmp_path))
    assert "not inside a git repository" in str(err.value)


def test_worktree_create_carries_the_repo(tmp_path):
    conn, path = _conn(tmp_path)
    repo = _git_repo(tmp_path)
    st.build_dag(conn, path, [{"id": "b", "prompt": "x", "model": "kimi-k2.5"}],
                 name="feat", cwd=str(repo))
    row = conn.execute(
        "SELECT payload FROM tasks WHERE task_type = 'worktree_create'").fetchone()
    assert json.loads(row["payload"])["repo"] == str(repo)


def test_worktree_create_runs_in_the_repo_it_was_given(tmp_path, monkeypatch):
    """The bug: git ran in the worker's cwd. Started anywhere but the repo — a
    home directory, say — every named DAG died with 'not a git repository'."""
    repo = _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)  # NOT the repo
    monkeypatch.setattr(handlers, "WORKTREE_ROOT", str(tmp_path / "wt"))
    out = handlers.worktree_create(json.dumps(
        {"branch": "feature-x", "repo": str(repo)}))
    assert os.path.isdir(out)
    assert (tmp_path / "wt" / "feature-x" / "README.md").exists()


def test_base_defaults_to_the_repos_own_branch(tmp_path, monkeypatch):
    """git init makes `master` here; defaulting to "main" is a guess about
    someone else's repository."""
    repo = _git_repo(tmp_path)
    current = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
                             capture_output=True, text=True).stdout.strip()
    assert handlers._default_base(str(repo)) == current


# ── discipline is the floor ──────────────────────────────────────────────────

def test_an_agent_node_gets_the_default_skill(tmp_path):
    prompt = handlers._prompt({"prompt": "build it", "cwd": str(tmp_path)}, None)
    assert "coding-excellence" in prompt


def test_a_node_can_still_opt_out(tmp_path):
    prompt = handlers._prompt({"prompt": "build it", "cwd": str(tmp_path),
                               "skills": []}, None)
    assert "coding-excellence" not in prompt


def test_an_explicit_skill_list_wins(tmp_path):
    prompt = handlers._prompt({"prompt": "build it", "cwd": str(tmp_path),
                               "skills": ["silicorism"]}, None)
    assert "Skill: silicorism" in prompt
    assert "Skill: coding-excellence" not in prompt


def test_an_injected_skill_says_where_its_other_files_are(tmp_path):
    """coding-excellence's SKILL.md opens with 'read CORE.md now'. A node with
    discovery off cannot resolve a bare filename."""
    import skills
    if not skills.find_skill("coding-excellence", cwd=str(tmp_path)):
        pytest.skip("coding-excellence is not installed on this machine")
    text = skills.load_skills(["coding-excellence"], cwd=str(tmp_path))
    assert "skill files are in /" in text
