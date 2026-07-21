// herdr — native Pi extension exposing the orchestrator to a master pi agent.
//
// Thin wrapper: all logic lives in `python cli.py` / herdr_tools.py, so the
// same workflow runs from pi, claude, or a bare shell. Registers both LLM
// tools (registerTool) and slash-command aliases (registerCommand) — the
// commands need no TypeBox and are the proven fallback if tool registration
// varies across pi versions.
//
// UNVERIFIED IN THIS ENV: authored against the installed pi ExtensionAPI
// (registerTool/registerCommand/pi.exec) but not smoke-tested against a live
// session. Verify with:  pi -e extensions/herdr.ts   then call the tools.
// @ts-nocheck
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CLI = path.join(HERE, "..", "cli.py");
const DB = process.env.HERDR_DB || path.join(HERE, "..", "orch.db");

function slug(text: string): string {
  return (text || "feature").toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "").slice(0, 32) || "feature";
}

export default function (pi: any) {
  // Run `python cli.py <args>`; returns combined stdout/stderr.
  const cli = async (args: string[], cwd = process.cwd()) => {
    const res = await pi.exec("python", [CLI, ...args], { timeout: 60000, cwd });
    return (res.stdout + (res.stderr ? `\n${res.stderr}` : "")).trim();
  };
  const ok = (text: string) => ({ content: [{ type: "text", text }] });
  const err = (text: string) => ({ content: [{ type: "text", text }], isError: true });

  // --- LLM-callable tools ---------------------------------------------------
  const tools = [
    {
      name: "herdr_init_pipeline",
      label: "herdr: init pipeline",
      description: "Build and submit the 5-task DAG (worktree, scout, builder, "
        + "fixer, cleanup) to the orchestrator DB for a feature request.",
      parameters: {
        type: "object",
        properties: {
          prompt: { type: "string", description: "What to build" },
          base_branch: { type: "string", description: "Base branch", default: "main" },
          name: { type: "string", description: "Feature/branch name (optional)" },
        },
        required: ["prompt"],
      },
      execute: async (_id: string, p: any) => {
        try {
          const name = p.name || slug(p.prompt);
          const out = await cli(["submit-feature", "--db", DB, "--name", name,
            "--prompt", p.prompt, "--base", p.base_branch || "main"]);
          return ok(out);
        } catch (e: any) { return err(String(e?.message ?? e)); }
      },
    },
    {
      name: "herdr_start_workers",
      label: "herdr: start workers",
      description: "Launch N detached workers that run pi/claude tasks live in "
        + "tmux panes (HERDR_NATIVE) until the queue drains.",
      parameters: {
        type: "object",
        properties: { count: { type: "number", description: "Worker count", default: 3 } },
      },
      execute: async (_id: string, p: any) => {
        try {
          const n = String(p.count ?? 3);
          // detach so the tool returns immediately; workers keep running.
          const out = await pi.exec("bash", ["-lc",
            `HERDR_NATIVE=1 nohup python ${CLI} run --db ${DB} --workers ${n} `
            + `--drain >/tmp/herdr-workers.log 2>&1 & echo "started ${n} workers"`],
            { timeout: 15000 });
          return ok((out.stdout || "").trim());
        } catch (e: any) { return err(String(e?.message ?? e)); }
      },
    },
    {
      name: "herdr_get_status",
      label: "herdr: status",
      description: "Live DAG execution status (task counts, agents) and recent "
        + "inter-agent P2P messages.",
      parameters: { type: "object", properties: {} },
      execute: async () => {
        try { return ok(await cli(["status", "--db", DB])); }
        catch (e: any) { return err(String(e?.message ?? e)); }
      },
    },
    {
      name: "herdr_clean_worktrees",
      label: "herdr: gc worktrees",
      description: "Run garbage collection on finished worktrees (add failed=true "
        + "to also remove quarantined ones).",
      parameters: {
        type: "object",
        properties: { failed: { type: "boolean", description: "Also clear quarantined" } },
      },
      execute: async (_id: string, p: any) => {
        try {
          const args = ["gc", "--db", DB];
          if (p?.failed) args.push("--failed");
          return ok(await cli(args));
        } catch (e: any) { return err(String(e?.message ?? e)); }
      },
    },
  ];

  for (const t of tools) {
    try { pi.registerTool(t); } catch { /* older pi: commands below still work */ }
  }

  // --- slash-command aliases (proven registerCommand API) -------------------
  pi.registerCommand("herdr-init", {
    description: "herdr: submit a feature pipeline — /herdr-init <prompt>",
    handler: async (args: string, ctx: any) => {
      const text = (args || "").trim();
      if (!text) return ctx.ui.notify("usage: /herdr-init <prompt>", "error");
      const out = await cli(["submit-feature", "--db", DB, "--name", slug(text),
        "--prompt", text]);
      pi.sendMessage({ customType: "herdr", content: out, display: true },
        { deliverAs: "followUp" });
    },
  });
  pi.registerCommand("herdr-status", {
    description: "herdr: DAG + P2P status snapshot",
    handler: async (_a: string, ctx: any) =>
      pi.sendMessage({ customType: "herdr", content: await cli(["status", "--db", DB]),
        display: true }, { deliverAs: "followUp" }),
  });
  pi.registerCommand("herdr-clean", {
    description: "herdr: gc finished worktrees (--failed for quarantined)",
    handler: async (args: string, ctx: any) => {
      const a = ["gc", "--db", DB];
      if ((args || "").includes("--failed")) a.push("--failed");
      ctx.ui.notify(await cli(a), "info");
    },
  });
}
