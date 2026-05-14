import { Search } from "lucide-react";
import { LiveDot } from "@/components/LiveDot";
import { useHealth } from "@/lib/queries";
import type { ReactNode } from "react";

interface Props {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  onCmdK?: () => void;
}

export function Topbar({ title, subtitle, actions, onCmdK }: Props) {
  const { data: health } = useHealth();
  return (
    <header className="flex items-center gap-4 px-[18px] py-2.5 border-b border-line-soft bg-bg-0 min-h-[52px]">
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-2.5">
          <h1 className="m-0 text-sm font-semibold text-fg-0 tracking-tight">{title}</h1>
          {subtitle && <span className="mono text-[11px] text-fg-3 truncate">{subtitle}</span>}
        </div>
      </div>

      <button
        type="button"
        onClick={onCmdK}
        className="flex items-center gap-2 px-2.5 py-1 pr-2 bg-bg-1 border border-line-soft rounded-sm text-fg-2 text-[11.5px] cursor-pointer min-w-[220px] hover:bg-bg-2"
      >
        <Search size={12} />
        <span>Search runs, campaigns…</span>
        <span className="flex-1" />
        <span className="kbd">⌘K</span>
      </button>

      <div className="flex items-center gap-2 px-2.5 py-1 bg-bg-1 border border-line-soft rounded-sm text-[11px]">
        <LiveDot tone={health?.orchestrator === "ok" ? "ok" : "err"} />
        <span className="mono text-fg-1">orchestrator</span>
        <span className="text-fg-3">·</span>
        <LiveDot tone={health?.ollama === "ok" ? "ok" : "err"} animated={false} />
        <span className="mono text-fg-1">ollama</span>
        <span className="text-fg-3">·</span>
        <LiveDot tone={health?.hindsight === "ok" ? "ok" : "err"} animated={false} />
        <span className="mono text-fg-1">hindsight</span>
      </div>

      {actions}
    </header>
  );
}
