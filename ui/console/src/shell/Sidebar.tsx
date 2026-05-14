import { Link, useLocation, useNavigate } from "react-router-dom";
import {
  Activity, Box, Brain, ChevronDown, Hand, Layers,
  Settings, Terminal as TerminalIcon, Circle,
} from "lucide-react";
import { LiveDot } from "@/components/LiveDot";
import { useRuns } from "@/lib/queries";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/dashboard",  label: "Dashboard",      Icon: Activity,     hot: "g d" },
  { to: "/runs",       label: "Runs",           Icon: Layers,       hot: "g r" },
  { to: "/campaigns",  label: "Campaigns",      Icon: Box,          hot: "g c", stub: true },
  { to: "/logs",       label: "Live Logs",      Icon: TerminalIcon, hot: "g l", stub: true },
  { to: "/hitl",       label: "HITL Console",   Icon: Hand,         hot: "g h", badgeKey: "paused" as const },
  { to: "/memory",     label: "Memory & Gates", Icon: Brain,        hot: "g m", stub: true },
  { to: "/config",     label: "Config",         Icon: Settings,     hot: "g s", stub: true },
];

export function Sidebar() {
  const loc = useLocation();
  const navigate = useNavigate();
  const { data: runs = [] } = useRuns();
  const pausedCount = runs.filter((r) => r.paused).length;

  return (
    <aside className="w-[220px] flex-shrink-0 bg-bg-0 border-r border-line-soft flex flex-col pb-3">
      {/* brand */}
      <div className="flex items-center gap-2.5 px-4 py-3.5 border-b border-line-soft">
        <div className="w-[22px] h-[22px] rounded grid place-items-center bg-accent-soft text-accent">
          <Circle size={14} fill="currentColor" />
        </div>
        <div className="flex flex-col leading-tight">
          <span className="text-[13px] font-semibold text-fg-0">orchestrator</span>
          <span className="mono text-[10px] text-fg-3">v0.14.2-rc.3</span>
        </div>
      </div>

      {/* env switcher */}
      <div className="px-3 pt-2.5 pb-1.5">
        <button
          type="button"
          className="w-full flex items-center justify-between px-2.5 py-1.5 bg-bg-1 border border-line-soft rounded-sm cursor-pointer hover:bg-bg-2"
        >
          <div className="flex items-center gap-2">
            <LiveDot tone="ok" />
            <span className="mono text-[11px] text-fg-0">homelab/prod</span>
          </div>
          <ChevronDown size={12} className="text-fg-3" />
        </button>
      </div>

      {/* nav */}
      <nav className="flex flex-col px-2 py-1.5 gap-px">
        {NAV.map((item) => {
          const isActive = loc.pathname.startsWith(item.to);
          const showBadge = item.badgeKey === "paused" && pausedCount > 0;
          return (
            <button
              key={item.to}
              onClick={() => navigate(item.to)}
              className={cn(
                "flex items-center gap-2.5 px-2.5 py-1.5 rounded-sm text-left text-[12.5px] border",
                isActive
                  ? "bg-bg-2 border-line-soft text-fg-0"
                  : "bg-transparent border-transparent text-fg-1 hover:bg-bg-1",
              )}
            >
              <span className={isActive ? "text-accent" : "text-fg-2"}>
                <item.Icon size={14} />
              </span>
              <span className="flex-1">{item.label}</span>
              {showBadge && (
                <span className="mono text-[10px] leading-none px-[5px] py-0.5 rounded-[3px] bg-warn-soft text-warn border border-warn">
                  {pausedCount}
                </span>
              )}
              <span className={cn("kbd", !isActive && "opacity-55")}>{item.hot}</span>
            </button>
          );
        })}
      </nav>

      <div className="flex-1" />

      {/* footer: ws status + theme seam tip */}
      <div className="px-3.5 py-2 border-t border-line-soft">
        <div className="flex items-center gap-2 text-[11px] text-fg-2">
          <LiveDot tone="ok" />
          <span className="mono">/ws connected</span>
        </div>
        <div className="mt-1.5 text-[10px] text-fg-3 leading-snug">
          theme: <span className="mono text-fg-2">useTheme()</span><br />
          flip via <span className="mono text-fg-2">__setTheme("personal")</span>
        </div>
      </div>
    </aside>
  );
}

/** Re-export for headers that want to avoid an import roundtrip. */
export { NAV as SIDEBAR_NAV };
export const NAV_BY_PATH = Object.fromEntries(NAV.map((n) => [n.to, n] as const));
export type NavItem = (typeof NAV)[number];

// Stub Link import (kept so future inline links are easy to add)
export { Link };
