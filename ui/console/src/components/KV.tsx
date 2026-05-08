import type { ReactNode } from "react"
import { cn } from "@/lib/cn"

interface KVProps {
  k: ReactNode
  v: ReactNode
  /** Render the value in mono font. Default true. */
  mono?: boolean
}

/**
 * KV — uppercase label : mono value, divided by a dashed hairline.
 *
 * Used in provenance side-panels, run detail, etc.
 */
export function KV({ k, v, mono = true }: KVProps) {
  return (
    <div className="flex items-center justify-between border-b border-dashed border-line-soft py-[5px]">
      <span className="text-[11px] uppercase tracking-[0.4px] text-fg-2">{k}</span>
      <span className={cn("text-xs text-fg-0", mono && "font-mono")}>{v}</span>
    </div>
  )
}
