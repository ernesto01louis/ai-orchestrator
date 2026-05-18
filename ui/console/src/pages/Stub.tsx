import { Box, Brain, Settings, Terminal as TerminalIcon, type LucideIcon } from "lucide-react";
import { Card } from "@/components/ui/card";

interface Blueprint {
  Icon: LucideIcon;
  label: string;
  lines: string[];
  endpoints: string[];
}

const BLUEPRINTS: Record<string, Blueprint> = {
  campaigns: {
    Icon: Box,
    label: "Campaigns",
    lines: [
      "Campaign list, hitl_mode badge column, status, budget bar, children counts.",
      "Click → detail with param grid table, run-tree visualization (live phases),",
      "evidence-bundle download (RO-Crate ZIP) + verify, NoteDiscovery research trace.",
    ],
    endpoints: [
      "GET /campaigns",
      "GET /campaigns/{id}/tree",
      "GET /campaigns/{id}/budget",
      "GET /campaigns/{id}/evidence",
      "GET /campaigns/{id}/evidence.crate.zip",
    ],
  },
  logs: {
    Icon: TerminalIcon,
    label: "Live Logs",
    lines: [
      "Full-bleed terminal tailing /ws across all runs.",
      "Filterable by run_id, phase, log level. Pause/resume scroll-lock.",
      "Search-in-buffer (⌘F) + jump-to-run-detail on click.",
    ],
    endpoints: ["WS /ws (global)"],
  },
  memory: {
    Icon: Brain,
    label: "Memory & Gates",
    lines: [
      "Search box for /memory/search?q=…",
      "Model stats table from /model-stats (calls, p95, error rate).",
      "Gates list from /gates with toggle per-gate (POST /gates/{name}/{enable|disable}).",
    ],
    endpoints: ["GET /memory", "GET /memory/search?q=...", "GET /model-stats", "GET /gates"],
  },
  config: {
    Icon: Settings,
    label: "Config",
    lines: [
      "Read-only display of /health and feature flags.",
      "Postgres / Redis / OTEL / Budget / Sky / SmartPause / HITL / NoteDiscovery enabled states.",
      "Environment switcher pulls from /environment/{target}.",
    ],
    endpoints: ["GET /health", "GET /metrics", "GET /environment/{target}"],
  },
};

export function Stub({ routeId }: { routeId: string }) {
  const bp = BLUEPRINTS[routeId];
  if (!bp) return null;
  return (
    <div className="p-8 max-w-[720px]">
      <div className="flex items-center gap-3 mb-3.5">
        <div className="w-[34px] h-[34px] rounded-md bg-bg-2 border border-line-soft text-accent grid place-items-center">
          <bp.Icon size={16} />
        </div>
        <div>
          <h2 className="m-0 text-base font-semibold text-fg-0">{bp.label}</h2>
          <div className="text-[11px] text-fg-3 mt-0.5">
            Stub for follow-up · ask the operator to design this page next.
          </div>
        </div>
      </div>

      <Card title="Planned content">
        <ul className="m-0 pl-4 text-fg-1 text-[12.5px] leading-relaxed">
          {bp.lines.map((l, i) => <li key={i}>{l}</li>)}
        </ul>
      </Card>

      <div className="h-3" />

      <Card title="Endpoints">
        <div className="flex flex-col gap-1">
          {bp.endpoints.map((e) => (
            <div key={e} className="mono text-xs text-fg-1 px-2 py-1 bg-bg-2 border border-line-soft rounded-[4px]">
              {e}
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
