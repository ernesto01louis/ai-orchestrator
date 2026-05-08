import type { BadgeTone } from "./ui/Badge"
import { cn } from "@/lib/cn"

interface ScoreBarProps {
  score: number | null | undefined
  /** Bar width in px. Default 60. */
  width?: number
}

const TONE_FILL: Record<Exclude<BadgeTone, "info" | "accent" | "muted">, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  err: "bg-err",
}

/**
 * ScoreBar — 4px-tall progress bar + tabular-num value.
 *
 * Tone derives from the score: ≥0.8 ok, ≥0.6 warn, otherwise err.
 * Renders an em-dash when score is null/undefined.
 */
export function ScoreBar({ score, width = 60 }: ScoreBarProps) {
  if (score == null) {
    return <span className="font-mono text-fg-3">—</span>
  }
  const pct = Math.max(0, Math.min(1, score)) * 100
  const tone: keyof typeof TONE_FILL =
    score >= 0.8 ? "ok" : score >= 0.6 ? "warn" : "err"
  return (
    <div className="inline-flex items-center gap-2">
      <div
        className="h-1 overflow-hidden rounded-sm bg-bg-3"
        style={{ width }}
      >
        <div
          className={cn("h-full", TONE_FILL[tone])}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="num text-[11px] text-fg-1">{score.toFixed(2)}</span>
    </div>
  )
}
