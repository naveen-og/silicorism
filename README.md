# Silicorism

Silicon + Tribalism: high-throughput multi-agent task orchestrator. SQLite in WAL mode + a pool of
concurrent worker processes. Pure Python stdlib — no dependencies.

## Layout
| File | Role |
|------|------|
| `db.py` | WAL layer: schema, PRAGMAs, atomic `BEGIN IMMEDIATE` writes with backoff |
| `handlers.py` | task-type handlers (`shell`, `sleep`, `echo`, `fail`, `pi`, `claude`, worktree, `fixer_loop`) |
| `worker.py` | one worker process: claim → run → log → mark, clean signal shutdown |
| `cli.py` | init / add / run / status / submit-feature / supervise / gc / msg … |
| `silicorism_tools.py` | harness-agnostic bridge: pipeline build, gc, status, worker spawn |
| `tmux_orchestrator.py` | live tmux panes per task; stdout tee'd to a per-task log |
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
silicorism submit-feature --name auth --prompt "add JWT auth"   # 5-task DAG
```

## Native agents, harnesses & skills
`pi`/`claude` tasks run live in a tmux pane under `SILICORISM_NATIVE=1`; stdout/stderr
tee to `~/.config/silicorism/logs/task-<id>.log` and the tail becomes the task's
`output_artifact` for downstream DAG steps. A DAG node picks its harness + model via
the payload (`{"model": "opus-4.8"}` routes claude, `{"model": "deepseek-v4"}` pi).
Requested skills (`{"skills": ["tdd"]}`) are resolved from `~/.claude/skills`,
`~/.pi/skills`, and local `./.claude/skills`, `./.pi/skills` (local wins) and injected
into the agent prompt.

## MCP server (Claude Code)
```bash
claude mcp add silicorism -- python /path/to/silicorism_mcp.py
```
Exposes `silicorism_plan_and_submit`, `silicorism_get_status`,
`silicorism_start_workers`, `silicorism_gc`.

## Concurrency model
- Every state write goes through `db.immediate()` = `BEGIN IMMEDIATE` + exponential
  backoff on `SQLITE_BUSY`. `busy_timeout=5000` absorbs normal contention; backoff
  covers the tail.
- `claim_task` selects the top-priority `pending` row and flips it to `processing`
  in one transaction — a task can never be claimed twice.
- Workers requeue their in-flight task on `SIGINT`/`SIGTERM`; nothing is orphaned.
- Idle loops run `PRAGMA wal_checkpoint(PASSIVE)` for non-blocking WAL upkeep.

## Test
```bash
python tests/test_db.py            # 8 procs × writes, no busy, no double-claim
python tests/test_integration.py   # 40-task pool drains, correct terminal states
# or: python -m pytest tests/ -q
```

## Subagents
`.claude/agents/*.md` define delegation roles for Claude Code: **planner** decomposes
a goal and enqueues tasks, **executor** runs one atomic task, **reviewer** audits
read-only. The Python pool executes the queue; the agents decide what goes in it.
