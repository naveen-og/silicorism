"""Grid placement asserts on constructed tmux commands — no tmux server needed."""

from __future__ import annotations

import sys
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
    assert any(f"select-pane -t {pane} -T" in f for f in flat), flat


def test_command_wrapper_preserves_exit_code_and_tee():
    fake = FakeTmux()
    _place(fake, 1)
    sent = [f for f in fake.flat() if "send-keys" in f]
    assert sent and "echo $? >" in sent[0] and "| tee " in sent[0], sent


def test_mark_pane_done_swaps_the_status_marker():
    fake = FakeTmux()
    with patch("subprocess.run", side_effect=fake):
        tmux.mark_pane_done("%3", failed=True)
    title = [f for f in fake.flat() if "select-pane" in f][-1]
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
    assert legacy.called
    conn.close()


def test_worker_records_the_pane_target(tmp_path):
    import db
    import worker

    dbp = str(tmp_path / "w2.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    tid = db.add_task(conn, "pi", '{"agent_id": "builder-x"}')
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()

    with patch.object(worker.tmux, "grid_pane", return_value=("agents", "%7")):
        worker._place_pane(conn, task, "pi 'go'", "/tmp/s", "/tmp/l")

    stored = conn.execute("SELECT pane_target FROM tasks WHERE id=?",
                          (tid,)).fetchone()["pane_target"]
    assert stored == "agents.%7"
    conn.close()
