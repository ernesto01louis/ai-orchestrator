import { cn } from "@/lib/cn"
import type { BadgeTone } from "./ui/Badge"

interface LiveDotProps {
  tone?: BadgeTone
  /** Whether the dot pulses. Default true. */
  animated?: boolean
  className?: string
}

const TONE_COLOR: Record<BadgeTone, string> = {
  ok: "bg-ok text-ok",
  warn: "bg-warn text-warn",
  err: "bg-err text-err",
  info: "bg-info text-info",
  accent: "bg-accent text-accent",
  muted: "bg-fg-2 text-fg-2",
}

/**
 * LiveDot — 7px circle with optional pulse-ring animation.
 *
 * Used for `/ws connected`, health-pill statuses, and active-run
 * indicators. The `text-` colour is what `pulse-ring` reads from
 * `currentColor`.
 */
export function LiveDot({ tone = "ok", animated = true, className }: LiveDotProps) {
  return (
    <span
      className={cn(
        "inline-block size-[7px] rounded-full",
        TONE_COLOR[tone],
        animated && "pulse",
        className,
      )}
      aria-hidden
    />
  )
}
