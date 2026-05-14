// Backend contract types. Mirror the FastAPI response shapes.

export type Phase =
  | "planner"
  | "post_planner"
  | "generator"
  | "judge"
  | "optimizer"
  | "complete"
  | "failed";

export type HitlMode = "full_auto" | "checkpoint" | "smartpause";

/** `null` = running normally; "smartpause" or `hitl:<mode>` when paused. */
export type PausedState = null | "smartpause" | `hitl:${HitlMode}`;

export interface Run {
  id: string;
  project: string;
  campaign_id: string;
  phase: Phase;
  score: number | null;
  model: string;
  target: string;
  started_at: string;
  paused: PausedState;
  hitl_mode: HitlMode;
  /** planner confidence, 0..1 — present when SmartPause may engage */
  confidence?: number;
}

export interface Budget {
  used: number;
  total: number;
  percentage: number;
  state: "healthy" | "warn" | "err";
}

export interface Campaign {
  id: string;
  name: string;
  hitl_mode: HitlMode;
  status: "running" | "complete" | "aborted" | "paused";
  children: number;
  completed: number;
  failed: number;
  started_at: string;
  budget: Budget;
  grid: Record<string, unknown[]>;
}

export interface Health {
  orchestrator: "ok" | "degraded" | "down";
  ollama: "ok" | "degraded" | "down";
  hindsight: "ok" | "degraded" | "down";
  postgres: "ok" | "degraded" | "down";
  redis: "ok" | "degraded" | "down";
  tempo: "ok" | "degraded" | "down";
  prometheus: "ok" | "degraded" | "down";
  dvc: "ok" | "degraded" | "down";
  uptime_s: number;
  version: string;
}

export interface Metrics {
  llm_calls_total: number;
  llm_calls_rate_5m: number;
  llm_tokens_in_total: number;
  llm_tokens_out_total: number;
  llm_p50_ms: number;
  llm_p95_ms: number;
  llm_p99_ms: number;
  campaigns_active: number;
  runs_active: number;
  runs_paused: number;
  budget_total_usd: number;
  budget_used_usd: number;
  ollama_queue_depth: number;
  ollama_gpu_util: number;
  ollama_vram_used_gb: number;
  ollama_vram_total_gb: number;
  /** sparkline series for the dashboard cards */
  sparks?: { llm_rate: number[]; p95: number[]; queue: number[]; gpu: number[] };
}

export type InterveneAction = "approve" | "reject" | "edit" | "skip" | "abort";

export interface IntervenePayload {
  action: InterveneAction;
  /** Required when action === "edit". Free-form JSON string. */
  payload?: string;
}

// ── WebSocket message shapes ────────────────────────────────────────
export interface WsLog {
  type: "log";
  run_id: string;
  line: string;
  phase: Phase;
  ts: string;
}

export interface WsStatus {
  type: "status";
  run_id: string;
  phase: Phase;
  score: number | null;
  paused: PausedState;
}

export type WsMessage = WsLog | WsStatus;
