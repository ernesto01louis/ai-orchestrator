import { Search } from "lucide-react"
import { useQuery } from "@tanstack/react-query"
import { LiveDot } from "@/components/LiveDot"
import { apiGetHealth } from "@/lib/api"

interface TopbarProps {
  title: string
  subtitle?: string
  /** Right-aligned slot for page-specific actions. */
  actions?: React.ReactNode
  onCmdK?: () => void
}

/**
 * Topbar — page title + monospace subtitle + ⌘K trigger + health pill.
 *
 * The health pill polls `/health` every 5s. The cmd-K trigger is a
 * placeholder until the command palette lands; it's a normal button so
 * keyboard hints work today.
 */
export function Topbar({ title, subtitle, actions, onCmdK }: TopbarProps) {
  const { data: health } = useQuery({
    queryKey: ["health"],
    queryFn: apiGetHealth,
    refetchInterval: 5000,
  })

  return (
    <header className="flex min-h-[52px] items-center gap-4 border-b border-line-soft bg-bg-0 px-[18px] py-2.5">
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2.5">
          <h1 className="m-0 text-sm font-semibold tracking-[-0.1px] text-fg-0">
            {title}
          </h1>
          {subtitle && (
            <span className="font-mono text-[11px] text-fg-3">{subtitle}</span>
          )}
        </div>
      </div>

      <button
        onClick={onCmdK}
        className="flex min-w-[220px] cursor-pointer items-center gap-2 rounded-sm border border-line-soft bg-bg-1 px-2 py-1.5 pl-2.5 text-[11.5px] text-fg-2"
      >
        <Search size={12} />
        <span>Search runs, campaigns…</span>
        <span className="flex-1" />
        <span className="kbd">⌘K</span>
      </button>

      <div className="flex items-center gap-2 rounded-sm border border-line-soft bg-bg-1 px-2.5 py-1.5 text-[11px]">
        <LiveDot tone={health?.orchestrator === "ok" ? "ok" : "err"} />
        <span className="font-mono text-fg-1">orchestrator</span>
        <span className="text-fg-3">·</span>
        <LiveDot
          tone={health?.ollama === "ok" ? "ok" : "err"}
          animated={false}
        />
        <span className="font-mono text-fg-1">ollama</span>
        <span className="text-fg-3">·</span>
        <LiveDot
          tone={health?.hindsight === "ok" ? "ok" : "err"}
          animated={false}
        />
        <span className="font-mono text-fg-1">hindsight</span>
      </div>

      {actions}
    </header>
  )
}
