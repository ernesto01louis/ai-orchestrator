import { Activity } from "lucide-react"
import { StubPage } from "./StubPage"

/**
 * Dashboard — placeholder for PR 2.6 (a).
 *
 * The real page lands in PR 2.6 (b) and follows the prototype layout:
 * metric strip → paused-runs strip → 1.4fr/1fr split (Active runs |
 * Budget+Recent campaigns) → live /ws preview.
 */
export function DashboardPage() {
  return (
    <StubPage
      title="Dashboard"
      Icon={Activity}
      planned={[
        "Metric strip (LLM rate, p95 latency, Ollama queue, GPU util) with sparklines.",
        "Paused-runs strip — surfaces every SmartPause + HITL pause needing operator action.",
        "1.4fr/1fr split — Active runs table | Budget · 24h + Recent campaigns.",
        "Live · /ws preview card tailing the last 8 messages, click → Live Logs.",
      ]}
      endpoints={[
        "GET /health (5s)",
        "GET /metrics_console (5s)",
        "GET /runs (3s)",
        "GET /campaigns (5s)",
        "WS /ws",
      ]}
    />
  )
}
