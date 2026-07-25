# Grid TUI, adaptive routing, and token economy

Date: 2026-07-25
Status: approved, ready for implementation planning

## Goal

Match the output quality of Opus 5 at xhigh working for hours, at a fraction of the
Claude token cost, by keeping Claude in the planning and adjudication role and giving
the token-heavy execution to the OSS trio (qwen3-coder-480b, kimi-k2.5, glm-5) running
in pi panes.

Three things serve that goal, and this spec covers all three:

1. **Observability** — see every agent working at once, so a bad run is caught in
   seconds instead of after the DAG finishes.
2. **Right-sizing** — a trivial task should not spawn three agents and a worktree.
3. **Token economy** — Claude should spend turns on planning and on failures, never on
   watching a queue drain.

Non-goal: replacing any OSS model with a Claude model at execution time. No code path
added here may fall back to a Claude model.

## Current behaviour and why it falls short

| Area | Today | Problem |
|---|---|---|
| Pane layout | `run_task_in_pane` calls `new-window` per task (`tmux_orchestrator.py:104`) | Attach shows one agent at a time; parallel work is invisible |
| Dashboard | `\033[2J\033[H` reprint loop (`cli.py:173`) | Flickers, no DAG view, no per-node model or timing |
| DAG shape | `build_pipeline` always emits scout + builder + fixer (`silicorism_tools.py:38-58`) | Three agents and a worktree for a task that needs one agent |
| Orchestrator loop | MCP instructions say poll `silicorism_get_status` | Every poll is a full Claude turn that learns "still running" |

## Verified environment

All tmux mechanics below were probed on the target machine before being specified.

```
tmux 3.7b
split-window -P -F '#{pane_id}'      -> %5      (stable for the pane's life)
select-layout <win> tiled            -> OK
set-option -p -t %id remain-on-exit on -> OK    (pane scope, not just window)
select-pane -t %id -T "scout OK"     -> pane_title set
set-option -w pane-border-status top -> OK
```

`db.py:108` already carries an additive `_MIGRATIONS` tuple applied on every
`init_db`, so new columns are a one-line idempotent add.

## Section A — agents grid

Agents are tiled into shared `agents*` windows instead of one window each.

`grid_pane(task_id, label, cwd, command, sentinel, logfile) -> (window, pane_id)`
replaces the per-task `new-window` call:

1. Choose the first `agents*` window holding fewer than `GRID_MAX` panes; if all are
   full, create the next spill window (`agents-2`, `agents-3`, ...). `GRID_MAX` is a
   module constant defaulting to 4, overridable via `SILICORISM_GRID_MAX`.
2. New window: `new-window -n <win> -c <cwd> -P -F '#{pane_id}'`.
   Existing window: `split-window -t <win> -c <cwd> -P -F '#{pane_id}'`.
3. `set-option -p -t <pane_id> remain-on-exit on` — a finished pane freezes with its
   final TUI visible rather than closing.
4. `select-pane -t <pane_id> -T "<label> RUNNING"`, then
   `select-layout -t <win> tiled`.
5. Send the existing wrapper unchanged:
   `{ cmd; echo $? > tmp; } 2>&1 | tee log; mv tmp fin`.

The exit-code and artifact contract is untouched, so `wait_for_exit`, the sentinel
protocol, and the log-tail artifact hand-off all keep working as they do today.

**Pane ids, not indices.** `%5` is stable for the pane's life; `1.2` shifts whenever a
sibling closes. Every subsequent call (title update, kill) targets the `%id`.

**Lifecycle.** Finished panes stay, retitled `DONE` or `FAILED`; the window re-tiles
when the next agent spawns. Scrollback for a completed agent remains reachable.

**Status markers** ride the pane border: running / done / failed, set on transition,
with `pane-border-status top` and a `pane-border-format` that renders
`#{pane_title}`.

**Window map.** `0:dashboard`, `1:agents`, `2:agents-2`, ... The orchestrator agent
keeps its existing pane in window 0 alongside the dashboard.

**Persistence.** New column `tasks.pane_target TEXT` stores `"1:agents.%5"`. It is
display metadata only — no control flow reads it.

**Degradation.** If any tmux call fails (no server, session killed mid-run),
`grid_pane` falls back to the current `new-window` path and the task still runs. The
pane is a viewport, never a dependency of execution.

**Rejected:** spawning a window per task and then `join-pane`-ing it into the grid.
Two tmux calls per agent plus a visible flicker, for no gain over splitting directly.

## Section B — curses dashboard

New module `dashboard.py` (stdlib `curses`). `cli.py`'s `cmd_dashboard` becomes a thin
call into it; the `silicorism dashboard` entry point is unchanged.

**Boundary.** `dashboard.py` receives a DB connection and owns drawing only. It never
writes to the DB and never shells out to tmux. The frame builder is a pure function
from task rows to a list of strings, so it is tested without a terminal; `curses` only
paints those strings.

Layout, redrawn every `--interval` (default 1s):

```
-- silicorism -- feat/auth ------------------ 12:04 --
 pending 2   running 2   done 3   failed 0

 [done] worktree                   0.4s
 +- [done] scout      glm-5        1m02   1:agents.%4
    +- [run]  builder qwen3-coder  0m48   1:agents.%5
       +- [wait] fixer kimi-k2.5    -
          +- [wait] verify pytest -q -

 P2P  builder->scout: where do routes live?
      scout->builder: api/routes.py:40
```

**Tree** is derived from the `depends_on` JSON array each task already stores: every
node is indented under its first dependency. Fan-out siblings render at equal depth.
Cycles are impossible because `_toposort` (`silicorism_tools.py:78`) rejects them at
submit time.

**Two additive migrations**, via the existing `_MIGRATIONS` mechanism:

- `pane_target TEXT` (Section A).
- `started_at TEXT`, stamped in `claim_task`. Without it there is no honest duration
  for a finished task — `updated_at` is the end time, not the start.

**Model column** parses the task payload and reverses `handlers.MODEL_ALIASES`, so
`bedrock-mantle/qwen.qwen3-coder-480b-a35b-instruct` displays as `qwen3-coder-480b`.
An unparseable payload renders `-` and never raises.

**Resize and failure.** `KEY_RESIZE` re-derives the frame. Lines wider than the
terminal are truncated, never wrapped. A terminal too small or a dumb `TERM` falls back
to the current plain print loop rather than dying — a monitor must not take the session
down with it.

**Keys:** `q` quit, `r` force redraw. Nothing else; tmux's own bindings handle panes.

## Section C — complexity tiers

`build_pipeline` keeps its current signature and gains `complexity="standard"`, so
every existing caller and test is unaffected. Three shape builders sit behind it.

```
simple    solo(qwen3-coder-480b)                    [+ verify iff test_command given]
          no worktree - runs in cwd

standard  worktree -> scout -> builder -> fixer -> verify [-> merge] -> cleanup
          unchanged from today

complex   parallel fan-out, see below
```

`simple` skips `worktree_create` and `worktree_cleanup` entirely and runs in
`os.getcwd()`, which `build_dag` already supports via its `name=None` path
(`silicorism_tools.py:99-107`). This is deliberate: a fresh scratch project has no git
repo for a worktree and no tests for a verify gate, and the tier must work there.

### complex: parallel builders, one worktree each

```
worktree-a (branch <name>-a)     worktree-b (branch <name>-b)    both from base
  +- scout              runs in wt-a; partitions work into file-disjoint slices A and B
       +- builder-a     wt-a, slice A   ) concurrent
       +- builder-b     wt-b, slice B   )
            +- integrate      deterministic handler
                 +- integrator  pi agent (kimi-k2.5), resolves conflicts
                      +- fixer -> verify [-> merge into base] -> cleanup-a, cleanup-b
```

**Slice communication.** The scout's prompt asks it to partition the work into two
file-disjoint slices. Each builder receives that text through the existing artifact
hand-off (`db.dep_artifacts` -> `_with_context`), so builder-b needs no file from wt-a.
This is the same mechanism the current builder already uses to read the scout.

**New handler `worktree_integrate`.** `worktree_merge` cannot be reused: it runs
`git switch <base>` in the main repo (`handlers.py:285`, no `cwd`), and git refuses to
check out a branch that is already checked out in another worktree. `worktree_integrate`
instead commits wt-b, then runs `git merge --no-ff <name>-b` with `cwd=worktree-a`,
where branch-a is already checked out and no switch is needed.

Its contract differs deliberately from `worktree_merge`: **on conflict it does not
abort.** It leaves the conflicted tree in place and returns the conflicted file list as
its artifact, so the next node has something to fix. `worktree_merge` keeps its
abort-and-quarantine behaviour unchanged — no branch is added to that path.

**`integrator` agent** reads that artifact. Clean merge -> its prompt instructs a no-op.
Conflicts -> it resolves the markers and commits. The `verify` gate downstream decides
whether the integration was correct; the agent is never trusted on its own claim.

**Cleanup ordering.** Both cleanups run last, after the base merge. Branch-b's commits
are reachable from branch-a once integrated, so earlier deletion would be safe, but
ordering it last keeps a failed run's post-mortem intact — a quarantined worktree is
useless if its branch is already gone.

**Known risk, accepted.** Both builders branch from the same base, so a sloppy scout
partition means overlapping files and real conflict resolution on every run. The
`verify` gate is the backstop. `simple` and `standard` are unaffected.

### MCP surface

`silicorism_plan_and_submit` gains `complexity: "simple" | "standard" | "complex"`. The
server `instructions` block gains one rule: classify the request into a tier and pass
it, or pass a full `nodes` DAG when no tier fits. An unknown or absent value falls back
to `standard` rather than erroring — a submit must not fail over a typo in a hint.

**Models.** `simple` pins `qwen3-coder-480b`. `standard` and `complex` keep the
existing role trio (`DEFAULT_MODELS`, `silicorism_tools.py:20-24`). All remain
overridable per node.

## Section D — token economy

**Blocking wait replaces the poll loop.** New MCP tool
`silicorism_wait(timeout_s, db)` blocks server-side until every task in the DB is in a
terminal state (`completed` or `failed`), or until any task fails, whichever comes
first — the same whole-queue scope `silicorism_get_status` already reports on. It then
returns once. Default 600s, hard cap 3600s, so a hung agent
cannot wedge the call; on timeout it returns a "still running" digest and the
orchestrator decides whether to wait again. One Claude turn per DAG instead of one per
poll. The MCP `instructions` block is updated to call `silicorism_wait` instead of
polling `silicorism_get_status`.

**Digests, not dumps.** `silicorism_wait` and `silicorism_get_status` return counts,
the terminal verdict, and — for failed nodes only — the last error plus a truncated
artifact. Successful nodes' artifacts never enter Claude's context; they flow
agent-to-agent inside Python through `dep_artifacts`, where they cost nothing.

**Front-loaded reasoning.** Tier selection and per-node prompts are where Opus-grade
thinking is spent, once, at plan time: each node's prompt carries explicit acceptance
criteria and file-level scope. Weak models fail from underspecified instructions far
more often than from weak weights, so one thorough planning turn is what buys quality
parity.

**Deterministic gates keep Claude out.** `verify` re-runs tests with no model involved,
and the fixer loop retries locally through the OSS escalation ladder. Claude is
re-engaged only when the ladder is exhausted.

**Escalation stays OSS-only.** The ladder (`handlers.py:46-50`) is three bedrock OSS
models. The comment above it (`handlers.py:45`) wrongly claims the terminal rung is
"claude opus"; the code is right and the comment is stale. Fix the comment so no one
later "corrects" the code to match it and starts billing per retry.

## Testing

Existing style is `subprocess.run` patched with assertions on command strings
(`tmux_orchestrator.py:159-200`); new tmux tests follow it.

| Area | Test |
|---|---|
| Grid placement | 5 agents -> 4 panes in `agents`, 1 in `agents-2`; asserts `split-window -P -F` and `select-layout tiled` appear in the call log |
| Pane targeting | Title and kill calls target `%N`, never `w.i` |
| Grid fallback | tmux failure falls back to `new-window`; task still runs |
| Dashboard frame | Pure frame builder over fake rows: tree indentation, model names, `-` for unparseable payload. No curses in tests |
| Tier shapes | `simple` -> 1 task, no worktree; `simple` + test_command -> 2; `standard` -> 6 (existing test unchanged); `complex` -> fan-out node set with correct `depends_on` |
| `worktree_integrate` | Real temp git repo, two worktrees: one clean merge, one genuine conflict. Asserts the conflicted file list is returned and the tree is left conflicted |
| Migrations | Opening a pre-existing DB adds `pane_target` and `started_at`, preserving rows |
| `silicorism_wait` | Returns on terminal state; returns a "still running" digest on timeout; caps at 3600s |

`worktree_integrate` is tested against real git rather than mocks: a merge handler
asserted only against command strings proves nothing about whether the merge works.

## Out of scope

- Replacing any execution model with a Claude model.
- Reading pane content back into the orchestrator (log tails already serve that).
- Pane control keybindings in the dashboard.
- Migrating to stronger OSS models (kimi-k3, glm-5.2). Deferred until this design is
  proven to deliver quality parity.
