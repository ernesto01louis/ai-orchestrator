import { cn } from "@/lib/cn"

interface ConfidenceBarProps {
  /** Planner self-reported confidence in [0, 1]. */
  value: number
  /** Bar width in px. Default 80. */
  width?: number
  className?: string
}

/**
 * ConfidenceBar — Phase 3.2 SmartPause planner-confidence visualisation.
 *
 * Same shape as ScoreBar but with a different threshold scheme: ≥0.7 ok
 * (would not trip SmartPause's default threshold), 0.4–0.7 warn, <0.4
 * err. The trailing tabular-num value uses the same tone colour.
 */
export function ConfidenceBar({ value, width = 80, className }: ConfidenceBarProps) {
  const pct = Math.max(0, Math.min(1, value)) * 100
  const tone =
    value >= 0.7
      ? { fill: "bg-ok", text: "text-ok" }
      : value >= 0.4
      ? { fill: "bg-warn", text: "text-warn" }
      : { fill: "bg-err", text: "text-err" }
  return (
    <div className={cn("inline-flex items-center gap-1.5", className)}>
      <div
        className="h-1 overflow-hidden rounded-sm bg-bg-3"
        style={{ width }}
      >
        <div
          className={cn("h-full", tone.fill)}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={cn("num text-[10.5px]", tone.text)}>
        {value.toFixed(2)}
      </span>
    </div>
  )
}
