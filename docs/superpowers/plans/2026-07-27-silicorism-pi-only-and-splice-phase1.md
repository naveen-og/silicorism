# Silicorism Pi-Only Orchestration + Splice Phase 1 Implementation Plan

> **For agentic workers:** this plan is executed by pi worker nodes dispatched through
> silicorism. The orchestrating Claude session designs, submits, gates and reviews;
> it never spawns a Claude subagent to do the work. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make silicorism structurally incapable of routing execution work to a Claude
model, give it a task-prune path so a dead pipeline can be cleared from inside the MCP
session, ship a Claude Code skill so a cold session runs ask → plan → dispatch-to-pi,
then build Splice Phase 1 (`cst-indexer.ts`) with pi worker nodes.

**Architecture:** `build_dag` in `silicorism_tools.py` is the single choke point every DAG
node passes through, so harness coercion lands there and every caller (MCP tool, CLI,
built-in tiers) inherits it. The MCP schema and `INSTRUCTIONS` string are narrowed to
match, so a cold client is never offered the Claude harness in the first place. Task
pruning extends the existing `silicorism_gc` tool rather than adding a new one. The
Claude Code skill is a file in this repo, symlinked into `~/.claude/skills/`, matching
how every other skill there is wired.

**Tech Stack:** Python 3 (stdlib + sqlite3, no new deps), pytest for repo tests,
Node 26 + web-tree-sitter 0.26.11 + TypeScript 5.9.3 for Splice.

## Global Constraints

- No new Python dependencies. Repo is stdlib-only by design.
- Execution nodes run OSS models only: `qwen3-coder-480b` (build), `kimi-k2.5`
  (review/fix), `glm-5` (reason/scout). Never a Claude model, never a Claude subagent.
- Model friendly names resolve **only** on the pi branch: `handlers.py:80`
  `resolve_model` is called at `handlers.py:192` (pi) and never in the claude branch at
  `handlers.py:196-198`. This is the root cause of the `glm-5` failure and the reason
  for Task 1.
- Repo tests: `cd /home/naveen/Projects/orchestrator && python -m pytest -q`.
- Splice tests: `cd /home/naveen/Projects/splice && npm run typecheck && npm test`.
- Splice source must compile under `erasableSyntaxOnly: true`, `verbatimModuleSyntax: true`,
  `noUncheckedIndexedAccess: true` — no enums, no parameter properties, no namespaces,
  `import type` for type-only imports.
- Splice repo is private, git-initialised at `/home/naveen/Projects/splice`, HEAD
  `c913877`. Deps already installed; no agent may run `npm install`.

## File Structure

| File | Responsibility | Workstream |
|---|---|---|
| `silicorism_tools.py:280-283` | harness coercion choke point | 1 |
| `silicorism_tools.py` (new `prune_tasks`) | delete terminal tasks + their logs | 1 |
| `silicorism_mcp.py:233-236, 362-366` | schema: drop `claude` from the enum, expose `tasks` on gc | 1 |
| `silicorism_mcp.py:31-70` | `INSTRUCTIONS`: pi-only protocol wording | 1 |
| `skills/silicorism/SKILL.md` (new) | cold-session orchestrator protocol for Claude Code | 1 |
| `tests/test_tiers.py`, `tests/test_mcp.py` | regression coverage for the above | 1 |
| `/home/naveen/Projects/splice/src/cst-indexer.ts` (new) | Layer 1: parse, index, `#Node-ID`, scope map | 2 |
| `/home/naveen/Projects/splice/test/cst-indexer.test.ts` (new) | Layer 1 stability + summary tests | 2 |

---

## WORKSTREAM 1 — Orchestrator hardening (executed by pi nodes, gated by pytest)

### Task 1: Coerce every execution node onto the pi harness

**Files:**
- Modify: `silicorism_tools.py:278-284` (inside `build_dag`)
- Modify: `silicorism_tools.py:516-519` (the inline self-test that submits `harness: "claude"`)
- Test: `tests/test_tiers.py`

**Interfaces:**
- Consumes: `db.add_task(conn, task_type, payload, *, depends_on, worktree_path)` — `db.py:215`.
- Produces: `build_dag` never inserts a task row with `task_type == "claude"`. Task 2 and
  Task 3 rely on that invariant; the skill in Task 3 states it as a guarantee.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tiers.py`:

```python
def test_claude_harness_is_coerced_to_pi(tmp_path):
    """A node asking for the claude harness still runs on pi: execution never
    bills or routes to a Claude model."""
    dbp = str(tmp_path / "coerce.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    silicorism_tools.build_dag(conn, dbp, [
        {"id": "a", "prompt": "x", "harness": "claude", "model": "glm-5"},
    ])
    rows = conn.execute("SELECT task_type FROM tasks").fetchall()
    assert [r["task_type"] for r in rows] == ["pi"]
    conn.close()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd /home/naveen/Projects/orchestrator && python -m pytest tests/test_tiers.py::test_claude_harness_is_coerced_to_pi -q`
Expected: FAIL — `assert ['claude'] == ['pi']`.

- [ ] **Step 3: Implement the coercion**

In `silicorism_tools.py`, replace the harness block at lines 280-283:

```python
        harness = n.get("harness") or "pi"
        if harness not in ("pi", "claude", "verify"):
            raise ValueError(
                f"node {nid!r}: harness must be 'pi', 'claude' or 'verify'")
```

with:

```python
        harness = n.get("harness") or "pi"
        if harness not in ("pi", "claude", "verify"):
            raise ValueError(
                f"node {nid!r}: harness must be 'pi' or 'verify'")
        # Execution is pi-only. "claude" is accepted for back-compat and coerced:
        # the OSS friendly names resolve on the pi branch alone (handlers.py:192),
        # so a claude node with model "glm-5" dies at CLI startup instead of running.
        if harness == "claude":
            harness = "pi"
```

- [ ] **Step 4: Run the test again**

Run: `cd /home/naveen/Projects/orchestrator && python -m pytest tests/test_tiers.py::test_claude_harness_is_coerced_to_pi -q`
Expected: PASS.

- [ ] **Step 5: Fix the stale inline self-test**

`silicorism_tools.py:518` submits `{"id": "b", ..., "harness": "claude"}` and only asserts
the node ids. Leave the node as-is (it now exercises the coercion path) and add one line
after the existing `assert set(dag["nodes"]) == {"a", "b"}`:

```python
        assert [r["task_type"] for r in
                conn.execute("SELECT task_type FROM tasks ORDER BY id")] == ["pi", "pi"]
```

- [ ] **Step 6: Full suite green**

Run: `cd /home/naveen/Projects/orchestrator && python -m pytest -q`
Expected: all pass, zero failures.

- [ ] **Step 7: Commit**

```bash
cd /home/naveen/Projects/orchestrator
git add silicorism_tools.py tests/test_tiers.py
git commit -m "fix: coerce claude harness nodes onto pi so execution never routes to a Claude model"
```

---

### Task 2: Prune terminal tasks from inside the MCP session

**Files:**
- Modify: `silicorism_tools.py` (add `prune_tasks`, next to `gc_worktrees` at line 409)
- Modify: `silicorism_mcp.py:200-210` (`_gc` handler), `silicorism_mcp.py:358-369` (gc schema)
- Test: `tests/test_mcp.py`

**Interfaces:**
- Consumes: `db.connect`, `db.immediate` (`db.py`), statuses `("pending","processing","completed","failed")` — `db.py:18`.
- Produces: `silicorism_tools.prune_tasks(conn) -> dict` returning `{"deleted": <int>}`;
  reached over MCP as `silicorism_gc(tasks=true)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp.py`:

```python
def test_gc_prunes_terminal_tasks_but_keeps_live_ones(tmp_path):
    """A dead pipeline must be clearable without shelling into sqlite."""
    dbp = str(tmp_path / "prune.db")
    db.init_db(dbp)
    conn = db.connect(dbp)
    done = db.add_task(conn, "pi", '{"prompt": "a"}')
    conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (done,))
    conn.commit()
    live = db.add_task(conn, "pi", '{"prompt": "b"}')
    assert silicorism_tools.prune_tasks(conn) == {"deleted": 1}
    left = [r["id"] for r in conn.execute("SELECT id FROM tasks")]
    assert left == [live]
    conn.close()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd /home/naveen/Projects/orchestrator && python -m pytest tests/test_mcp.py::test_gc_prunes_terminal_tasks_but_keeps_live_ones -q`
Expected: FAIL — `AttributeError: module 'silicorism_tools' has no attribute 'prune_tasks'`.

- [ ] **Step 3: Implement `prune_tasks`**

Add to `silicorism_tools.py` immediately after `gc_worktrees`:

```python
def prune_tasks(conn) -> dict:
    """Delete completed/failed tasks and their logs. Pending and processing rows
    are never touched, so a live pipeline cannot be pruned out from under itself.
    """
    with db.immediate(conn) as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM tasks WHERE status IN ('completed','failed')")]
        if ids:
            marks = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM execution_logs WHERE task_id IN ({marks})", ids)
            c.execute(f"DELETE FROM tasks WHERE id IN ({marks})", ids)
    return {"deleted": len(ids)}
```

- [ ] **Step 4: Wire it into the existing gc tool**

In `silicorism_mcp.py`, `_gc` becomes:

```python
def _gc(args: dict) -> str:
    """Reclaim finished worktrees; tasks=true also prunes terminal task rows."""
    dbp = _db(args)
    db.init_db(dbp)
    conn = db.connect(dbp)
    try:
        out = silicorism_tools.gc_worktrees(
            conn, dbp, failed=bool(args.get("failed")))
        if args.get("tasks"):
            out["tasks"] = silicorism_tools.prune_tasks(conn)
        return json.dumps(out)
    finally:
        conn.close()
```

and the schema at `silicorism_mcp.py:361-367` gains one property:

```python
                "tasks": {"type": "boolean",
                          "description": "also delete completed/failed task rows "
                                         "(and their logs) so a dead pipeline stops "
                                         "poisoning the wait verdict"},
```

Update that tool's `description` to: `"Garbage-collect finished worktrees; failed=true also removes quarantined ones; tasks=true prunes terminal task rows."`

- [ ] **Step 5: Run the test and the suite**

Run: `cd /home/naveen/Projects/orchestrator && python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /home/naveen/Projects/orchestrator
git add silicorism_tools.py silicorism_mcp.py tests/test_mcp.py
git commit -m "feat: prune terminal tasks via silicorism_gc(tasks=true)"
```

---

### Task 3: Narrow the MCP surface and ship the cold-session skill

**Files:**
- Modify: `silicorism_mcp.py:233-236` (node `harness` enum), `:237-241` (model description)
- Modify: `silicorism_mcp.py:31-70` (`INSTRUCTIONS`)
- Create: `skills/silicorism/SKILL.md`
- Test: `tests/test_mcp.py`

**Interfaces:**
- Consumes: the Task 1 invariant (no task row is ever `task_type == "claude"`).
- Produces: `~/.claude/skills/silicorism` symlink → `<repo>/skills/silicorism`, resolvable
  by `skills.find_skill("silicorism")` (`skills.py:27`, search dir `~/.claude/skills`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp.py`:

```python
def test_node_schema_offers_no_claude_harness():
    """A cold client must not be able to pick a harness that bills Claude."""
    tool = next(t for t in silicorism_mcp.TOOLS
                if t["name"] == "silicorism_plan_and_submit")
    node = tool["inputSchema"]["properties"]["nodes"]["items"]["properties"]
    assert node["harness"]["enum"] == ["pi", "verify"]
    assert "claude" not in silicorism_mcp.INSTRUCTIONS.lower().split("never")[0]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd /home/naveen/Projects/orchestrator && python -m pytest tests/test_mcp.py::test_node_schema_offers_no_claude_harness -q`
Expected: FAIL — `assert ['pi', 'claude', 'verify'] == ['pi', 'verify']`.

- [ ] **Step 3: Narrow the schema**

`silicorism_mcp.py:233-236` becomes:

```python
                            "harness": {"type": "string",
                                        "enum": ["pi", "verify"],
                                        "description": "'pi' runs an OSS execution "
                                        "agent; 'verify' makes this node a test gate: "
                                        "give test_command, not prompt"},
```

and the model description at `:237-241` becomes:

```python
                            "model": {"type": "string",
                                      "description": "friendly name: qwen3-coder-480b "
                                      "(build), kimi-k2.5 (review/fix), glm-5 "
                                      "(reason/scout). These resolve on the pi harness "
                                      "only. Never a Claude model."},
```

- [ ] **Step 4: Rewrite step 3 of `INSTRUCTIONS`**

In the `MASTER PLAN` clause of `INSTRUCTIONS` (`silicorism_mcp.py:38-46`), replace
"each node's model, harness, and thinking level" with "each node's model and thinking
level (harness is always `pi` for execution nodes and `verify` for gates)", and append
this sentence to the final paragraph:

```
"YOU are the orchestrator: you ask the questions, write the plan and gate the "
"results yourself. Never delegate the planning or the implementation to another "
"Claude agent or subagent — the execution nodes are pi agents on OSS models, and "
"that is the only place work runs."
```

- [ ] **Step 5: Write the skill**

Create `skills/silicorism/SKILL.md`:

```markdown
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
```

- [ ] **Step 6: Link it where Claude Code looks**

```bash
ln -sfn /home/naveen/Projects/orchestrator/skills/silicorism /home/naveen/.claude/skills/silicorism
ls -l /home/naveen/.claude/skills/silicorism
python -c "import sys; sys.path.insert(0,'/home/naveen/Projects/orchestrator'); import skills; print(skills.find_skill('silicorism'))"
```
Expected: the symlink resolves and `find_skill` prints the `SKILL.md` path.

- [ ] **Step 7: Suite green, then commit**

Run: `cd /home/naveen/Projects/orchestrator && python -m pytest -q`

```bash
cd /home/naveen/Projects/orchestrator
git add silicorism_mcp.py skills/silicorism/SKILL.md tests/test_mcp.py
git commit -m "feat: pi-only node schema and cold-session orchestrator skill"
```

---

## WORKSTREAM 2 — Splice Phase 1 (`cst-indexer.ts`)

Executed as a pi DAG: `scout` (glm-5) → `build` (qwen3-coder-480b) → `review` (kimi-k2.5)
→ `gate` (verify). Node prompts carry the full spec below verbatim.

### Task 4: Recon brief

**Files:**
- Create: `/home/naveen/Projects/splice/NOTES-phase1.md`

- [ ] Record, with file:line citations, the exact `web-tree-sitter` signatures from
  `node_modules/web-tree-sitter/web-tree-sitter.d.ts` (`Parser.init` at `:160`,
  `setLanguage` at `:176`, `Language.load` at `:317`, `Query`, and the Node accessors),
  the `fnv1a` implementation in `~/.pi/agent/extensions/hashline.ts`, the tabs/no-parameter-
  properties style of `~/.pi/agent/extensions/lsp.ts`, and the absolute paths of the four
  grammar `.wasm` files.
- [ ] Acceptance: `NOTES-phase1.md` exists, every API claim cites a line, no other file changed.

### Task 5: Build the indexer

**Files:**
- Create: `/home/naveen/Projects/splice/src/cst-indexer.ts`
- Create: `/home/naveen/Projects/splice/test/cst-indexer.test.ts`
- Delete: `/home/naveen/Projects/splice/test/scaffold.test.ts`

**Interfaces produced (Phase 2 depends on these names):**
- `indexFile(filePath: string, source: string): Promise<FileIndex | null>` — null on an
  unsupported extension.
- `resolve(index: FileIndex, anchor: string): { ok: true; node: IndexedNode } | { ok: false; reason: "unknown-id" } | { ok: false; reason: "stale"; currentAnchor: string }`
- `renderScopeMap(index: FileIndex): string`
- `IndexedNode` carries `{ anchor, path, hash, kind, signature, startIndex, endIndex, startRow, endRow, depth, summary }`.

- [ ] Lazy `Parser.init()` singleton; grammar `.wasm` paths resolved with
  `createRequire(import.meta.url).resolve(...)`; languages cached per extension for
  `.ts`, `.tsx`, `.js`, `.jsx`, `.py`.
- [ ] Structural nodes only: classes, functions, methods, interfaces, type aliases, and
  top-level exported const arrow functions.
- [ ] **Hybrid `#Node-ID`** — canonical dotted path (`Pay.processPayment`), `#<ordinal>`
  1-based document-order suffix applied to the whole colliding set on duplicates, and a
  4-hex FNV-1a hash of the node's exact source slice. Anchor renders as
  `#Pay.processPayment@a3f1`.
- [ ] `resolve` distinguishes `unknown-id` from `stale` and never accepts a hash mismatch.
- [ ] SSIRM summary via tree-sitter Queries, not regex: `reads` (`this.<prop>` /
  `self.<prop>` reads), `mutates` (assignment targets), `calls` (callee text); each list
  sorted, capped at 6 with a `+N` suffix.
- [ ] `renderScopeMap` emits one line per node, two-space indent per depth:
  `#Pay.processPayment@a3f1  L2-5  async method processPayment(id: string)  /* reads [this.db] mutates [this.status] calls [this.db.charge] */`,
  signature = source up to the body start, whitespace-collapsed, truncated at 100 chars,
  empty summary segments omitted entirely.
- [ ] Tests cover: (a) sibling stability under an unrelated edit, (b) self-hash changes on
  own-body edit, (c) insertion leaves existing anchors untouched, (d) collision ordinals
  stable across an unrelated edit, (e) `unknown-id` vs `stale` with `currentAnchor`,
  (f) reads/mutates summary exactness, (g) Python `self.` summaries, (h) `.yaml` → null.
- [ ] Acceptance: pasted real output of `npm run typecheck` and `npm test`, both clean, no
  `any` casts used to silence the typechecker.

### Task 6: Review and fix

- [ ] Verify every `web-tree-sitter` call against the `.d.ts` — an invented signature is the
  highest-severity defect. Check the stability contract holes, `resolve`'s two miss
  reasons, coverage of (a)-(h), speculative abstractions to delete, and leaked tree
  objects / re-inits.
- [ ] Scope: `src/cst-indexer.ts` and `test/cst-indexer.test.ts` only. No mutation, VFS or
  LSP code may appear — those are Phases 2 and 3.
- [ ] Acceptance: numbered defect list with fixed/left status plus pasted test output.

### Task 7: Gate

- [ ] `harness: "verify"`, `test_command`:
  `cd /home/naveen/Projects/splice && npm run typecheck && npm test`

---

## Self-Review

**Spec coverage:** pi-only routing → Task 1 + Task 3; prune path → Task 2; cold-session
invocability → Task 3; Splice Phase 1 → Tasks 4-7. No requirement is unassigned.

**Ordering note:** Workstream 1 lands before Workstream 2 is submitted, because Task 1 is
what makes the Workstream 2 DAG runnable at all — the previous submission died on
`There's an issue with the selected model (glm-5)` from a `claude` harness node.

**Type consistency:** `indexFile` / `resolve` / `renderScopeMap` / `IndexedNode` /
`FileIndex` are used under those exact names in Tasks 5, 6 and by Phase 2.
`prune_tasks(conn) -> {"deleted": int}` is used under that name in Task 2's test, its
implementation and the `_gc` handler.
