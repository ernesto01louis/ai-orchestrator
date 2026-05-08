import { Badge, type BadgeTone } from "./ui/Badge"
import type { RunPhase } from "@/lib/types"

const PHASE_TONE: Record<RunPhase, BadgeTone> = {
  planner: "info",
  generator: "accent",
  judge: "warn",
  optimizer: "ok",
  post_planner: "info",
  complete: "ok",
  failed: "err",
  pending: "muted",
}

/** Mono-cased pill rendering a `RunPhase` with the canonical phase tone. */
export function PhaseBadge({ phase }: { phase: RunPhase }) {
  return (
    <Badge tone={PHASE_TONE[phase] ?? "muted"} mono>
      {phase}
    </Badge>
  )
}

export { PHASE_TONE }
