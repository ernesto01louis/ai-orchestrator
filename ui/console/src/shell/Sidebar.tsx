import {
  Activity,
  Box,
  Brain,
  ChevronDown,
  Hand,
  Layers,
  Settings,
  Terminal as TerminalIcon,
  type LucideIcon,
} from "lucide-react"
import { NavLink } from "react-router-dom"
import { cn } from "@/lib/cn"
import { LiveDot } from "@/components/LiveDot"

interface NavEntry {
  path: string
  label: string
  Icon: LucideIcon
  hot: string
  /** Stub pages render the StubPage component until they're built out. */
  stub?: boolean
  /** Render the paused-runs warn badge when count > 0. */
  showPausedBadge?: boolean
}

export const NAV: NavEntry[] = [
  { path: "/", label: "Dashboard", Icon: Activity, hot: "g d" },
  { path: "/runs", label: "Runs", Icon: Layers, hot: "g r" },
  { path: "/campaigns", label: "Campaigns", Icon: Box, hot: "g c", stub: true },
  { path: "/logs", label: "Live Logs", Icon: TerminalIcon, hot: "g l", stub: true },
  { path: "/hitl", label: "HITL Console", Icon: Hand, hot: "g h", showPausedBadge: true },
  { path: "/memory", label: "Memory & Gates", Icon: Brain, hot: "g m", stub: true },
  { path: "/config", label: "Config", Icon: Settings, hot: "g s", stub: true },
]

interface SidebarProps {
  pausedCount: number
  /** Build banner / version label. */
  version?: string
}

export function Sidebar({ pausedCount, version = "dev" }: SidebarProps) {
  return (
    <aside className="flex w-[220px] shrink-0 flex-col border-r border-line-soft bg-bg-0 pb-3">
      {/* Brand */}
      <div className="flex items-center gap-2.5 border-b border-line-soft px-4 py-3.5">
        <div className="grid size-[22px] place-items-center rounded-md bg-accent-soft text-accent">
          <LogoMark />
        </div>
        <div className="flex flex-col leading-[1.1]">
          <span className="text-[13px] font-semibold text-fg-0">orchestrator</span>
          <span className="font-mono text-[10px] text-fg-3">{version}</span>
        </div>
      </div>

      {/* Env switcher placeholder — wires into /environment/{target} later */}
      <div className="px-3 pb-1.5 pt-2.5">
        <div className="flex cursor-pointer items-center justify-between rounded-sm border border-line-soft bg-bg-1 px-2.5 py-1.5">
          <div className="flex items-center gap-2">
            <LiveDot tone="ok" />
            <span className="font-mono text-[11px] text-fg-0">homelab/prod</span>
          </div>
          <ChevronDown size={12} className="text-fg-3" />
        </div>
      </div>

      {/* Nav */}
      <nav className="flex flex-col gap-px px-2 py-1.5">
        {NAV.map((item) => (
          <NavItem
            key={item.path}
            item={item}
            badge={item.showPausedBadge ? pausedCount : 0}
          />
        ))}
      </nav>

      <div className="flex-1" />

      {/* Bottom strip — /ws status + theme tip */}
      <div className="border-t border-line-soft px-3.5 py-2">
        <div className="flex items-center gap-2 text-[11px] text-fg-2">
          <LiveDot tone="ok" />
          <span className="font-mono">/ws connected</span>
        </div>
        <div className="mt-1.5 text-[10px] leading-[1.4] text-fg-3">
          theme: <span className="font-mono text-fg-2">useTheme()</span>
          <br />
          flip via{" "}
          <span className="font-mono text-fg-2">__setTheme(&quot;personal&quot;)</span>
        </div>
      </div>
    </aside>
  )
}

function NavItem({ item, badge }: { item: NavEntry; badge: number }) {
  const { Icon } = item
  return (
    <NavLink
      to={item.path}
      end={item.path === "/"}
      className={({ isActive }) =>
        cn(
          "flex items-center gap-2.5 rounded-sm px-2.5 py-1.5 text-[12.5px]",
          "border",
          isActive
            ? "border-line-soft bg-bg-2 text-fg-0"
            : "border-transparent text-fg-1 hover:bg-bg-1",
        )
      }
    >
      {({ isActive }) => (
        <>
          <Icon
            size={14}
            className={isActive ? "text-accent" : "text-fg-2"}
          />
          <span className="flex-1">{item.label}</span>
          {badge > 0 ? (
            <span className="rounded-[3px] border border-warn bg-warn-soft px-[5px] py-0.5 font-mono text-[10px] leading-none text-warn">
              {badge}
            </span>
          ) : item.stub ? (
            <span className="font-mono text-[9.5px] tracking-[0.4px] text-fg-3">·</span>
          ) : null}
          <span className={cn("kbd text-[9.5px]", !isActive && "opacity-55")}>
            {item.hot}
          </span>
        </>
      )}
    </NavLink>
  )
}

function LogoMark() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="9" />
      <path d="M12 3v18M3 12h18" />
      <circle cx="12" cy="12" r="3" fill="currentColor" stroke="none" />
    </svg>
  )
}
