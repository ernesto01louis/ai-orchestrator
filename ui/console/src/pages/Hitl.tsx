import { Hand } from "lucide-react"
import { StubPage } from "./StubPage"

/**
 * HITL Console — placeholder for PR 2.6 (a).
 *
 * The real page lands in PR 2.6 (b): queue + selected-run detail with
 * approve / reject / edit / skip / abort buttons posting to
 * /runs/{id}/intervene. Surfaces planner confidence (Phase 3.2) and
 * the full intervene-mode legend.
 */
export function HitlPage() {
  return (
    <StubPage
      title="HITL Console"
      Icon={Hand}
      planned={[
        "Queue card listing every paused run with mode + phase + waiting time.",
        "Selected-run detail with planner output (read-only or editable in `edit` mode).",
        "Five intervene actions: approve / reject / edit / skip / abort.",
        "Phase 3.2 SmartPause confidence bar inline when the run was auto-paused.",
        "Intervene-mode legend (5-col grid) anchoring the page.",
      ]}
      endpoints={[
        "GET /runs (3s, filtered to paused)",
        "POST /runs/{id}/intervene",
      ]}
    />
  )
}
