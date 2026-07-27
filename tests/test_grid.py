"""Grid placement asserts on constructed tmux commands — no tmux server needed."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tmux_orchestrator as tmux  # noqa: E402


class FakeTmux:
    """Records tmux argv and answers the queries grid_pane makes."""

    def __init__(self, existing=()):
        self.calls = []
        self.windows = list(existing)      # window name -> pane list
        self.panes = {w: [] for w in self.windows}
        self._next = 0

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        argv = cmd[1:]  # drop "tmux"
        out = ""
        if argv[0] == "has-session":
            return self._result(0, "")
        if argv[0] == "list-windows":
            out = "\n".join(self.windows)
        elif argv[0] == "list-panes":
            win = argv[2].split(":", 1)[1]
            out = "\n".join(self.panes.get(win, []))
        elif argv[0] in ("new-window", "split-window"):
            win = (argv[argv.index("-n") + 1] if "-n" in argv
                   else argv[argv.index("-t") + 1].split(":", 1)[1])
            self._next += 1
            pane = f"%{self._next}"
            self.panes.setdefault(win, []).append(pane)
            if win not in self.windows:
                self.windows.append(win)
            out = pane
        elif argv[0] == "display-message":
            out = "builder RUNNING"
        return self._result(0, out)

    @staticmethod
    def _result(code, out):
        class R:
            returncode = code
            stdout = out
            stderr = ""
        return R()

    def flat(self):
        return [" ".join(c) for c in self.calls]


def _place(fake, n):
    """Place n agents through grid_pane, returning [(window, pane), ...]."""
    placed = []
    with patch("subprocess.run", side_effect=fake):
        for i in range(n):
            placed.append(tmux.grid_pane(i, f"agent-{i}", "/tmp/wt",
                                         "pi 'go'", f"/tmp/sent-{i}"))
    return placed


def test_first_four_agents_share_one_window():
    fake = FakeTmux()
    placed = _place(fake, 4)
    assert [w for w, _ in placed] == ["agents"] * 4
    assert len({p for _, p in placed}) == 4  # distinct pane ids


def test_fifth_agent_spills_to_a_second_window():
    fake = FakeTmux()
    placed = _place(fake, 5)
    assert [w for w, _ in placed] == ["agents"] * 4 + ["agents-2"]


def test_grid_uses_tiled_layout_and_pane_id_capture():
    fake = FakeTmux()
    _place(fake, 2)
    flat = fake.flat()
    assert any("-P -F #{pane_id}" in f for f in flat), flat
    assert any("select-layout" in f and "tiled" in f for f in flat), flat
    assert any("split-window" in f for f in flat), flat


def test_pane_options_and_title_target_the_pane_id():
    fake = FakeTmux()
    (_, pane), = _place(fake, 1)
    flat = fake.flat()
    assert any(f"set-option -p -t {pane} remain-on-exit on" in f for f in flat), flat
    assert any(f"set-option -p -t {pane} {tmux.LABEL_OPT}" in f for f in flat), flat


def test_the_agent_is_launched_from_a_script_not_typed_into_the_shell():
    """A prompt spanning lines would be replayed as Enter presses by send-keys,
    leaving the shell in quote continuation and the TUI never starting."""
    fake = FakeTmux()
    with patch("subprocess.run", side_effect=fake):
        tmux.grid_pane(1, "agent-1", "/w", "pi 'line one\nline two'",
                       "/tmp/sent-1.exit", logfile="/tmp/l.log")
    sent = [f for f in fake.flat() if "send-keys" in f]
    assert sent and "\n" not in sent[0], sent
    script = sent[0].split()[-2]
    body = open(script, encoding="utf-8").read()
    assert "line one\nline two" in body          # prompt survives verbatim
    assert "echo $? >" in body
    # never piped: a pipe costs the agent its tty and with it the whole TUI
    assert "| tee " not in body, body
    assert any("pipe-pane" in f and "/tmp/l.log" in f for f in fake.flat())
    os.remove(script)


def test_mark_pane_done_swaps_the_status_marker():
    fake = FakeTmux()
    with patch("subprocess.run", side_effect=fake):
        tmux.mark_pane_done("%3", failed=True)
    title = [f for f in fake.flat() if tmux.LABEL_OPT in f][-1]
    assert "%3" in title and tmux.FAILED in title, title


def test_missing_pane_id_raises():
    fake = FakeTmux()

    def no_pane(cmd, **kw):
        r = fake(cmd, **kw)
        if cmd[1] in ("new-window", "split-window"):
            r.stdout = ""
        return r

    with patch("subprocess.run", side_effect=no_pane):
        try:
            tmux.grid_pane(1, "x", "/tmp", "pi 'go'", "/tmp/s")
        except RuntimeError:
            return
    raise AssertionError("expected RuntimeError when tmux returns no pane id")


def test_unrelated_agents_prefixed_window_is_not_reused():
    """A user's own 'agents-notes' window (however empty) must never be
    mistaken for a grid spill window."""
    fake = FakeTmux(existing=["agents-notes"])
    (window, _), = _place(fake, 1)
    assert window == "agents"
    split_targets = [f for f in fake.flat() if "split-window" in f]
    assert not any("agents-notes" in f for f in split_targets), split_targets


def test_next_spill_window_skips_gap_instead_of_colliding():
    """agents and agents-3 exist (agents-2 was closed) and are both full;
    the next spill must be agents-4, never a duplicate agents-3."""
    fake = FakeTmux(existing=["agents", "agents-3"])
    fake.panes["agents"] = [f"%{i}" for i in range(tmux.GRID_MAX)]
    fake.panes["agents-3"] = [f"%{i + 100}" for i in range(tmux.GRID_MAX)]
    (window, _), = _place(fake, 1)
    assert window == "agents-4", window


def test_worker_falls_back_to_a_window_when_the_grid_fails(tmp_path):
    """tmux breakage must never fail a task — the pane is a viewport, not a dep."""
    import db
    import worker

    dbp = str(tmp_path / "w.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", "{}")
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()

    with patch.object(worker.tmux, "grid_pane", side_effect=RuntimeError("no server")), \
         patch.object(worker.tmux, "run_task_in_pane",
                      return_value="task-1-pi") as legacy:
        window, pane = worker._place_pane(conn, task, "pi 'go'", "/tmp/s", "/tmp/l")

    assert pane is None and window == "task-1-pi"
    assert legacy.call_count == 1
    stored = conn.execute("SELECT pane_target FROM tasks WHERE id=?",
                          (tid,)).fetchone()["pane_target"]
    assert stored == "task-1-pi"  # the window, since there is no pane
    conn.close()


def test_a_failed_pane_target_write_does_not_launch_a_second_agent(tmp_path):
    """The agent is already live once grid_pane returns — never re-launch it."""
    import db
    import worker

    dbp = str(tmp_path / "w3.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", "{}")
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()

    with patch.object(worker.tmux, "grid_pane", return_value=("agents", "%3")), \
         patch.object(worker.db, "set_pane_target",
                      side_effect=RuntimeError("database is locked")), \
         patch.object(worker.tmux, "run_task_in_pane") as legacy:
        window, pane = worker._place_pane(conn, task, "pi 'go'", "/tmp/s", "/tmp/l")

    assert (window, pane) == ("agents", "%3")
    assert not legacy.called
    conn.close()


def test_worker_records_the_pane_target(tmp_path):
    import db
    import worker

    dbp = str(tmp_path / "w2.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", '{"agent_id": "builder-x"}')
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()

    with patch.object(worker.tmux, "grid_pane", return_value=("agents", "%7")) as g:
        worker._place_pane(conn, task, "pi 'go'", "/tmp/s", "/tmp/l")

    stored = conn.execute("SELECT pane_target FROM tasks WHERE id=?",
                          (tid,)).fetchone()["pane_target"]
    assert stored == "agents.%7"
    # the pane's label is the agent id, so the grid reads as a roster of agents
    assert g.call_args[0][1] == "builder-x"
    conn.close()


def test_pane_placement_is_serialised_across_processes():
    """Four workers racing must not each create their own 'agents' window."""
    import threading

    fake = FakeTmux()
    order = []
    real_next = tmux._next_grid_window

    def slow_next(session):
        order.append("enter")
        time.sleep(0.02)  # widen the window a racing caller would slip through
        out = real_next(session)
        order.append("exit")
        return out

    with patch("subprocess.run", side_effect=fake), \
         patch.object(tmux, "_next_grid_window", side_effect=slow_next):
        threads = [threading.Thread(target=tmux.grid_pane,
                                    args=(i, f"a{i}", "/w", "pi 'go'",
                                          f"/tmp/s{i}.exit"),
                                    kwargs={"logfile": "/tmp/l.log"})
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

    # strictly alternating enter/exit means no two selections overlapped
    assert order == ["enter", "exit"] * 4, order
    for i in range(4):
        os.remove(os.path.join(tmux.SENTINEL_DIR, f"task-{i}.sh"))


def test_a_user_window_named_my_agents_is_not_seen_as_the_grid():
    """list-windows output is line-oriented; splitting on spaces invents windows."""
    fake = FakeTmux(existing=["dashboard", "my agents", "notes"])
    assert tmux._grid_windows("s") == []


def test_log_tails_are_stripped_of_escape_codes(tmp_path):
    """tmux logs what the pane drew, so the raw file is a repaint stream."""
    log = tmp_path / "t.log"
    log.write_text("\x1b[38;2;108;113;196m─\x1b[39m CONTEXT.md written\r\n")
    assert tmux.read_log_tail(str(log)) == "─ CONTEXT.md written"


def test_trim_log_keeps_the_tail(tmp_path):
    log = tmp_path / "big.log"
    log.write_text("x" * 5000 + "END")
    tmux.trim_log(str(log), max_bytes=1000)
    body = log.read_text()
    assert len(body) == 1000 and body.endswith("END")


def test_the_script_directory_is_private_to_this_user():
    """The shell executes these scripts; a shared /tmp dir is a foothold."""
    import stat
    d = tmux._sentinel_dir()
    assert str(os.getuid()) in d
    assert stat.S_IMODE(os.stat(d).st_mode) & 0o077 == 0
