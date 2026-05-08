import type { HTMLAttributes, ReactNode } from "react"
import { cn } from "@/lib/cn"

export type BadgeTone = "ok" | "warn" | "err" | "info" | "accent" | "muted"

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone
  /** Render a small leading colored dot. */
  dot?: boolean
  /** Use mono font (default for ids / phase names). */
  mono?: boolean
  children?: ReactNode
}

/**
 * Badge — pill-shaped status indicator.
 *
 * Tone palette mirrors the design tokens: each tone has a `text`,
 * `bg-soft`, and `border` colour pulled from CSS vars.
 */
export function Badge({
  tone = "muted",
  dot,
  mono,
  className,
  children,
  ...props
}: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-1.5 py-[3px] text-[11px] leading-none",
        TONE_CLASS[tone],
        mono && "font-mono",
        className,
      )}
      {...props}
    >
      {dot && (
        <span
          className={cn("size-[6px] rounded-full", DOT_CLASS[tone])}
          aria-hidden
        />
      )}
      {children}
    </span>
  )
}

// Tailwind class lookup — keep these strings literal so the JIT picks
// them up. Don't compose them dynamically.
const TONE_CLASS: Record<BadgeTone, string> = {
  ok: "text-ok bg-ok-soft border-ok/40",
  warn: "text-warn bg-warn-soft border-warn/40",
  err: "text-err bg-err-soft border-err/40",
  info: "text-info bg-info-soft border-info/40",
  accent: "text-accent bg-accent-soft border-accent-line",
  muted: "text-fg-2 bg-bg-2 border-line",
}

const DOT_CLASS: Record<BadgeTone, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  err: "bg-err",
  info: "bg-info",
  accent: "bg-accent",
  muted: "bg-fg-2",
}
