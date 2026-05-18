import * as React from "react";
import { cn } from "@/lib/utils";

export type Tone = "ok" | "warn" | "err" | "info" | "accent" | "muted";

const toneStyle: Record<Tone, { fg: string; bg: string; border: string; dot: string }> = {
  ok:     { fg: "text-ok",     bg: "bg-ok-soft",     border: "border-ok/40",     dot: "bg-ok" },
  warn:   { fg: "text-warn",   bg: "bg-warn-soft",   border: "border-warn/40",   dot: "bg-warn" },
  err:    { fg: "text-err",    bg: "bg-err-soft",    border: "border-err/40",    dot: "bg-err" },
  info:   { fg: "text-info",   bg: "bg-info-soft",   border: "border-info/40",   dot: "bg-info" },
  accent: { fg: "text-accent", bg: "bg-accent-soft", border: "border-accent/40", dot: "bg-accent" },
  muted:  { fg: "text-fg-2",   bg: "bg-bg-2",        border: "border-line",      dot: "bg-fg-2" },
};

interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
  dot?: boolean;
  mono?: boolean;
}

export function Badge({ tone = "muted", dot, mono, className, children, ...rest }: BadgeProps) {
  const t = toneStyle[tone];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-[11px] leading-none px-1.5 py-1 rounded-full border",
        t.fg, t.bg, t.border,
        mono && "mono",
        className,
      )}
      {...rest}
    >
      {dot && <span className={cn("w-1.5 h-1.5 rounded-full", t.dot)} />}
      {children}
    </span>
  );
}

export const TONE = toneStyle;
