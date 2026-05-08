/**
 * Wire shapes for the orchestrator REST + WebSocket contract.
 *
 * Mirrors the FastAPI server's response bodies as of Phase 3 ship.
 * Only the fields the UI consumes are typed — extra keys pass through
 * via the `[k: string]: unknown` index signature where present.
 */

export type RunPhase =
  | "planner"
  | "post_planner"
  | "generator"
  | "judge"
  | "optimizer"
  | "complete"
  | "failed"
  | "pending"

export type HitlMode =
  | "full_auto"
  | "gate_only"
  | "checkpoint"
  | "step_by_step"
  | "co_pilot"
  | "smartpause"

export type RunPaused = "smartpause" | `hitl:${string}` | null

export interface Run {
  id: string
  project: string
  campaign_id?: string | null
  phase: RunPhase
  score: number | null
  model: string
  target: string
  started_at: string // ISO 8601
  paused: RunPaused
  hitl_mode: HitlMode
  /** Phase 3.2 SmartPause planner self-confidence in [0, 1]. */
  confidence?: number | null
}

export interface CampaignBudget {
  used: number
  total: number
  percentage: number
  state: "healthy" | "warn" | "err"
}

export type CampaignStatus = "running" | "complete" | "aborted" | "failed" | "pending"

export interface Campaign {
  id: string
  name: string
  hitl_mode: HitlMode
  status: CampaignStatus
  children: number
  completed: number
  failed: number
  started_at: string
  budget: CampaignBudget
  /** Param grid (raw shape varies per campaign). */
  grid?: Record<string, unknown>
}

export interface Health {
  orchestrator: "ok" | "degraded" | "down"
  ollama: "ok" | "degraded" | "down"
  hindsight: "ok" | "degraded" | "down"
  postgres?: "ok" | "degraded" | "down"
  redis?: "ok" | "degraded" | "down"
  tempo?: "ok" | "degraded" | "down"
  prometheus?: "ok" | "degraded" | "down"
  dvc?: "ok" | "degraded" | "down"
  uptime_s?: number
  version?: string
}

export interface Metrics {
  // Headline counters
  llm_calls_total: number
  llm_calls_rate_5m: number
  llm_tokens_in_total: number
  llm_tokens_out_total: number

  // Latency
  llm_p50_ms: number
  llm_p95_ms: number
  llm_p99_ms: number

  // Aggregates
  campaigns_active: number
  runs_active: number
  runs_paused: number

  // Budget (Phase 2.4)
  budget_total_usd: number
  budget_used_usd: number

  // Ollama
  ollama_queue_depth: number
  ollama_gpu_util: number
  ollama_vram_used_gb: number
  ollama_vram_total_gb: number

  // Sparkline windows. The server may return these inline or via a
  // separate /metrics_sparks endpoint; the UI is tolerant of either.
  sparks?: {
    llm_rate?: number[]
    p95?: number[]
    queue?: number[]
    gpu?: number[]
  }
}

// ─── /ws message frames ────────────────────────────────────────────────

export interface WsLogMessage {
  type: "log"
  run_id: string
  phase: RunPhase
  line: string
  ts?: string
  level?: "info" | "warn" | "err"
}

export interface WsStatusMessage {
  type: "status"
  run_id: string
  phase: RunPhase
  score?: number | null
  paused?: RunPaused
}

export interface WsPongMessage {
  type: "pong"
}

export type WsMessage = WsLogMessage | WsStatusMessage | WsPongMessage

// ─── Intervene route body ─────────────────────────────────────────────

export type InterveneAction = "approve" | "reject" | "edit" | "skip" | "abort"

export interface IntervenePayload {
  action: InterveneAction
  /** Required when action === "edit"; otherwise omitted. */
  payload?: string
}
