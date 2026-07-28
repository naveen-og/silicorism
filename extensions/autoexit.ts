// autoexit — run the full pi TUI for exactly one task, then exit.
//
// Loaded by silicorism worker panes (`pi -e autoexit.ts "<prompt>"`) so the
// pane shows the real interactive TUI while the orchestrator still gets a
// deterministic exit code + clean artifact:
//   - on agent_settled: write the last assistant text to $SILICORISM_ARTIFACT
//     (if set) and exit pi — 0 when the run finished, 1 when it errored or
//     was aborted.
// Without SILICORISM_ARTIFACT the extension is inert (safe to load manually).
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import * as fs from "node:fs";

export default function (pi: ExtensionAPI) {
  const artifactPath = process.env.SILICORISM_ARTIFACT;
  if (!artifactPath) return; // interactive human session: do nothing

  let lastAssistantText = "";
  // Verdict comes from how the run stopped, never from whether it chattered:
  // a turn that ends "thinking + toolCall -> toolResult -> thinking" is a
  // completed run with no final text, and exiting 1 on it made the worker
  // fail and requeue tasks whose work was already on disk.
  let lastStopReason = "";

  pi.on("agent_end", async (event) => {
    for (const m of event.messages ?? []) {
      if (m.role !== "assistant") continue;
      lastStopReason = (m as any).stopReason ?? "";
      const parts = Array.isArray(m.content) ? m.content : [];
      const text = parts
        .filter((c: any) => c.type === "text" && c.text)
        .map((c: any) => c.text)
        .join("\n")
        .trim();
      if (text) lastAssistantText = text;
    }
  });

  pi.on("agent_settled", async () => {
    const ok = lastStopReason !== "" && lastStopReason !== "error"
      && lastStopReason !== "aborted";
    try {
      // Nothing is written when the run produced no text, so the worker falls
      // through to the pane log tail; a placeholder would mask it.
      if (lastAssistantText) fs.writeFileSync(artifactPath, lastAssistantText);
    } catch {
      /* artifact is best-effort; the exit code is the contract */
    }
    // Give the TUI a beat to paint the final frame, then exit the process —
    // the pane's sentinel wrapper captures this as the task's exit code.
    setTimeout(() => process.exit(ok ? 0 : 1), 300);
  });
}
