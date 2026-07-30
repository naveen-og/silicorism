"""The frame builder is pure, so the whole dashboard — layout, colours and the
fitting — is testable without a terminal. curses only paints what it returns."""

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
G = dashboard.GLYPHS_ASCII      # deterministic and greppable in assertions


def _row(tid, ttype, status, *, deps=None, payload=None, started=None,
         updated=None, pane=None, progress=None, retries=0):
    return {"id": tid, "task_type": ttype, "status": status,
            "depends_on": json.dumps(deps) if deps else None,
            "payload": payload, "started_at": started, "updated_at": updated,
            "pane_target": pane, "agent_id": None, "last_progress_at": progress,
            "retry_count": retries}


def _counts(**kw):
    return {"pending": 0, "processing": 0, "completed": 0, "failed": 0, **kw}


def _hb(agent, status, task, seen):
    return {"agent_id": agent, "status": status, "current_task_id": task,
            "last_seen": seen}


def _frame(tasks=(), messages=(), counts=None, **kw):
    kw.setdefault("g", G)
    return dashboard.build_frame(list(tasks), list(messages),
                                 counts or _counts(), now=NOW, **kw)


def _text(lines) -> str:
    return "\n".join(dashboard.flatten(lines))


def _task_rows(lines):
    return [ln for ln in lines if ln.kind.startswith("task:")]


def _keys(line, needle):
    """Colour keys of the spans in `line` whose text contains `needle`."""
    return [k for t, k in line.spans if needle in t]


# --- formatting -------------------------------------------------------------

def test_short_model_reverses_the_alias_table():
    payload = json.dumps(
        {"model": "bedrock-mantle/qwen.qwen3-coder-480b-a35b-instruct"})
    assert dashboard.short_model(payload) == "qwen3-coder-480b"


def test_short_model_survives_garbage():
    assert dashboard.short_model("not json") == "-"
    assert dashboard.short_model(None) == "-"
    assert dashboard.short_model("{}") == "-"


def test_elapsed_switches_to_hours_so_a_long_node_stays_readable():
    running = _row(1, "pi", "processing", started="2026-07-25T12:00:00.000Z")
    assert dashboard.elapsed(running, NOW) == "0m30"
    done = _row(2, "pi", "completed", started="2026-07-25T12:00:00.000Z",
                updated="2026-07-25T12:01:02.000Z")
    assert dashboard.elapsed(done, NOW) == "1m02"
    slow = _row(3, "pi", "processing", started="2026-07-25T10:44:00.000Z")
    assert dashboard.elapsed(slow, NOW) == "1h16m"
    assert dashboard.elapsed(_row(4, "pi", "pending"), NOW) == "-"


def test_run_time_spans_the_whole_dag():
    tasks = [_row(1, "pi", "completed", started="2026-07-25T11:50:00.000Z",
                  updated="2026-07-25T11:52:00.000Z"),
             _row(2, "pi", "processing", started="2026-07-25T11:52:00.000Z")]
    assert dashboard.run_time(tasks, NOW) == "10m30"      # still running
    finished = [dict(t, status="completed",
                     updated_at="2026-07-25T11:55:10.000Z") for t in tasks]
    assert dashboard.run_time(finished, NOW) == "5m10"    # start to last update
    assert dashboard.run_time([_row(9, "pi", "pending")], NOW) == ""


def test_trunc_marks_the_cut():
    assert dashboard._trunc("builder-taskboard", 10, G) == "builder-.."
    assert dashboard._trunc("short", 10, G) == "short"
    assert len(dashboard._trunc("x" * 40, 12, G)) == 12


# --- the DAG table ----------------------------------------------------------

def test_a_fanin_node_renders_below_everything_it_waits_for():
    """Parenting each node under its FIRST dependency drew the verify gate
    above the builders it was waiting for. Depth order is the run order."""
    tasks = [_row(1, "worktree_create", "completed"),
             _row(2, "pi", "completed", deps=[1],
                  payload=json.dumps({"agent_id": "scout"})),
             _row(3, "pi", "completed", deps=[2],
                  payload=json.dumps({"agent_id": "build-a"})),
             _row(4, "pi", "processing", deps=[2],
                  payload=json.dumps({"agent_id": "build-b"})),
             _row(5, "pi", "pending", deps=[3, 4],
                  payload=json.dumps({"agent_id": "fixer"})),
             _row(6, "verify", "pending", deps=[5])]
    names = [ln.spans[3][0].strip()
             for ln in _task_rows(_frame(tasks, counts=_counts(pending=3,
                                                              processing=1,
                                                              completed=2)))]
    assert names == ["worktree_create", "scout", "build-a", "build-b",
                     "fixer", "verify"], names


def test_the_table_is_a_grid_and_never_exceeds_the_width():
    tasks = [_row(1, "pi", "completed")]
    for i in range(2, 12):
        tasks.append(_row(i, "pi", "pending", deps=[i - 1]))
    tasks.append(_row(99, "pi", "pending", deps=[2]))       # the fan-out
    rows = dashboard.flatten(_task_rows(_frame(
        tasks, counts=_counts(pending=11, completed=1), width=120)))
    assert len(rows) == 12
    assert len({ln.index("#") for ln in rows}) == 1          # one id column
    assert max(len(ln) for ln in rows) <= 120
    # depth costs no indentation at all, so a deep chain reads like a shallow one
    assert len({len(ln) - len(ln.lstrip()) for ln in rows}) == 1


def test_every_row_carries_its_task_id():
    """You need the id to cancel a node or tail its logs."""
    text = _text(_frame([_row(7, "pi", "processing")],
                        counts=_counts(processing=1)))
    assert "#7" in text


def test_nodes_are_named_by_their_agent():
    """Four rows all reading 'pi' say nothing about which node is which."""
    tasks = [_row(1, "pi", "processing",
                  payload=json.dumps({"agent_id": "scout-taskboard"})),
             _row(2, "verify", "pending")]
    text = _text(_frame(tasks, counts=_counts(pending=1, processing=1)))
    assert "scout-taskboard" in text
    assert "verify" in text                     # falls back to the task type


def test_running_nodes_spin_so_a_frozen_monitor_is_visible():
    task = [_row(1, "pi", "processing")]
    seen = {dashboard.flatten(_task_rows(_frame(
        task, counts=_counts(processing=1), tick=t)))[0][:6] for t in range(4)}
    assert len(seen) == 4, seen                 # a different frame each tick
    still = dashboard.flatten(_task_rows(_frame([_row(1, "pi", "completed")],
                                                counts=_counts(completed=1))))
    assert G["done"] in still[0]


def test_pane_target_and_retries_are_shown():
    tasks = [_row(1, "pi", "processing", pane="agents.%5", retries=2)]
    lines = _frame(tasks, counts=_counts(processing=1))
    text = _text(lines)
    assert "agents.%5" in text and "retry 2" in text
    assert _keys(_task_rows(lines)[0], "retry 2") == ["fail"]


def test_a_task_whose_dependency_is_gone_still_appears():
    """The counts include it, so the table must too."""
    tasks = [_row(1, "pi", "completed"),
             _row(2, "pi", "processing", deps=[99]),
             _row(3, "pi", "pending", deps=[2])]
    lines = _frame(tasks, counts=_counts(pending=1, processing=1, completed=1))
    assert [ln.spans[2][0].strip() for ln in _task_rows(lines)] == ["#1", "#2", "#3"]


def test_a_dependency_cycle_terminates():
    tasks = [_row(1, "pi", "pending", deps=[2]), _row(2, "pi", "pending", deps=[1])]
    assert len(_task_rows(_frame(tasks, counts=_counts(pending=2)))) == 2


def test_malformed_depends_on_does_not_raise():
    tasks = [_row(1, "pi", "pending"), _row(2, "pi", "pending")]
    tasks[1]["depends_on"] = "7"        # a bare scalar, not a list
    assert len(_task_rows(_frame(tasks, counts=_counts(pending=2)))) == 2


def test_an_empty_queue_says_so_instead_of_rendering_nothing():
    assert "(no tasks yet)" in _text(_frame())


# --- progress, workers, errors, P2P -----------------------------------------

def test_the_progress_bar_stacks_every_status_and_fills_its_width():
    bar = dashboard.progress_bar(
        _counts(completed=5, failed=1, processing=2, pending=2), 22, G)
    assert dashboard.span_width(bar) == 22
    assert dashboard.span_width(bar[1:-1]) == 20            # minus the two caps
    # pending is drawn as unfilled grey, not a fourth colour that means something
    assert [k for _, k in bar] == ["dim", "done", "fail", "run", "dim", "dim"]
    assert bar[4][0] == G["bar_empty"] * len(bar[4][0])


def test_one_failure_in_a_hundred_nodes_still_shows_a_red_cell():
    bar = dashboard.progress_bar(_counts(completed=99, failed=1), 22, G)
    assert dashboard.span_width(bar) == 22
    red = [t for t, k in bar if k == "fail"]
    assert red and len(red[0]) >= 1


def test_the_progress_bar_survives_an_empty_queue():
    bar = dashboard.progress_bar(_counts(), 12, G)
    assert dashboard.span_width(bar) == 12
    assert {k for _, k in bar} == {"dim"}


def test_the_header_names_the_repo_and_totals_the_run():
    lines = _frame([_row(1, "pi", "processing",
                         started="2026-07-25T11:58:00.000Z")],
                   counts=_counts(processing=1, completed=3, failed=1),
                   width=100, label="orchestrator")
    head = _text(lines).splitlines()
    assert "silicorism" in head[0] and "orchestrator" in head[0]
    assert "12:00:30" in head[0] and "run 2m30" in head[0]
    assert "5 nodes" in head[1] and "3 done" in head[1] and "1 fail" in head[1]
    assert all(len(ln) <= 100 for ln in head)


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
    lines = _frame([stalled], counts=_counts(processing=1))
    assert "idle 4m30" in _text(lines)
    assert _keys(_task_rows(lines)[0], "idle") == ["fail"]


def test_a_worker_that_stopped_beating_is_marked_dead():
    live = _hb("worker-0", "busy", 7, "2026-07-25T12:00:25.000Z")
    dead = _hb("worker-1", "busy", 8, "2026-07-25T11:58:00.000Z")
    assert "0m05 ago" in "".join(t for t, _ in dashboard._worker_spans(live, NOW, G))
    assert "DEAD" not in "".join(t for t, _ in dashboard._worker_spans(live, NOW, G))
    dspans = dashboard._worker_spans(dead, NOW, G)
    assert "DEAD 2m30" in "".join(t for t, _ in dspans)
    assert G["dead"] in [t for t, k in dspans if k == "fail"]
    text = _text(_frame(workers=[live, dead]))
    assert "WORKERS" in text and "worker-1" in text and "DEAD" in text
    assert "#7" in text                            # the task it is holding


def test_errors_section_shows_the_failure_reason():
    lines = _frame(counts=_counts(failed=1),
                   errors=[{"task_id": 9,
                            "message": "verify failed (exit 1):\n2 failed"}])
    text = _text(lines)
    assert "ERRORS" in text and "#9 verify failed (exit 1): 2 failed" in text
    assert [ln for ln in lines if ln.kind == "error"][0].spans[1][1] == "fail"


def test_messages_are_rendered_and_newlines_flattened():
    text = _text(_frame(messages=[{"sender_id": "a", "recipient_id": "b",
                                   "content": "one\ntwo", "status": "unread"}]))
    assert f"a {G['arrow']}  b" in text and "one two" in text


def test_empty_sections_are_omitted_not_rendered_blank():
    text = _text(_frame())
    for absent in ("WORKERS", "ERRORS", "P2P"):
        assert absent not in text
    assert "TASKS" in text and "q quit" in text


def test_no_line_ever_exceeds_the_width():
    tasks = [_row(1, "pi", "processing", payload=json.dumps({"model": "glm-5"}),
                  pane="agents.%5" * 20, progress="2026-07-25T11:00:00.000Z")]
    for width in (40, 60, 100):
        lines = _frame(tasks, counts=_counts(processing=1),
                       messages=[{"sender_id": "a" * 30, "recipient_id": "b" * 30,
                                  "content": "c" * 200, "status": "unread"}],
                       workers=[_hb("w" * 30, "busy", 1, "bad-ts")],
                       errors=[{"task_id": 1, "message": "e" * 300}],
                       width=width, label="lbl")
        assert all(len(ln) <= width for ln in dashboard.flatten(lines)), width


# --- fitting ----------------------------------------------------------------

def test_fitting_collapses_finished_nodes_before_dropping_live_ones():
    tasks = [_row(i, "pi", "completed") for i in range(1, 21)]
    tasks += [_row(21, "pi", "processing", progress="2026-07-25T11:00:00.000Z"),
              _row(22, "pi", "failed"), _row(23, "pi", "pending")]
    lines = _frame(tasks, counts=_counts(completed=20, processing=1, failed=1,
                                         pending=1),
                   workers=[_hb("worker-0", "busy", 21,
                                "2026-07-25T12:00:28.000Z")],
                   errors=[{"task_id": 22, "message": "boom"}])
    fitted = dashboard.fit(lines, 16, G)
    text = "\n".join(dashboard.flatten(fitted))
    assert len(fitted) <= 16
    assert "20 done" in text                    # the finished run, folded
    assert "idle 1h00m" in text                 # the stalled node survives
    assert "boom" in text and "worker-0" in text and "q quit" in text


def test_fitting_keeps_the_newest_nodes_when_even_that_is_not_enough():
    tasks = [_row(i, "pi", "failed") for i in range(1, 31)]
    lines = _frame(tasks, counts=_counts(failed=30))
    fitted = dashboard.fit(lines, 12, G)
    text = "\n".join(dashboard.flatten(fitted))
    assert len(fitted) == 12
    assert "earlier nodes" in text
    assert "#30" in text and "#1 " not in text  # the tail, not the head
    assert "q quit" in text


def test_a_frame_that_already_fits_is_untouched():
    lines = _frame([_row(1, "pi", "pending")], counts=_counts(pending=1))
    assert dashboard.fit(lines, 40, G) == lines


# --- wiring -----------------------------------------------------------------

def test_glyphs_fall_back_to_ascii_when_asked(monkeypatch):
    monkeypatch.setenv("SILICORISM_ASCII", "1")
    assert dashboard.glyphs() is dashboard.GLYPHS_ASCII
    monkeypatch.delenv("SILICORISM_ASCII")
    assert dashboard.glyphs(unicode_ok=True) is dashboard.GLYPHS_UNICODE


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
        lines = dashboard.frame(conn, width=100, label="probe", g=G)
    finally:
        conn.close()
    text = "\n".join(dashboard.flatten(lines))
    assert "silicorism  probe" in text and "1 nodes" in text
    assert G["wait"] in text
