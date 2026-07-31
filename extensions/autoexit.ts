// autoexit — run the full pi TUI for exactly one task, then exit.
//
// Loaded by silicorism worker panes (`pi -e autoexit.ts "<prompt>"`) so the
// pane shows the real interactive TUI while the orchestrator still gets a
// deterministic exit code + clean artifact:
//   - on agent_settled: write the last assistant text to $SILICORISM_ARTIFACT
//     (if set), the run's token totals to $SILICORISM_USAGE (if set), and exit
//     pi — 0 when the run finished, 1 when it errored or was aborted.
// With neither variable set the extension is inert (safe to load manually).
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import * as fs from "node:fs";

export default function (pi: ExtensionAPI) {
  const artifactPath = process.env.SILICORISM_ARTIFACT;
  const usagePath = process.env.SILICORISM_USAGE;
  if (!artifactPath && !usagePath) return; // interactive human session

  let lastAssistantText = "";
  // Tokens per assistant response. agent_end hands back the conversation so
  // far, not just the new turn, so the same response arrives on every later
  // event — keyed by responseId, a re-delivery overwrites instead of adding.
  const seen = new Map<string, any>();
  let provider = "";
  let model = "";
  // Verdict comes from how the run stopped, never from whether it chattered:
  // a turn that ends "thinking + toolCall -> toolResult -> thinking" is a
  // completed run with no final text, and exiting 1 on it made the worker
  // fail and requeue tasks whose work was already on disk.
  let lastStopReason = "";

  pi.on("agent_end", async (event) => {
    const msgs = event.messages ?? [];
    for (let i = 0; i < msgs.length; i++) {
      const m = msgs[i];
      if (m.role !== "assistant") continue;
      lastStopReason = (m as any).stopReason ?? "";
      const parts = Array.isArray(m.content) ? m.content : [];
      const text = parts
        .filter((c: any) => c.type === "text" && c.text)
        .map((c: any) => c.text)
        .join("\n")
        .trim();
      if (text) lastAssistantText = text;
      const u = (m as any).usage;
      if (u) {
        // No responseId: key on position in the cumulative array, which is
        // stable across re-deliveries; a running counter would not be.
        seen.set((m as any).responseId || `#${i}`, u);
        provider = (m as any).provider || provider;
        model = (m as any).model || model;
      }
    }
  });

  pi.on("agent_settled", async () => {
    const ok = lastStopReason !== "" && lastStopReason !== "error"
      && lastStopReason !== "aborted";
    try {
      // Nothing is written when the run produced no text, so the worker falls
      // through to the pane log tail; a placeholder would mask it.
      if (artifactPath && lastAssistantText) {
        fs.writeFileSync(artifactPath, lastAssistantText);
      }
    } catch {
      /* artifact is best-effort; the exit code is the contract */
    }
    try {
      if (usagePath && seen.size) {
        const t = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
        for (const u of seen.values()) {
          t.input += u.input || 0;
          t.output += u.output || 0;
          t.cacheRead += u.cacheRead || 0;
          t.cacheWrite += u.cacheWrite || 0;
        }
        fs.writeFileSync(usagePath,
          JSON.stringify({ ...t, provider, model }));
      }
    } catch {
      /* telemetry never blocks the exit code */
    }
    // Give the TUI a beat to paint the final frame, then exit the process —
    // the pane's sentinel wrapper captures this as the task's exit code.
    setTimeout(() => process.exit(ok ? 0 : 1), 300);
  });
}
