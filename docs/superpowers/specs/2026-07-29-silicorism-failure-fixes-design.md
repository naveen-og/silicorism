# Silicorism failure fixes — design

Date: 2026-07-29
Source: `SILICORISM-FAILURES-2026-07-29.md` (11 items from a real 5-step DAG run)

Scope: the eight CONFIRMED items — F1, F2, F3, F4, F5, F6, F8, F10, F11.
F7 (model may not be honoured) and F9 (agents inherit global prompt plugins) are
SUSPECTED and get reproduction steps only, no code.

## The problem in one line

A node can report success while its own tests fail, and when a node wedges nothing
notices for an hour, because `busy` means "the Python worker has not crashed" and
nothing else.

## Mechanism 1 — worker-run test gate (F1)

Today `worker._run_native` calls `db.complete_task` (worker.py:151) on the pane's
exit code alone. That code reflects the agent process exiting, not the work being
correct. `autoexit.ts` exits 0 whenever the run settled without an error stop
reason, so an agent that ran nothing at all still completes its task.

**Change.** A `pi` node may carry `test_command`. `build_dag` writes it into the
payload. After the pane exits 0 and before `complete_task`, the worker runs it
through the existing `handlers.verify` (handlers.py:340), which shells out in the
task's cwd and raises on non-zero. The raise lands inside `_run_native`'s existing
`try`, so the established failure path handles it: pane marked FAILED, task failed
or requeued by `db.fail_task`.

The verify output is appended to the artifact, so the orchestrator and the next
node see the evidence, not a claim.

Deliberately not the artifact-regex option: grepping the artifact for a pass token
proves only that the model typed the token.

## Mechanism 2 — progress signal (F2)

`_stop_and_beat` (worker.py:108) beats `busy` on a 30s timer while `_run_native`
blocks. That is a liveness beat for the worker loop; it carries no information
about the agent.

**Change.** New `tasks.last_progress_at` column (via the existing `_MIGRATIONS`
tuple in db.py:116). On each beat the worker takes the newest mtime under the
task's cwd; when it advances, it stamps `last_progress_at`.

The walk skips `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `dist`,
`build`, `target` and dotted directories. `ponytail:` this is an O(tree) scan every
30s — acceptable for a repo-sized tree, replace with inotify if it ever shows up
in a profile.

**Why not the pane log.** `pipe-pane` records every repaint, so a pi TUI spinner
grows the log continuously while nothing happens — precisely the observed failure.
File mtime distinguishes drawing from working; log bytes do not.

`silicorism_get_status` gains `stalled`: one entry per processing task with its
`last_progress_at` and idle seconds, so a wedged run is machine-detectable.

## Mechanism 3 — stall timeout (F3)

`tmux.wait_for_exit`'s 3600s cap is a wall-clock ceiling, not a stall detector; a
node that hangs at minute 2 holds its worker until minute 60.

**Change.** The poll callback returns stop once idle time exceeds
`stall_timeout_s` (default 600s) and records the reason, so `_run_native` raises
`native agent stalled: no progress for 612s` instead of the generic timeout
message. `timeout_s` and `stall_timeout_s` are per-node fields carried in the
payload; the 3600s ceiling remains the default.

The stall reason must be distinguishable from the operator's SIGINT, which uses
the same stop channel: the callback reports which one fired.

## Mechanism 4 — pane and process hygiene (F4, F10)

`_mark_pane` only retitles. A timed-out node therefore leaks a live agent plus its
children (observed: `gopls`, `pyright-langserver` still resident).

**Change.**

1. On the stall/timeout path only — where the agent is still running — the worker
   trims the log, then `tmux kill-pane`. A plain non-zero exit keeps its pane:
   that process is already dead and the scrollback is the post-mortem.
2. `_launch_script` installs `trap 'trap - TERM; kill -TERM 0' HUP TERM INT`.
   tmux gives each pane its own process group, so signalling group 0 from the
   script reaches the agent's descendants. `trap - TERM` first prevents the
   handler re-entering itself.
3. Pane labels are prefixed with a short slug of the DB path, so a long-lived
   session shows which run a pane belongs to.
4. A successful pane is killed rather than left at DONE. Failed panes are kept.

## Mechanism 5 — recovery and wait honesty (F5, F6)

A task wedged in `processing` is not terminal, so `gc(tasks=true)` cannot prune it
and the row poisons every later `wait` verdict. `reap_stale` does not help: it
keys on a stale heartbeat, and the wedged worker was heartbeating.

**Change.**

- `db.fail_stuck(conn, older_than_s=300)` force-fails processing rows that are
  stale by heartbeat **or** by `last_progress_at`. Exposed as
  `silicorism_gc(stuck=true)`; `gc(tasks=true)` can then prune them.
- `silicorism_cancel_task(task_id)` fails one named task unconditionally and
  kills its pane through the existing `tasks.pane_target` column.
- `wait_for_settle` returns `timed_out` and `elapsed_s`. The tool description
  states the sanctioned next action on a timeout: inspect status, then either
  re-wait or cancel the stalled task.

## Model policy (F8)

`qwen3-coder-480b` is banned by standing operator instruction but is the default
in `handlers.DEFAULT_PI_MODEL`, `silicorism_tools.SIMPLE_MODEL`,
`DEFAULT_MODELS["builder"]`, the `ESCALATION` ladder, the MCP `INSTRUCTIONS` text
and `~/.claude/skills/silicorism/SKILL.md`.

**Change.** All of those move to `kimi-k2.5` (build/fix) and `glm-5`
(scout/reason). The escalation ladder becomes kimi-k2.5 → glm-5. The alias stays
in `MODEL_ALIASES` so an explicit request still resolves.

## Prompt hardening (F11)

`build_dag` appends a fixed deliverables block to every pi node prompt:

1. Paste the verbatim output of every command that proves the acceptance criteria.
2. State every value the prompt told you to choose, and why.
3. Never claim a command passed without its pasted output.

This is also the cheapest available defence against F9 (agents inheriting a
terse-output plugin), independent of whether F9 is ever confirmed.

## Testing

Every mechanism gets a test in the existing pytest suite, using the established
fakes (`@patch("worker.tmux")`, `FakeTmux` from tests/test_grid.py):

- pi task with `test_command: "false"` ends `failed`, not `completed`; with
  `"true"` it completes and the artifact carries the verify line.
- `last_progress_at` advances when a file is touched under cwd and not otherwise.
- A poll callback whose task has not progressed past `stall_timeout_s` stops the
  wait and raises with the stall reason.
- `kill_pane` is called on the stall path and not on a clean non-zero exit;
  the launch script text contains the trap.
- `fail_stuck` flips a heartbeat-stale and a progress-stale processing row and
  leaves a fresh one alone; `cancel_task` fails a named row.
- `wait_for_settle` on a busy queue returns `timed_out: true` with an elapsed
  figure; a settled queue returns `timed_out: false`.
- No built-in tier or default emits `qwen` in a payload.
- Every pi node payload built by `build_dag` contains the deliverables block.

## Not in scope

- **F7** — reproduce with a 2-node DAG using deliberately different models, each
  node logging its resolved model into the artifact, before touching resolution
  code.
- **F9** — run one node prompt twice, once with the operator's plugins disabled
  for the agent, and compare whether command output is pasted verbatim.
