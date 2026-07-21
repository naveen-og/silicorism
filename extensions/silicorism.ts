// silicorism — native Pi extension exposing the orchestrator to a master pi agent.
//
// Thin wrapper: all logic lives in `python cli.py` / silicorism_tools.py, so the
// same workflow runs from pi, claude, or a bare shell. Registers LLM tools
// (registerTool) and slash-command aliases (registerCommand).
//
// Verified against the installed Pi ExtensionAPI (@earendil-works/pi-coding-agent):
//   - ToolDefinition.parameters is a TypeBox schema (Type.Object), not raw JSON
//   - execute(toolCallId, params, signal, onUpdate, ctx) -> AgentToolResult
//   - AgentToolResult = { content, details } (no isError field)
//   - pi.exec(cmd, args, opts) -> { stdout, stderr, code, killed }
// Smoke-test:  pi -e extensions/silicorism.ts   then call the tools.
import type {
  ExtensionAPI,
  ExtensionCommandContext,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CLI = path.join(HERE, "..", "cli.py");
const DB = process.env.SILICORISM_DB || path.join(HERE, "..", "silicorism.db");

function slug(text: string): string {
  return (text || "feature").toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "").slice(0, 32) || "feature";
}

function text(t: string) {
  return { content: [{ type: "text" as const, text: t }], details: {} };
}

export default function (pi: ExtensionAPI) {
  // Run `python cli.py <args>`; returns combined stdout/stderr.
  const cli = async (args: string[], cwd = process.cwd()): Promise<string> => {
    const res = await pi.exec("python", [CLI, ...args], { timeout: 60000, cwd });
    return (res.stdout + (res.stderr ? `\n${res.stderr}` : "")).trim();
  };

  // --- LLM-callable tools ---------------------------------------------------
  pi.registerTool({
    name: "silicorism_plan_and_submit",
    label: "silicorism: plan and submit",
    description: "Build and submit the 5-task DAG (worktree, scout, builder, "
      + "fixer, cleanup) to the orchestrator for a feature request.",
    parameters: Type.Object({
      prompt: Type.String({ description: "What to build" }),
      base_branch: Type.Optional(Type.String({ description: "Base branch" })),
      name: Type.Optional(Type.String({ description: "Feature/branch name" })),
    }),
    execute: async (_toolCallId, params, _signal, _onUpdate, _ctx) => {
      try {
        const name = params.name || slug(params.prompt);
        return text(await cli(["submit-feature", "--db", DB, "--name", name,
          "--prompt", params.prompt, "--base", params.base_branch || "main"]));
      } catch (e: any) { return text(`Error: ${e?.message ?? e}`); }
    },
  });

  pi.registerTool({
    name: "silicorism_start_workers",
    label: "silicorism: start workers",
    description: "Launch N detached workers that run pi/claude tasks live in "
      + "tmux panes (SILICORISM_NATIVE) until the queue drains.",
    parameters: Type.Object({
      count: Type.Optional(Type.Number({ description: "Worker count" })),
    }),
    execute: async (_toolCallId, params, _signal, _onUpdate, _ctx) => {
      try {
        const n = String(params.count ?? 3);
        // detach so the tool returns immediately; workers keep running.
        const res = await pi.exec("bash", ["-lc",
          `SILICORISM_NATIVE=1 nohup python ${CLI} run --db ${DB} --workers ${n} `
          + `--drain >/tmp/silicorism-workers.log 2>&1 & echo "started ${n} workers"`],
          { timeout: 15000 });
        return text((res.stdout || "").trim());
      } catch (e: any) { return text(`Error: ${e?.message ?? e}`); }
    },
  });

  pi.registerTool({
    name: "silicorism_get_status",
    label: "silicorism: status",
    description: "Live DAG execution status (task counts, agents) and recent "
      + "inter-agent P2P messages.",
    parameters: Type.Object({}),
    execute: async () => {
      try { return text(await cli(["status", "--db", DB])); }
      catch (e: any) { return text(`Error: ${e?.message ?? e}`); }
    },
  });

  pi.registerTool({
    name: "silicorism_gc",
    label: "silicorism: gc worktrees",
    description: "Garbage-collect finished worktrees (failed=true also removes "
      + "quarantined ones).",
    parameters: Type.Object({
      failed: Type.Optional(Type.Boolean({ description: "Also clear quarantined" })),
    }),
    execute: async (_toolCallId, params, _signal, _onUpdate, _ctx) => {
      try {
        const args = ["gc", "--db", DB];
        if (params.failed) args.push("--failed");
        return text(await cli(args));
      } catch (e: any) { return text(`Error: ${e?.message ?? e}`); }
    },
  });

  // --- slash-command aliases ------------------------------------------------
  pi.registerCommand("silicorism-init", {
    description: "silicorism: submit a feature pipeline — /silicorism-init <prompt>",
    handler: async (args: string, ctx: ExtensionCommandContext) => {
      const prompt = (args || "").trim();
      if (!prompt) { ctx.ui.notify("usage: /silicorism-init <prompt>", "error"); return; }
      const out = await cli(["submit-feature", "--db", DB, "--name", slug(prompt),
        "--prompt", prompt]);
      await pi.sendMessage({ customType: "silicorism", content: out, display: true },
        { deliverAs: "followUp" });
    },
  });

  pi.registerCommand("silicorism-status", {
    description: "silicorism: DAG + P2P status snapshot",
    handler: async (_args: string, _ctx: ExtensionCommandContext) => {
      await pi.sendMessage(
        { customType: "silicorism", content: await cli(["status", "--db", DB]),
          display: true }, { deliverAs: "followUp" });
    },
  });

  pi.registerCommand("silicorism-clean", {
    description: "silicorism: gc finished worktrees (--failed for quarantined)",
    handler: async (args: string, ctx: ExtensionCommandContext) => {
      const a = ["gc", "--db", DB];
      if ((args || "").includes("--failed")) a.push("--failed");
      ctx.ui.notify(await cli(a), "info");
    },
  });
}
