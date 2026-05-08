import { useQuery } from "@tanstack/react-query"
import { Outlet, Route, Routes, useLocation } from "react-router-dom"
import { Sidebar, NAV } from "@/shell/Sidebar"
import { Topbar } from "@/shell/Topbar"
import { DashboardPage } from "@/pages/Dashboard"
import { RunsPage } from "@/pages/Runs"
import { HitlPage } from "@/pages/Hitl"
import {
  CampaignsStubPage,
  ConfigStubPage,
  LogsStubPage,
  MemoryStubPage,
} from "@/pages/Stubs"
import { apiGetRuns } from "@/lib/api"

export const VERSION_LABEL = "v0.4.0-phase2.6-rc"

/**
 * App shell — sidebar + topbar + outlet for the active route.
 *
 * The topbar's title/subtitle is derived from the current pathname so
 * each page doesn't have to re-implement it. Add a row in `TITLE_MAP`
 * when a new route lands.
 */
function AppShell() {
  const { data: runs = [] } = useQuery({
    queryKey: ["runs"],
    queryFn: apiGetRuns,
    refetchInterval: 3000,
  })
  const pausedCount = runs.filter((r) => r.paused).length

  const location = useLocation()
  const { title, subtitle } = topbarFromPath(location.pathname, runs.length, pausedCount)

  return (
    <div
      className="flex overflow-hidden bg-bg-0"
      style={{ height: "100vh" }}
    >
      <Sidebar pausedCount={pausedCount} version={VERSION_LABEL} />
      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <Topbar title={title} subtitle={subtitle} />
        <div className="flex-1 overflow-auto">
          <Outlet />
        </div>
      </main>
    </div>
  )
}

function topbarFromPath(
  pathname: string,
  totalRuns: number,
  paused: number,
): { title: string; subtitle?: string } {
  if (pathname === "/" || pathname === "") {
    return { title: "Dashboard", subtitle: "homelab/prod · last 5m" }
  }
  if (pathname.startsWith("/runs")) {
    return { title: "Runs", subtitle: `${totalRuns} total · ${paused} paused` }
  }
  if (pathname.startsWith("/hitl")) {
    return { title: "HITL Console", subtitle: `${paused} awaiting` }
  }
  const entry = NAV.find((n) => pathname.startsWith(n.path) && n.path !== "/")
  return {
    title: entry?.label ?? "—",
    subtitle: entry?.stub ? "stub · ask for this page next" : undefined,
  }
}

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<DashboardPage />} />
        <Route path="runs" element={<RunsPage />} />
        <Route path="runs/:runId" element={<RunsPage />} />
        <Route path="campaigns" element={<CampaignsStubPage />} />
        <Route path="logs" element={<LogsStubPage />} />
        <Route path="hitl" element={<HitlPage />} />
        <Route path="memory" element={<MemoryStubPage />} />
        <Route path="config" element={<ConfigStubPage />} />
      </Route>
    </Routes>
  )
}
