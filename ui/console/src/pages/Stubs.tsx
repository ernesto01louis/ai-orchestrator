import { Box, Brain, Settings, Terminal as TerminalIcon } from "lucide-react"
import { StubPage } from "./StubPage"

/**
 * Stub pages for the four routes designed but not yet implemented.
 *
 * Each spells out the planned content + endpoints so a future
 * implementer (or operator opening the route) sees exactly what's
 * coming next.
 */

export function CampaignsStubPage() {
  return (
    <StubPage
      title="Campaigns"
      Icon={Box}
      planned={[
        "List with hitl_mode badge column, status, budget bar, child counts.",
        "Detail: param grid table, run-tree visualisation (live phases).",
        "Phase 1.2 evidence-bundle download (RO-Crate ZIP) + verify Merkle.",
        "Phase 3.3 NoteDiscovery research trace from memory/<run>/planner_research.json.",
      ]}
      endpoints={[
        "GET /campaigns",
        "GET /campaigns/{id}/tree",
        "GET /campaigns/{id}/budget",
        "GET /campaigns/{id}/evidence",
        "GET /campaigns/{id}/evidence.crate.zip",
      ]}
    />
  )
}

export function LogsStubPage() {
  return (
    <StubPage
      title="Live Logs"
      Icon={TerminalIcon}
      planned={[
        "Full-bleed terminal tailing /ws across every run.",
        "Filterable by run_id, phase, log level. Pause / resume scroll-lock.",
        "Search-in-buffer (⌘F) + jump-to-run-detail on click.",
      ]}
      endpoints={["WS /ws (global)"]}
    />
  )
}

export function MemoryStubPage() {
  return (
    <StubPage
      title="Memory & Gates"
      Icon={Brain}
      planned={[
        "Search box for /memory/search?q=…",
        "Model stats table from /model-stats (calls, p95, error rate).",
        "Gates list from /gates with per-gate enable / disable toggle.",
      ]}
      endpoints={[
        "GET /memory",
        "GET /memory/search?q=...",
        "GET /model-stats",
        "GET /gates",
      ]}
    />
  )
}

export function ConfigStubPage() {
  return (
    <StubPage
      title="Config"
      Icon={Settings}
      planned={[
        "Read-only display of /health and feature flags.",
        "Phase 2.1–2.5 enabled states: postgres / redis / otel / budget / sky.",
        "Phase 3 enabled states: smartpause / hitl / note_discovery.",
        "Environment switcher pulls from /environment/{target}.",
      ]}
      endpoints={[
        "GET /health",
        "GET /metrics_console",
        "GET /environment/{target}",
      ]}
    />
  )
}
