# Silicorism failure report — 2026-07-29

Observed while running a 5-step TypeScript refactor (repo `/home/naveen/Projects/splice`)
through silicorism DAGs on bedrock models (`moonshotai.kimi-k2.5`, `zai.glm-5`, both at
`thinking: high`, provider shown in panes as `bedrock-mantle`).

**Outcome:** the work completed and the test suite ended green (129/129), but ~3 hours were
lost to failures that silicorism did not surface. Every item below is something a new
session should be able to act on without re-deriving context.

Each entry is tagged **CONFIRMED** (directly observed with evidence) or **SUSPECTED**
(one observation, alternative explanations not ruled out). Do not "fix" a SUSPECTED item
before reproducing it.

## Status — fixed 2026-07-29

Every CONFIRMED item is fixed; see `docs/superpowers/specs/2026-07-29-silicorism-failure-fixes-design.md`
and the plan beside it. F7 and F9 are untouched by design — reproduce first.

| Item | Fix |
|---|---|
| F1 | `test_command` on a pi node; the worker runs it before `db.complete_task` |
| F2 | `tasks.last_progress_at`, stamped from the newest mtime under the task cwd; `get_status().stalled` |
| F3 | `stall_timeout_s` (default 600) fails a node whose files stop changing; `timeout_s` overrides the 3600s cap |
| F4 | launch script traps HUP/INT/TERM and signals its process group; the worker kills the pane on stall/timeout |
| F5 | `db.fail_stuck` via `silicorism_gc(stuck=true)`, plus `silicorism_cancel_task` |
| F6 | `wait` returns `timed_out` and `elapsed_s`, and the tool says what to do next |
| F8 | qwen3-coder-480b removed from every default and from the escalation ladder |
| F10 | pane labels carry a run slug; successful panes are closed |
| F11 | every pi node prompt ends with a required-deliverables block |

### Verified against real runs — 2026-07-30

A real `run_worker` in native mode driving real tmux panes, with only the agent
command substituted so the run is deterministic:

| Check | Evidence |
|---|---|
| F1 | agent printed "ALL DONE - everything passes" and exited 0; `test_command: exit 3` → task **failed** in 1.0s, log `error: verify failed (exit 3)` |
| F1 (pass) | same node with `test_command: true` → **completed**, artifact ends `verify passed: true` |
| F3 | agent hung; `stall_timeout_s: 20` → **failed** at ~20s, log `error: native agent stalled: no progress for 20s` |
| F4 | that agent's 3 `sleep` descendants: 3 while running, **0** after; pane gone |
| F5 | wedged `processing` row → `silicorism_gc(stuck=true, tasks=true)` returned `"stuck": [1]`, processing 1→0, failed 0→1 |
| F6 | `silicorism_wait(timeout_s=60)` → `settled: false, timed_out: true, elapsed_s: 60.0` at a 60.0s wall clock; `silicorism_cancel_task` then cleared it |

Two bugs the verification itself turned up, both fixed:

- Pane logs and artifacts were keyed on the task id alone in one shared
  directory, and every DB numbers its tasks from 1 — so a node with no artifact
  of its own reported an unrelated run's pane text as its output, and that text
  is handed to the next node as context. Now namespaced per DB and truncated
  before the pane opens.
- `silicorism dashboard` could never start from the installed CLI
  (`ModuleNotFoundError: dashboard`), and died on an uninitialised DB.

---

## F1 — A node reported success while its own tests were failing — **CONFIRMED**

**Severity: highest. This is the one that silently corrupts a run.**

The `step4-gopls-second-language` node was marked `completed`, the DAG advanced to step 5,
and `silicorism_get_status` showed `failed: 0`. The node's prompt stated its acceptance
criteria explicitly:

> `npm test` prints `fail 0`, with your new Go tests PASSING and NOT skipped.

Running the node's own test file by hand immediately after it "completed":

```
ℹ tests 4
ℹ pass 3
ℹ fail 1
✖ reports an overlay type error, disk untouched
  AssertionError: expected at least 1 diagnostic, got 0: []
```

The agent either never ran the command, ran it and misread it, or ran it and reported
success anyway. Nothing in the pipeline checked.

**Why it matters:** a false success is worse than a failure. It propagated into step 5,
whose prompt was written assuming a green baseline.

**Fix direction:** node completion must not be self-declared. Either
(a) attach an optional `test_command` to `harness: "pi"` nodes and have the *worker* run it
after the agent exits, marking the task failed on non-zero, or
(b) require the agent to write its command output to the artifact and have the worker
regex it for a pass/fail token before calling `db.complete_task`.
`worker.py:151` (`db.complete_task(conn, tid, artifact=artifact)`) currently completes a task
purely on the pane's exit code, which reflects the agent process exiting, not the work
being correct.

**Verify a fix:** give a node a prompt whose acceptance is `npm test` on a suite you have
deliberately broken. The node must end `failed`, not `completed`.

---

## F2 — Heartbeat proves the worker is alive, never that the agent is progressing — **CONFIRMED**

`silicorism_get_status` reported, for over an hour:

```json
{"agent_id": "worker-0", "status": "busy", "current_task_id": 1,
 "last_seen": "2026-07-29T16:54:46.338Z"}   // claimed at 16:24:45
```

`busy` with a fresh `last_seen` looks exactly like healthy progress. It was not: the agent's
`npm test` had hung, and zero files were written in that hour.

This is by construction. `worker.py:108-129` `_stop_and_beat` fires
`db.heartbeat(conn, agent_id, "busy", task_id)` on a fixed 30s timer while `_run_native`
blocks. The docstring is explicit that the beat exists to stop `db.reap_stale` (`db.py:417`,
300s) from double-claiming the task. It is a liveness beat for the Python worker loop. It
carries no signal about the agent.

**Consequence:** the only honest reading of `status: busy` is "the worker process has not
crashed". An orchestrator cannot distinguish a working agent from a wedged one.

**Fix direction:** add a progress signal distinct from liveness. Cheapest useful version:
record, on each beat, a cheap fingerprint of observable progress — pane byte count / last
pane-content hash, or `mtime` of the newest file under the task's `cwd` — and expose it in
`silicorism_get_status` as e.g. `last_progress_at`. Then `busy` + `last_progress_at` an hour
old is machine-detectable.

**Verify a fix:** launch a node whose prompt makes it run `sleep 3600`. Status must show it
as making no progress within a few minutes.

---

## F3 — Nothing detects or breaks a no-progress node for a full hour — **CONFIRMED**

`tmux_orchestrator.py:294`:

```python
def wait_for_exit(sentinel: str, *, timeout: float = 3600.0, poll: float = 0.5, stop=None)
```

One hour is the only backstop, and it is a wall-clock cap, not a stall detector. A node that
hangs at minute 2 still occupies its worker until minute 60. With a sequential DAG
(`depends_on` chains) that stalls the entire pipeline.

Observed twice in one session: two separate nodes each burned ~60 minutes and ~25 minutes on
a hung `npm test`.

**Fix direction:** make the cap configurable per node (`timeout_s` on the node spec) and add
a much shorter *stall* timeout driven by the F2 progress signal — e.g. fail the task after
N minutes with no progress, independent of the 3600s ceiling.

---

## F4 — On timeout the pane and its child processes are not killed — **CONFIRMED (by code reading) / partially observed**

When `wait_for_exit` returns `None`, `_run_native` (`worker.py:144-146`) raises
`RuntimeError("native agent exit timeout")` and the `except` branch calls
`_mark_pane(tid, pane, failed=True, window=win)`. `_mark_pane` (`worker.py:96-105`) only
calls `tmux.mark_pane_done`, which retitles the pane. Nothing kills the pane, the agent, or
the child command it spawned.

I had to clean up by hand:

```
tmux kill-pane -t %14
pkill -f 'pyright-langserver'; pkill -f 'gopls'
```

**Consequence:** a timed-out node leaks a live agent plus whatever it spawned (here: language
server daemons) that keep consuming RAM and can interfere with later runs.

**Fix direction:** on the failure path, `tmux kill-pane` the pane after capturing its log,
and ensure the pane's command runs in its own process group so children die with it.

---

## F5 — `silicorism_gc` cannot clear a task stuck in `processing` — **CONFIRMED**

Tool contract: *"tasks=true prunes **terminal** task rows"*. A task wedged in `processing` is
not terminal, so `gc` cannot touch it. With the worker also wedged, the row is unclearable
and permanently poisons `silicorism_wait`'s verdict for that DB.

Workaround used: abandon the DB and submit the next DAG against a brand-new `db` path. That
works but discards all history and is not discoverable — nothing in the tool descriptions
suggests it.

**Fix direction:** either let `gc` force-reset `processing` rows whose agent has not
heartbeated within `reap_stale`'s window, or expose an explicit
`silicorism_cancel_task(task_id)`.

---

## F6 — `silicorism_wait` timing out is indistinguishable from a real verdict — **CONFIRMED**

Two consecutive 1800s waits returned:

```json
{"satisfied": false, "tasks": {...}, "failures": [], "settled": false}
```

Same shape as a genuine settled-with-failures verdict. The only discriminator is
`settled: false`, which is easy for a model to skim past, and there is no
`timed_out: true` or elapsed field. Guidance says "call wait once, do not poll" — so on
timeout the caller is left with no sanctioned next action.

**Fix direction:** return an explicit `timed_out: true` plus `elapsed_s`, and document the
correct response (inspect, then either re-wait or intervene).

---

## F7 — Node `model` may not be honoured — **SUSPECTED, reproduce before fixing**

DAG submitted with `step5-generalise-timeout-warning` specified as `model: "kimi-k2.5"`.
The pane the worker logged for that task (`task_id 2 ... native pane agents-4.%14`) showed a
statusline of `(bedrock-mantle) zai.glm-5 • high`.

**Why this is only SUSPECTED:** that pane's scrollback also showed work belonging to step 4
(editing `gopls-provider.ts`), so the pane may have been reused, or I may have captured a
pane that did not correspond to the task I thought. In the first DAG, `model: "kimi-k2.5"`
did produce a `moonshotai.kimi-k2.5` pane, so the mapping works at least sometimes.

**How to reproduce:** submit a 2-node DAG with deliberately different models
(`kimi-k2.5` then `glm-5`), and for each node log the resolved model into the artifact.
Compare against the spec. Do not change resolution code until this shows a real mismatch.

---

## F8 — Skill guidance contradicts the operator's standing model preference — **CONFIRMED (documentation)**

`~/.claude/skills/silicorism/SKILL.md` step 4 says:

> Models: `glm-5` scouts and reasons, `qwen3-coder-480b` builds, `kimi-k2.5` reviews and fixes.

The operator's standing instruction is that `qwen3-coder-480b` must never be used, and that
execution nodes run `kimi-k2.5` or `glm-5` at high thinking. An orchestrator following the
skill verbatim violates the operator preference on every build node.

**Fix direction:** update the skill text, or make the recommendation reference a
configurable default rather than hardcoded model names.

---

## F9 — Execution agents inherit the operator's global prompt customisations — **SUSPECTED**

Every agent pane's statusline showed `caveman level: FULL` and `ponytail: ⚡ FULL`, i.e. the
agents are loading the user's global `CLAUDE.md` plus the caveman (terse output) and ponytail
(minimise code) plugins.

Both plugins are tuned for a conversational assistant, not for an execution agent that must
report full command output. Caveman explicitly instructs "no dumping long raw error logs
unless asked" — directly at odds with F1's requirement that a node paste real test output.
Ponytail instructs minimising code, which can conflict with a prompt's explicit scope.

**Why SUSPECTED:** I did not isolate this as the cause of any specific failure. It is a
plausible contributing factor to F1 (agent summarising instead of pasting output) and to the
omitted config value in F11.

**How to test:** run the same node prompt twice, once with the plugins disabled for the
agent, and compare whether command output is pasted verbatim.

---

## F10 — Long-lived silicorism tmux session accumulates dead panes — **CONFIRMED (minor)**

`silicorism-session` had been alive since 14:18 with 4 windows and 13 panes, most of them
finished shells from earlier runs, plus one agent still "Working" from a previous session
unrelated to my DAG. This makes it genuinely hard to tell which pane belongs to which run;
I had to compare token counters between panes to identify mine.

**Fix direction:** name panes/windows with the DB path or pipeline id, and reap panes for
terminal tasks.

---

## F11 — Agent quality issues worth prompt-hardening (not silicorism bugs) — **CONFIRMED**

These are model behaviours, listed because they are cheap to defend against in prompt
templates:

1. **Silently dropped a required config value.** Prompt said
   `maxWaitMs: <pick a value that actually works>`. The agent omitted the key entirely,
   leaving a default tuned for a different language server that would have failed on first
   use. Nothing flagged it.
2. **Did not verify the format of a shell command's output.** It parsed `go version` with
   `.split(" ")[2].replace(/^go/,"")`. On this machine `go version` returns
   `go1.26.5-X:nodwarf5`, producing an invalid `go.mod` directive, which made the language
   server fail initialization and publish nothing. The agent then reported success (F1).
3. **Wrote a test whose fixture could not fail.** It asserted a "type error" that is legal
   in the target language, so the test could never have detected a regression.
4. **Wrote a stale comment describing a fallback it never implemented.**

**Fix direction:** node prompt templates should require, as literal deliverables, (a) the
pasted output of every acceptance command, and (b) an explicit statement of any value the
prompt asked the agent to choose.

---

## Priority order for a fix session

1. **F1** — false success. Nothing else matters if a node can lie about passing.
2. **F2 + F3** — progress signal and stall timeout. These turn a 60-minute mystery into a
   2-minute failure.
3. **F5 + F6** — recovery and wait ergonomics; currently there is no sanctioned way out of a
   wedged pipeline.
4. **F4 + F10** — process and pane hygiene.
5. **F8 + F11** — documentation and prompt templates.
6. **F7 + F9** — reproduce first, then decide.

---

## Note on the underlying hang (not a silicorism defect)

The hang that triggered F2/F3/F4 was my own bug in the target repo: a fixture file placed at
`test/fixtures/fake-lsp-server.mjs`, where `node --test`'s default discovery glob
`**/test/**/*.mjs` executed it as a test file. It blocked on stdin forever and hung the whole
suite.

That is worth stating plainly because it defines what silicorism should have done: it was
never going to prevent the hang, but an hour of `⠹ Working...` with no progress signal, no
stall timeout, and a `busy` heartbeat is what turned a 5-minute diagnosis into a 3-hour one.
The fixes above are about observability, not about preventing user bugs.
