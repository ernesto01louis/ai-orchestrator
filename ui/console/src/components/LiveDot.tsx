import { cn } from "@/lib/utils";

const TONES: Record<string, string> = {
  ok:     "bg-ok",
  warn:   "bg-warn",
  err:    "bg-err",
  info:   "bg-info",
  accent: "bg-accent",
};
const TEXT_TONES: Record<string, string> = {
  ok: "text-ok", warn: "text-warn", err: "text-err", info: "text-info", accent: "text-accent",
};

export function LiveDot({
  tone = "ok",
  animated = true,
}: { tone?: keyof typeof TONES; animated?: boolean }) {
  return (
    <span
      className={cn(
        "inline-block w-[7px] h-[7px] rounded-full",
        TONES[tone],
        TEXT_TONES[tone],
        animated && "animate-pulse-ring",
      )}
    />
  );
}
