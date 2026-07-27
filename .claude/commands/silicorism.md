---
description: Plan a task with superpowers, then have pi agents implement the plan
---

Goal: $ARGUMENTS

You plan. The pi agents build. Do not write the implementation yourself.

1. `superpowers:brainstorming` — settle the design with the user. Stop and ask when
   anything is ambiguous; a vague node prompt fails on a smaller model.
2. `superpowers:writing-plans` — turn the design into numbered tasks, each with its
   own acceptance criteria and file-level scope.
3. `silicorism_list_skills` — see what the execution nodes can bind. Superpowers
   skills are yours, not theirs: they only resolve from `~/.claude/skills`,
   `~/.pi/skills` and the CWD-local twins.
4. `silicorism_plan_and_submit` with `nodes` — ONE node per plan task, in plan
   order, each `depends_on` the previous. Copy the task's acceptance criteria and
   file scope into the node prompt verbatim. Never pass the raw goal to a built-in
   tier here: that discards the plan and makes a smaller model re-derive it.
   Models: qwen3-coder-480b builds, kimi-k2.5 reviews and fixes, glm-5 scouts.
   Set `name` to run the whole thing in a git worktree.
   Last node: `{"harness": "verify", "test_command": "<the plan's test command>"}`,
   depending on every task node. An agent can claim it is done; this cannot.
5. Tell the user: `tmux attach -t silicorism-session`.
6. `silicorism_wait` — once. Not a poll loop.
7. If the verdict is unsatisfied, read the failed nodes' artifacts, write a
   corrective DAG, resubmit through `silicorism_verify_and_continue`. Repeat until
   the verify gate passes.
