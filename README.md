# Silicorism

Silicon + Tribalism: a multi-agent task orchestrator that runs OSS coding models as
live agents, side by side, in one tmux window. SQLite in WAL mode plus a pool of
worker processes. Pure Python stdlib — no dependencies.

The premise: a strong reasoning model is expensive per token and a strong *coding*
model is not. So the expensive one plans and never executes, and a trio of OSS
models does the work in parallel, each in its own pane where you can watch it.

![Four pi agents tiled in one tmux window](docs/images/agents-grid.png)

Four `pi` agents on three different models, tiled in the `agents` window: each pane
is the real interactive TUI, not a log tail. The border carries the agent id and its
state (`RUNNING` / `DONE` / `FAILED`). Panes are capped at four per window so a TUI
stays readable — the fifth agent opens `agents-2`.

![The dashboard window](docs/images/dashboard.png)

The `dashboard` window answers "is this run healthy" at a glance: a stacked
done/failed/running/pending bar, then every node in execution order with its
model (or, for a gate node, its test command), elapsed time, pane, and `idle Nm`
when a running node's files have stopped changing. Below it, which workers are
still beating (`DEAD` when one is not), the actual failure line, and the P2P
feed. Running nodes spin, so a frozen monitor is never mistaken for a quiet
queue. Long runs fold their finished nodes into `N done` rather than pushing the
live ones off screen. `q` quits; glyphs fall back to ASCII off a UTF-8 locale or
under `SILICORISM_ASCII=1`.

## Layout
| File | Role |
|------|------|
| `db.py` | WAL layer: schema, PRAGMAs, atomic `BEGIN IMMEDIATE` writes with backoff |
| `handlers.py` | task-type handlers (`shell`, `sleep`, `echo`, `fail`, `pi`, `claude`, worktree, `fixer_loop`) |
| `worker.py` | one worker process: claim → run → log → mark, clean signal shutdown |
| `cli.py` | init / add / run / status / submit-feature / supervise / gc / msg … |
| `silicorism_tools.py` | harness-agnostic bridge: pipeline build, gc, status, worker spawn |
| `tmux_orchestrator.py` | the agents grid: tiled panes, pane-id addressing, per-task logs |
| `dashboard.py` | curses dashboard: progress bar, DAG, worker liveness, errors, P2P |
| `skills.py` | resolve + inject skill prompts from `.claude`/`.pi` (global + local) |
| `silicorism_mcp.py` | pure-stdlib JSON-RPC stdio MCP server for Claude Code |
| `extensions/silicorism.ts` | native Pi extension (typechecks clean vs shipped `pi` types) |
| `.claude/agents/` | `planner`, `executor`, `reviewer` subagent defs (`model: sonnet-5`) |
| `tests/` | concurrent-write + no-double-claim + end-to-end drain + MCP |

## Install
```bash
pip install -e .          # or: pipx install .  → global `silicorism` command
```
`--db` is optional: it defaults to `<repo>/.git/silicorism.db` inside a git repo,
else `~/.config/silicorism/repos/<slug>/silicorism.db`. So `silicorism` works from
any repo with no flags.

## Use
```bash
silicorism init                                        # DB auto-resolves per repo
silicorism add    --type shell --payload "echo hi" --priority 5
silicorism run    --workers 4 --drain                  # drop --drain to run forever
silicorism status --watch
silicorism reset                                       # requeue tasks stuck 'processing'
silicorism submit-feature --name auth --prompt "add JWT auth"   # 6-node DAG
silicorism dashboard                                   # curses DAG view
```

## Native agents, harnesses & skills
`pi`/`claude` tasks run live in a tmux pane under `SILICORISM_NATIVE=1`. The agent
owns the pane's tty — that is what makes the TUI render — and tmux mirrors the pane
into `~/.config/silicorism/logs/task-<id>.log`, whose tail becomes the task's
`output_artifact` for downstream DAG steps. A DAG node picks its harness + model via
the payload (`{"model": "opus-4.8"}` routes claude, `{"model": "deepseek-v4"}` pi).
Requested skills (`{"skills": ["tdd"]}`) are resolved from `~/.claude/skills`,
`~/.pi/skills`, and local `./.claude/skills`, `./.pi/skills` (local wins) and injected
into the agent prompt.

## MCP server (Claude Code)
```bash
claude mcp add silicorism --scope user -- python /path/to/silicorism_mcp.py
```
Tools: `silicorism_list_skills`, `silicorism_plan_and_submit`, `silicorism_wait`,
`silicorism_get_status`, `silicorism_start_workers`, `silicorism_gc`,
`silicorism_verify_and_continue`.

On connect the server sends an `instructions` protocol the orchestrator follows:
**discover skills → clarify (zero assumptions) → master plan → submit → verify &
loop**. `silicorism_list_skills` inventories skills (name/harness/scope/description)
so they can be bound to DAG nodes during planning.

**Orchestrator loop.** `silicorism_plan_and_submit` submits a plan **and
auto-starts native-pane workers** in one call. Pass `prompt` plus a
`complexity` tier, or `nodes` for a custom DAG you design — each node sets its own
`id`, `prompt`, `depends_on`, `harness` (`pi`/`claude`), `model`, `thinking`
(`high`…), and `skills`. `silicorism_get_status` returns a `satisfied` verdict
plus each failed task's artifact + last error; `silicorism_verify_and_continue`
lets the orchestrator resubmit a corrective DAG and re-run until satisfied.

Default per-role models are the **bedrock-mantle OSS pair** (`thinking:high`):
scout `zai.glm-5`, builder and fixer `moonshotai.kimi-k2.5` — all overridable
per node. Friendly names
(`qwen3-coder-480b`, `kimi-k2.5`, `glm-5`, `deepseek-v4-flash`, `nemotron-3-ultra`,
`hy3`, `mimo-2.5`, `north-mini-code`) resolve to full ids; full ids pass through.

**Complexity tiers.** `silicorism_plan_and_submit` takes
`complexity: simple | standard | complex`, so a small program does not get a
six-node pipeline built for a refactor:

| Tier | Shape | When |
|------|-------|------|
| `simple` | one agent on `kimi-k2.5`, in `cwd`, no worktree; a `verify` node only if you supply a test command | a self-contained program |
| `standard` | worktree → scout → builder → fixer → verify → cleanup | a change to an existing codebase |
| `complex` | two builders in separate worktrees, fanned out from one scout, rejoined by `worktree_integrate` plus an integrator agent | work that splits into disjoint slices |

An unrecognised tier degrades to `standard`; a typo in a planning hint should not
fail a submit.

**Waiting, not polling.** `silicorism_wait` blocks until the queue settles and
returns the verdict once. Polling status in a loop costs a full orchestrator turn
per poll to learn "still running" — this is the difference between planning being
cheap and watching being expensive. It also settles early on a *fresh* failure, and
does not count nodes stranded behind a failed dependency as still active.

**The agents grid.** `pi` tasks run the full interactive TUI in a tiled pane
(`extensions/autoexit.ts` exits pi when the agent settles, writes the clean final
answer to `$SILICORISM_ARTIFACT`, and a sentinel file captures the exit code). Panes
are addressed by tmux pane id, not window index, so a closed pane never shifts
another agent's address. Attach with `tmux attach -t silicorism-session`.

**Retry escalation.** A failed `pi` task requeues on the next stronger model:
kimi-k2.5 → glm-5, then fails for the orchestrator to handle.

**Verify gate + merge.** The standard pipeline is six nodes: a deterministic `verify`
node re-runs the test command after the fixer — cleanup is unreachable unless it
exits 0. `build_pipeline(..., merge=True)` adds a `worktree_merge` node that
commits worktree changes and `--no-ff` merges the branch into base; conflicts
fail the task and quarantine the worktree.

## Concurrency model
- Every state write goes through `db.immediate()` = `BEGIN IMMEDIATE` + exponential
  backoff on `SQLITE_BUSY`. `busy_timeout=5000` absorbs normal contention; backoff
  covers the tail.
- `claim_task` selects the top-priority `pending` row and flips it to `processing`
  in one transaction — a task can never be claimed twice.
- Workers requeue their in-flight task on `SIGINT`/`SIGTERM`; nothing is orphaned.
- Idle loops run `PRAGMA wal_checkpoint(PASSIVE)` for non-blocking WAL upkeep.
- A draining worker exits only when nothing is pending *and* nothing is in flight —
  an empty poll means "nothing claimable yet", and exiting on it would leave one
  worker to run a fan-out serially.
- Pane placement is serialised with a lock file: four workers racing to open the
  grid would otherwise each create their own `agents` window.
- `silicorism_wait` requeues tasks whose worker stopped heartbeating, so a pane
  closed mid-run cannot strand everything behind it.
- Cleanup commits the worktree before removing it and deletes the branch only if
  it merged, so an unmerged run's work survives on its branch.

## Test
```bash
python -m pytest tests/ -q         # 106 tests
python tests/test_db.py            # 8 procs × writes, no busy, no double-claim
python tests/test_integration.py   # 40-task pool drains, correct terminal states
```

## Subagents
`.claude/agents/*.md` define delegation roles for Claude Code: **planner** decomposes
a goal and enqueues tasks, **executor** runs one atomic task, **reviewer** audits
read-only. The Python pool executes the queue; the agents decide what goes in it.
