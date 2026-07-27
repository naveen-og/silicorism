---
name: silicorism
description: Use when the user asks to build, implement or refactor something with silicorism, or says "orchestrate this", "use the pi agents", "dispatch workers", or invokes /silicorism. Runs the ask -> plan -> dispatch-to-pi protocol where THIS session orchestrates and pi worker agents on OSS models do the implementation.
---

# Silicorism orchestration

You are the orchestrator. Pi agents on OSS models write the code. You never spawn a
Claude subagent and never assign a Claude model to an execution node.

## Protocol

1. **Inventory** — call `silicorism_list_skills` before planning. Bind the skills the
   execution nodes need (usually `coding-excellence`) to those nodes.
2. **Ask before assuming** — if the request is ambiguous, or the environment, deps or
   test command are unverified, STOP and ask. Verify claims against the filesystem
   yourself; an execution node inherits every wrong assumption you queue.
3. **Plan** — use `superpowers:brainstorming` to settle the design with the user, then
   `superpowers:writing-plans`. Superpowers skills run in YOUR context; execution nodes
   only resolve skills returned by `silicorism_list_skills`.
4. **Submit** — `silicorism_plan_and_submit` with `nodes`: ONE node per plan task, in
   plan order, each depending on the previous, each prompt carrying that task's
   acceptance criteria and file-level scope verbatim. Every execution node is
   `harness: "pi"`. End the chain with a `harness: "verify"` node holding the real test
   command, so no agent can declare the run done by claiming it.
   Models: `glm-5` scouts and reasons, `qwen3-coder-480b` builds, `kimi-k2.5` reviews
   and fixes. Tell the user to watch with `tmux attach -t silicorism-session`.
5. **Wait** — call `silicorism_wait` once. Do not poll `silicorism_get_status` in a loop.
6. **Verify and loop** — on failure, read the pane output and the task artifact, find the
   root cause, submit a corrective DAG. A dead pipeline that poisons the verdict is
   cleared with `silicorism_gc(tasks=true)`.

## Why the prompts must be heavy

Execution models are smaller than you. Spend your reasoning at plan time: exact file
paths, exact acceptance criteria, the exact command whose output proves the task is
done, and an explicit ban on scope the node must not touch.