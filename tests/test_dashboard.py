"""The frame builder is a pure function, so the whole dashboard is testable
without a terminal. curses only paints the strings it returns."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import dashboard  # noqa: E402
import db  # noqa: E402

NOW = datetime(2026, 7, 25, 12, 0, 30, tzinfo=timezone.utc)


def _row(tid, ttype, status, *, deps=None, payload=None, started=None,
         updated=None, pane=None, progress=None):
    return {"id": tid, "task_type": ttype, "status": status,
            "depends_on": json.dumps(deps) if deps else None,
            "payload": payload, "started_at": started, "updated_at": updated,
            "pane_target": pane, "agent_id": None, "last_progress_at": progress}


def _counts(**kw):
    return {"pending": 0, "processing": 0, "completed": 0, "failed": 0, **kw}


def test_short_model_reverses_the_alias_table():
    payload = json.dumps(
        {"model": "bedrock-mantle/qwen.qwen3-coder-480b-a35b-instruct"})
    assert dashboard.short_model(payload) == "qwen3-coder-480b"


def test_short_model_survives_garbage():
    assert dashboard.short_model("not json") == "-"
    assert dashboard.short_model(None) == "-"
    assert dashboard.short_model("{}") == "-"


def test_elapsed_formats_running_and_finished_tasks():
    running = _row(1, "pi", "processing", started="2026-07-25T12:00:00.000Z")
    assert dashboard.elapsed(running, NOW) == "0m30"
    done = _row(2, "pi", "completed", started="2026-07-25T12:00:00.000Z",
                updated="2026-07-25T12:01:02.000Z")
    assert dashboard.elapsed(done, NOW) == "1m02"
    assert dashboard.elapsed(_row(3, "pi", "pending"), NOW) == "-"


def test_a_linear_chain_renders_flat_and_a_fanout_indents():
    """Indentation on a chain says nothing and costs every column to its right,
    so it is only spent where a node really has more than one child."""
    chain = [_row(1, "worktree_create", "completed"),
             _row(2, "pi", "completed", deps=[1]),
             _row(3, "pi", "processing", deps=[2])]
    flat = dashboard.build_frame(chain, [], _counts(processing=1, completed=2),
                                 now=NOW)
    body = [ln for ln in flat if "] pi" in ln or "] worktree" in ln]
    assert len(body) == 3 and not any("+-" in ln for ln in body), body

    branched = chain + [_row(4, "pi", "pending", deps=[2])]
    tree = dashboard.build_frame(branched, [], _counts(pending=1, processing=1,
                                                      completed=2), now=NOW)
    rows = [ln for ln in tree if "] pi" in ln or "] worktree" in ln]
    assert not rows[0].lstrip().startswith("+-")          # the root
    assert rows[1].lstrip().startswith("+-")              # its only child
    assert rows[2].index("+-") > rows[1].index("+-")      # the two siblings
    assert rows[3].index("+-") == rows[2].index("+-")


def test_fanout_siblings_render_at_equal_depth():
    tasks = [_row(1, "pi", "completed"),
             _row(2, "pi", "processing", deps=[1]),
             _row(3, "pi", "processing", deps=[1])]
    frame = dashboard.build_frame(tasks, [], {"pending": 0, "processing": 2,
                                              "completed": 1, "failed": 0},
                                  now=NOW)
    lines = [ln for ln in frame if "pi" in ln]
    assert lines[1].index("+-") == lines[2].index("+-")


def test_counts_and_pane_target_appear():
    tasks = [_row(1, "pi", "processing", pane="agents.%5")]
    frame = dashboard.build_frame(tasks, [], {"pending": 3, "processing": 1,
                                              "completed": 0, "failed": 2},
                                  now=NOW)
    text = "\n".join(frame)
    assert "pending 3" in text and "failed 2" in text
    assert "agents.%5" in text


def test_messages_are_rendered_and_newlines_flattened():
    frame = dashboard.build_frame(
        [], [{"sender_id": "a", "recipient_id": "b", "content": "one\ntwo",
              "status": "unread"}],
        {"pending": 0, "processing": 0, "completed": 0, "failed": 0}, now=NOW)
    text = "\n".join(frame)
    assert "a->b" in text and "one two" in text


def test_lines_are_truncated_never_wrapped():
    tasks = [_row(1, "pi", "processing", payload=json.dumps({"model": "glm-5"}),
                  pane="agents.%5" * 20)]
    frame = dashboard.build_frame(tasks, [], {"pending": 0, "processing": 1,
                                              "completed": 0, "failed": 0},
                                  width=40, now=NOW)
    assert all(len(line) <= 40 for line in frame), frame


def test_orphans_are_listed_not_swallowed():
    """A task whose parent is gone must still appear — the counts include it."""
    tasks = [_row(1, "pi", "completed"),
             _row(2, "pi", "processing", deps=[99]),
             _row(3, "pi", "pending", deps=[2])]
    frame = dashboard.build_frame(tasks, [], {"pending": 1, "processing": 1,
                                              "completed": 1, "failed": 0},
                                  now=NOW)
    text = "\n".join(frame)
    assert "(orphaned)" in text
    assert len([ln for ln in frame if "] pi" in ln]) == 3


def test_a_dependency_cycle_terminates():
    tasks = [_row(1, "pi", "pending", deps=[2]), _row(2, "pi", "pending", deps=[1])]
    frame = dashboard.build_frame(tasks, [], {"pending": 2, "processing": 0,
                                              "completed": 0, "failed": 0},
                                  now=NOW)
    assert len([ln for ln in frame if "] pi" in ln]) == 2


def test_malformed_depends_on_does_not_raise():
    tasks = [_row(1, "pi", "pending"), _row(2, "pi", "pending")]
    tasks[1]["depends_on"] = "7"  # a bare scalar, not a list
    frame = dashboard.build_frame(tasks, [], {"pending": 2, "processing": 0,
                                              "completed": 0, "failed": 0},
                                  now=NOW)
    assert len([ln for ln in frame if "] pi" in ln]) == 2


def test_the_p2p_feed_survives_a_short_terminal():
    lines = [f"row {i}" for i in range(40)] + ["", " P2P", "  a->b: hi"]
    fitted = dashboard._fit(lines, 10)
    assert len(fitted) == 10
    assert fitted[-1] == "  a->b: hi" and " P2P" in fitted
    assert " ..." in fitted


def test_a_stalled_running_node_is_flagged_idle():
    """A stalled agent and a working one are identical without this."""
    stalled = _row(1, "pi", "processing", started="2026-07-25T11:55:00.000Z",
                   progress="2026-07-25T11:56:00.000Z")
    busy = _row(2, "pi", "processing", started="2026-07-25T11:55:00.000Z",
                progress="2026-07-25T12:00:20.000Z")
    assert dashboard.idle(stalled, NOW) == "idle 4m30"
    assert dashboard.idle(busy, NOW) == ""          # 10s: under the threshold
    assert dashboard.idle(_row(3, "pi", "completed",
                              progress="2026-07-25T11:00:00.000Z"), NOW) == ""
    frame = dashboard.build_frame([stalled], [], _counts(processing=1), now=NOW)
    assert any("idle 4m30" in ln for ln in frame), frame


def test_a_worker_that_stopped_beating_is_marked_dead():
    live = {"agent_id": "worker-0", "status": "busy", "current_task_id": 7,
            "last_seen": "2026-07-25T12:00:25.000Z"}
    dead = {"agent_id": "worker-1", "status": "busy", "current_task_id": 8,
            "last_seen": "2026-07-25T11:58:00.000Z"}
    assert "DEAD" not in dashboard.worker_line(live, NOW)
    assert "5s ago" not in dashboard.worker_line(live, NOW)  # m'ss format
    assert dashboard.worker_line(live, NOW).endswith("0m05 ago")
    assert "DEAD 2m30" in dashboard.worker_line(dead, NOW)
    frame = dashboard.build_frame([], [], _counts(), now=NOW,
                                 workers=[live, dead])
    text = "\n".join(frame)
    assert " WORKERS" in text and "worker-1" in text and "DEAD" in text


def test_errors_section_shows_the_failure_reason():
    frame = dashboard.build_frame(
        [], [], _counts(failed=1), now=NOW,
        errors=[{"task_id": 9, "message": "verify failed (exit 1):\n2 failed"}])
    text = "\n".join(frame)
    assert " ERRORS" in text and "#9 verify failed (exit 1): 2 failed" in text


def test_empty_sections_are_omitted_not_rendered_blank():
    frame = dashboard.build_frame([], [], _counts(), now=NOW)
    text = "\n".join(frame)
    assert " WORKERS" not in text and " ERRORS" not in text
    assert " TASKS" in text and " P2P" in text


def test_the_header_names_the_repo_being_watched():
    frame = dashboard.build_frame([], [], _counts(), now=NOW, width=80,
                                 label="orchestrator")
    assert "silicorism orchestrator" in frame[0]
    assert "12:00:30" in frame[0] and len(frame[0]) <= 80


def test_line_key_colours_by_status():
    assert dashboard.line_key(" [FAIL] verify") == "fail"
    assert dashboard.line_key(" [run ] builder   kimi   1m02") == "run"
    assert dashboard.line_key(" [done] scout") == "done"
    assert dashboard.line_key(" [wait] fixer") == "wait"
    assert dashboard.line_key(" TASKS") == "head"
    assert dashboard.line_key(" P2P") == "head"
    assert dashboard.line_key(" pending 1   running 0") == ""
    assert dashboard.line_key("  (none)") == ""
    # a stalled run and a dead worker read as failures, because they are
    assert dashboard.line_key(" [run ] builder  3m01  idle 2m10") == "fail"
    assert dashboard.line_key("  worker-1  busy  task 8  DEAD 2m30") == "fail"


def test_fit_keeps_the_lower_sections_and_elides_the_tree():
    lines = ([" silicorism", " counts", "", " TASKS"]
             + [f" [run ] node{i}" for i in range(40)]
             + ["", " WORKERS", "  worker-0 busy", "", " P2P", "  a->b: hi"])
    fitted = dashboard._fit(lines, 12)
    assert len(fitted) == 12
    assert " ..." in fitted
    assert fitted[-1] == "  a->b: hi"
    assert " WORKERS" in fitted and "  worker-0 busy" in fitted
    assert fitted[0] == " silicorism"  # the counts header is never elided


def test_nodes_are_named_by_their_agent():
    """Four rows all reading 'pi' say nothing about which node is which."""
    tasks = [_row(1, "pi", "processing",
                  payload=json.dumps({"agent_id": "scout-taskboard"})),
             _row(2, "verify", "pending")]
    frame = dashboard.build_frame(tasks, [], {"pending": 1, "processing": 1,
                                              "completed": 0, "failed": 0},
                                  now=NOW)
    text = "\n".join(frame)
    assert "scout-taskboard" in text
    assert "verify" in text  # falls back to the task type


def test_loading_cli_by_file_makes_its_siblings_importable(tmp_path):
    """The installed console script imports cli from a file, not from CWD.

    `dashboard` was left out of the editable install's module map, so
    `silicorism dashboard` died with ModuleNotFoundError on every machine.
    This runs cli the way the entry point does, with the repo NOT on sys.path.
    """
    probe = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('cli', r'{REPO}/cli.py')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "sys.modules['cli'] = mod\n"
        "spec.loader.exec_module(mod)\n"
        "import dashboard\n"
        "print(dashboard.__file__)\n"
    )
    r = subprocess.run([sys.executable, "-I", "-c", probe], cwd=tmp_path,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(REPO / "dashboard.py"), r.stdout


def test_a_frame_off_a_real_db_needs_no_seeding(tmp_path):
    """A monitor pointed at an uninitialised DB used to die on 'no such table'."""
    path = tmp_path / "q.db"
    db.init_db(str(path))
    conn = db.connect(str(path))
    try:
        db.add_task(conn, "pi", json.dumps({"prompt": "x"}))
        lines = dashboard.frame(conn, width=100, label="probe")
    finally:
        conn.close()
    text = "\n".join(lines)
    assert "silicorism probe" in text and "[wait]" in text


def test_columns_stay_aligned_however_deep_the_tree():
    """Indented rows are padded to one gutter, so the columns are a grid and a
    tree deeper than _MAX_DEPTH stops walking off the right edge."""
    tasks = [_row(1, "pi", "completed")]
    for i in range(2, 12):
        tasks.append(_row(i, "pi", "pending", deps=[i - 1]))
    tasks.append(_row(99, "pi", "pending", deps=[2]))   # the fan-out
    frame = dashboard.build_frame(tasks, [], _counts(pending=11, completed=1),
                                 width=120, now=NOW)
    rows = [ln for ln in frame if "] pi" in ln]
    assert len(rows) == 12, rows
    assert len({ln.index("[") for ln in rows}) == 1, rows   # one status column
    assert len({ln.index("] pi") for ln in rows}) == 1, rows
    assert max(len(ln) for ln in rows) <= 120
    # 11 depths collapse onto at most _MAX_DEPTH indents instead of growing
    depths = {ln.index("+-") for ln in rows if "+-" in ln}
    assert len(depths) <= dashboard._MAX_DEPTH, depths
