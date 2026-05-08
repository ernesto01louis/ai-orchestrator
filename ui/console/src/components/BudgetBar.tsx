import { cn } from "@/lib/cn"
import type { CampaignBudget } from "@/lib/types"

interface BudgetBarProps extends CampaignBudget {
  /** Width override; default `100%`. */
  width?: number | string
  className?: string
}

const TONE_TEXT: Record<CampaignBudget["state"], string> = {
  healthy: "text-ok",
  warn: "text-warn",
  err: "text-err",
}
const TONE_BG: Record<CampaignBudget["state"], string> = {
  healthy: "bg-ok",
  warn: "bg-warn",
  err: "bg-err",
}

/**
 * BudgetBar — `$used/$total` left, percentage right (in tone colour),
 * 4px-tall fill below.
 *
 * Rendered both in the dashboard summary card and inline on each
 * campaign row in the Recent Campaigns list.
 */
export function BudgetBar({
  used,
  total,
  percentage,
  state,
  width = "100%",
  className,
}: BudgetBarProps) {
  return (
    <div
      className={cn("flex flex-col gap-1", className)}
      style={{ width: typeof width === "number" ? `${width}px` : width }}
    >
      <div className="flex justify-between text-[11px]">
        <span className="num text-fg-1">
          ${used.toFixed(2)} / ${total.toFixed(2)}
        </span>
        <span className={cn("num", TONE_TEXT[state])}>
          {percentage.toFixed(1)}%
        </span>
      </div>
      <div className="h-1 overflow-hidden rounded-sm bg-bg-3">
        <div
          className={cn("h-full", TONE_BG[state])}
          style={{ width: `${Math.min(100, percentage)}%` }}
        />
      </div>
    </div>
  )
}
