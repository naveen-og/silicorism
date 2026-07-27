"""The frame builder is a pure function, so the whole dashboard is testable
without a terminal. curses only paints the strings it returns."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard  # noqa: E402

NOW = datetime(2026, 7, 25, 12, 0, 30, tzinfo=timezone.utc)


def _row(tid, ttype, status, *, deps=None, payload=None, started=None,
         updated=None, pane=None):
    return {"id": tid, "task_type": ttype, "status": status,
            "depends_on": json.dumps(deps) if deps else None,
            "payload": payload, "started_at": started, "updated_at": updated,
            "pane_target": pane, "agent_id": None}


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


def test_tree_indents_children_under_their_first_dependency():
    tasks = [_row(1, "worktree_create", "completed"),
             _row(2, "pi", "completed", deps=[1]),
             _row(3, "pi", "processing", deps=[2])]
    frame = dashboard.build_frame(tasks, [], {"pending": 0, "processing": 1,
                                              "completed": 2, "failed": 0},
                                  now=NOW)
    body = [ln for ln in frame if "pi" in ln or "worktree" in ln]
    assert body[0].startswith(" ") and not body[0].lstrip().startswith("+-")
    assert body[1].lstrip().startswith("+-")
    assert body[2].index("+-") > body[1].index("+-")


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
