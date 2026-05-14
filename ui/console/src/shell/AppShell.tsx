import type { ReactNode } from "react";
import { useLocation, useParams } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import { useRuns } from "@/lib/queries";

const TITLES: Record<string, string> = {
  "/dashboard": "Dashboard",
  "/runs": "Runs",
  "/hitl": "HITL Console",
  "/campaigns": "Campaigns",
  "/logs": "Live Logs",
  "/memory": "Memory & Gates",
  "/config": "Config",
};

export function AppShell({ children }: { children: ReactNode }) {
  const loc = useLocation();
  const { runId } = useParams();
  const { data: runs = [] } = useRuns();
  const pausedCount = runs.filter((r) => r.paused).length;

  // Title selection based on top-level segment.
  const seg = "/" + (loc.pathname.split("/")[1] ?? "");
  let title = TITLES[seg] ?? "—";
  let subtitle: string | undefined;

  if (seg === "/runs" && runId) { title = "Run detail"; subtitle = runId; }
  else if (seg === "/runs") subtitle = `${runs.length} total · ${pausedCount} paused`;
  else if (seg === "/hitl") subtitle = `${pausedCount} awaiting`;
  else if (seg === "/dashboard") subtitle = "homelab/prod · last 5m";

  return (
    <div className="flex h-screen overflow-hidden bg-bg-0">
      <Sidebar />
      <main className="flex-1 flex flex-col min-w-0 overflow-hidden">
        <Topbar title={title} subtitle={subtitle} />
        <div className="flex-1 overflow-auto">{children}</div>
      </main>
    </div>
  );
}
