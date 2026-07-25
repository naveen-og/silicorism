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
claude mcp add silicorism --scope user -- python /path/to/silicorism_mcp.py
```
Tools: `silicorism_list_skills`, `silicorism_plan_and_submit`,
`silicorism_get_status`, `silicorism_start_workers`, `silicorism_gc`,
`silicorism_verify_and_continue`.

On connect the server sends an `instructions` protocol the orchestrator follows:
**discover skills → clarify (zero assumptions) → master plan → submit → verify &
loop**. `silicorism_list_skills` inventories skills (name/harness/scope/description)
so they can be bound to DAG nodes during planning.

**Orchestrator loop.** `silicorism_plan_and_submit` submits a plan **and
auto-starts native-pane workers** in one call. Pass `prompt` for the default
5-task pipeline, or `nodes` for a custom DAG you design — each node sets its own
`id`, `prompt`, `depends_on`, `harness` (`pi`/`claude`), `model`, `thinking`
(`high`…), and `skills`. `silicorism_get_status` returns a `satisfied` verdict
plus each failed task's artifact + last error; `silicorism_verify_and_continue`
lets the orchestrator resubmit a corrective DAG and re-run until satisfied.

Default per-role models are the **bedrock-mantle OSS trio** (`thinking:high`):
scout `zai.glm-5`, builder `qwen.qwen3-coder-480b-a35b-instruct`, fixer
`moonshotai.kimi-k2.5` — all overridable per node. Friendly names
(`qwen3-coder-480b`, `kimi-k2.5`, `glm-5`, `deepseek-v4-flash`, `nemotron-3-ultra`,
`hy3`, `mimo-2.5`, `north-mini-code`) resolve to full ids; full ids pass through.

**Live TUI panes.** `pi` tasks run the full interactive pi TUI in their tmux pane
(`extensions/autoexit.ts` exits pi when the agent settles, writes the clean final
answer to `$SILICORISM_ARTIFACT`, and the sentinel captures the exit code). Attach
with `tmux attach -t silicorism-session` to watch agents work in parallel.

**Retry escalation.** A failed `pi` task requeues on the next stronger model:
qwen3-coder-480b → kimi-k2.5 → glm-5, then fails for the orchestrator to handle.

**Verify gate + merge.** The 5-task pipeline is now 6: a deterministic `verify`
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
