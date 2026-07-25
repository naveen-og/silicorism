// autoexit — run the full pi TUI for exactly one task, then exit.
//
// Loaded by silicorism worker panes (`pi -e autoexit.ts "<prompt>"`) so the
// pane shows the real interactive TUI while the orchestrator still gets a
// deterministic exit code + clean artifact:
//   - on agent_settled: write the last assistant text to $SILICORISM_ARTIFACT
//     (if set) and exit pi — 0 when the run produced assistant output, 1 when
//     it errored/produced nothing.
// Without SILICORISM_ARTIFACT the extension is inert (safe to load manually).
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import * as fs from "node:fs";

export default function (pi: ExtensionAPI) {
  const artifactPath = process.env.SILICORISM_ARTIFACT;
  if (!artifactPath) return; // interactive human session: do nothing

  let lastAssistantText = "";

  pi.on("agent_end", async (event) => {
    for (const m of event.messages ?? []) {
      if (m.role !== "assistant") continue;
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
    const ok = lastAssistantText.length > 0;
    try {
      fs.writeFileSync(artifactPath, lastAssistantText || "(no output)");
    } catch {
      /* artifact is best-effort; the exit code is the contract */
    }
    // Give the TUI a beat to paint the final frame, then exit the process —
    // the pane's sentinel wrapper captures this as the task's exit code.
    setTimeout(() => process.exit(ok ? 0 : 1), 300);
  });
}
