---
name: silicorism
description: Use when the user asks to build, implement or refactor something with silicorism, or says "orchestrate this", "use the pi agents", "dispatch workers", or invokes /silicorism. Runs the ask -> plan -> dispatch-to-pi protocol where THIS session orchestrates and pi worker agents on OSS models do the implementation.
---

# Silicorism orchestration

You are the orchestrator. Pi agents on OSS models write the code. You never spawn a
Claude subagent and never assign a Claude model to an execution node.

The trade this architecture makes: your reasoning is expensive and their tokens are
cheap, so **everything you know has to be in the plan before anything runs**. A node
that has to work out what you meant is a node running on a smaller model's guess.

## Protocol

1. **Inventory** — call `silicorism_list_skills` before planning. Every agent node gets
   `coding-excellence` unless you pass `skills` explicitly; name others when the work
   needs them.
2. **Ask before assuming** — if the request is ambiguous, or the environment, deps or
   test command are unverified, STOP and ask. Verify claims against the filesystem
   yourself; an execution node inherits every wrong assumption you queue.
3. **Plan** — `superpowers:brainstorming` to settle the design with the user, then
   `superpowers:writing-plans`. Superpowers skills run in YOUR context; execution nodes
   only resolve skills returned by `silicorism_list_skills`. One plan task becomes one
   node, in plan order.
4. **Submit** — `silicorism_plan_and_submit` with `nodes`. See the node contract below.
   Tell the user to watch with `tmux attach -t silicorism-session`.
5. **Wait** — `silicorism_wait` once. Do not poll `silicorism_get_status` in a loop.
   `timed_out: true` means nothing settled: read `silicorism_get_status`'s `stalled`
   list, `silicorism_cancel_task` the wedged node, then wait again. A dead pipeline
   that poisons the verdict is cleared with `silicorism_gc(stuck=true, tasks=true)`.
6. **Verify by hand, then loop** — a green run is where your checking starts, not where
   it ends. See "What a green run does not prove". On failure, read the pane output and
   the task artifact, find the root cause, submit a corrective DAG.

## The node contract

Each node carries `id`, `prompt`, `depends_on`, `harness` (`pi` or `verify`), `model`,
`thinking`, and — this is what separates a good DAG from a lucky one — `requires`,
`writes` and `test_command`.

```json
{"id": "auth-impl", "depends_on": ["auth-test"], "harness": "pi",
 "model": "kimi-k2.5", "thinking": "high",
 "writes": ["internal/auth/jwt.go"],
 "prompt": "...acceptance criteria and file-level scope, verbatim from the plan...",
 "requires": {"files":   ["internal/auth/jwt.go"],
              "symbols": {"internal/auth/jwt.go": ["func ValidateJWT", "type Claims"]},
              "absent":  {"internal/auth/jwt.go": ["TODO", "unimplemented"]},
              "min_lines": {"internal/auth/jwt_test.go": 30}},
 "test_command": "go test ./internal/auth/..."}
```

- **`requires` is not optional on a building node.** The worker checks it literally
  after the agent exits and before the tests, and fails the node when something the
  plan named is missing. Write it from the plan's own acceptance criteria: the files
  that must exist, the symbols that must appear in them, the placeholders that must not
  survive. Tests only fail on what they cover — this is the only thing in the system
  that catches work that was never done.
- **`test_command` on the node itself** is run by the worker after the agent exits, so
  a node cannot report a pass its own tests do not support.
- **`writes`** declares the files a node owns; two unordered nodes claiming one file are
  rejected at submit.
- **`stall_timeout_s`** (default 600) fails a node whose files stop changing, instead of
  holding it to the 3600s ceiling. Raise it for a genuinely slow node.
- **End the chain with a `harness: "verify"` node** holding the real suite command, so
  no agent can close the run by claiming it.

## Order the DAG red-green

For anything with testable behaviour:

1. a `pi` node that writes the failing test and nothing else (`writes` says so),
2. a `verify` node with `expect_fail: true` on that test command,
3. a `pi` node that implements it,
4. a normal `verify` node.

Step 2 is what makes step 1 real. A test that passes before the code exists asserts
nothing, and without this gate the pipeline cannot tell the difference.

## Choosing model and thinking

`glm-5` reasons and scouts; `kimi-k2.5` builds, reviews and fixes. Never
`qwen3-coder-480b`, never a Claude model on an execution node.

| Node | Model | Thinking |
|---|---|---|
| Scouting an unfamiliar codebase, choosing an approach | `glm-5` | `high` |
| Building real logic, diagnosing and fixing a failure | `kimi-k2.5` | `high` |
| Mechanical work — rename, move, boilerplate, formatting | `kimi-k2.5` | `medium` |

When in doubt use `high`. A node redone costs more than the thinking that would have
avoided it.

## Worktrees

Pass `name` for an isolated git worktree and `cwd` for the repo it branches from.
Submit rejects a named DAG whose `cwd` is not inside a repository, and the base branch
defaults to that repo's own current branch. Omit `name` to run the nodes in the repo
itself — and then only with a restore point committed first.

## What a green run does not prove

`verify` proves the tests pass. It cannot prove the spec was built, because tests only
fail on what they cover. Every defect that has survived a green gate here was an
absence: a list capped at 3 where the spec said 6, a function imported by the tests and
never called, a feature stubbed with "not critical for the core functionality" and its
test quietly dropped. `requires` catches the ones you thought to name. For the rest,
after the run:

- diff the test files for weakened or deleted assertions,
- check each numbered acceptance criterion against the source yourself,
- back up the working tree before dispatching any node that rewrites existing files —
  one truncated write destroyed 305 lines of uncommitted tests, and node artifacts hold
  only REPORT text.

A pane that looks wedged is usually a hung test command, not a stalled model: check file
mtimes under the node's cwd, the pane's token counters, and `ps` for the test process
before re-dispatching.

## Why the prompts must be heavy

Execution models are smaller than you. Spend your reasoning at plan time: exact file
paths, exact acceptance criteria, the exact command whose output proves the task is
done, and an explicit ban on the scope the node must not touch.
