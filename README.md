# Silicorism

Silicon + Tribalism: high-throughput multi-agent task orchestrator. SQLite in WAL mode + a pool of
concurrent worker processes. Pure Python stdlib — no dependencies.

## Layout
| File | Role |
|------|------|
| `db.py` | WAL layer: schema, PRAGMAs, atomic `BEGIN IMMEDIATE` writes with backoff |
| `handlers.py` | task-type handlers (`shell`, `sleep`, `echo`, `fail`) |
| `worker.py` | one worker process: claim → run → log → mark, clean signal shutdown |
| `cli.py` | init / add / run / status / reset |
| `.claude/agents/` | `planner`, `executor`, `reviewer` subagent defs (`model: sonnet-5`) |
| `tests/` | concurrent-write + no-double-claim + end-to-end drain |

## Use
```bash
python cli.py init   --db silicorism.db
python cli.py add    --db silicorism.db --type shell --payload "echo hi" --priority 5
python cli.py run    --db silicorism.db --workers 4 --drain      # drops --drain to run forever
python cli.py status --db silicorism.db --watch
python cli.py reset  --db silicorism.db                          # requeue tasks stuck 'processing'
```

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
