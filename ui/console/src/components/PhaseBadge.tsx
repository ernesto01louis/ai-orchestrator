import { Badge, type Tone } from "@/components/ui/badge";
import type { Phase } from "@/lib/types";

const PHASE_TONE: Record<Phase, Tone> = {
  planner: "info",
  generator: "accent",
  judge: "warn",
  optimizer: "ok",
  post_planner: "info",
  complete: "ok",
  failed: "err",
};

export function PhaseBadge({ phase }: { phase: Phase | string }) {
  const tone = (PHASE_TONE as Record<string, Tone>)[phase] ?? "muted";
  return <Badge tone={tone} mono>{phase}</Badge>;
}

export { PHASE_TONE };
