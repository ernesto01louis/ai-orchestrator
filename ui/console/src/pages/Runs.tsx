import { Layers } from "lucide-react"
import { StubPage } from "./StubPage"

/**
 * Runs page — placeholder for PR 2.6 (a).
 *
 * The real list + detail pages land in PR 2.6 (b). Detail subscribes
 * to /ws filtered by run_id and exposes the Phase 1.5 manifest verify
 * action plus, when paused, the Phase 3.1 approve/edit/reject buttons.
 */
export function RunsPage() {
  return (
    <StubPage
      title="Runs"
      Icon={Layers}
      planned={[
        "List with phase / state filters + search by id, project, model.",
        "Detail with live log tail (/ws filtered by run_id), Phase 1.5 manifest verify.",
        "Phase 3.1 approve / edit / reject inline when the run is paused.",
        "Phases timeline panel + provenance KV (manifest sha, dsse, trace, dvc path).",
      ]}
      endpoints={[
        "GET /runs (3s)",
        "GET /runs/{id} (2.5s while open)",
        "GET /runs/{id}/verify",
        "POST /runs/{id}/resume",
        "POST /runs/{id}/intervene",
        "WS /ws (filter run_id)",
      ]}
    />
  )
}
