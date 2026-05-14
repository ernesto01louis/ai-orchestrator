// REST client. When VITE_USE_MOCKS=1 the fetchers resolve against the
// fixtures in mocks.ts instead of hitting the FastAPI backend, so the
// UI runs standalone for design review.
//
// In dev, /api/* is proxied to VITE_API_BASE (see vite.config.ts).
//
// Phase 2.6 bridging: the FastAPI backend serves runs / campaigns as
// `{runs: [...]}` and `{campaigns: [...]}` envelopes, and /health uses
// a nested shape. The small transform layer below unwraps + remaps so
// callers consume the frontend Run / Campaign / Health types directly.

import type {
  Campaign,
  Health,
  HitlMode,
  IntervenePayload,
  Metrics,
  Phase,
  PausedState,
  Run,
} from "./types";
import {
  MOCK_CAMPAIGNS,
  MOCK_HEALTH,
  MOCK_METRICS,
  MOCK_RUNS,
} from "./mocks";

const USE_MOCKS = import.meta.env.VITE_USE_MOCKS === "1";
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`GET ${path} → ${res.status}`);
  return (await res.json()) as T;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`POST ${path} → ${res.status}`);
  return (await res.json()) as T;
}

// ── Backend response shapes (subset of fields we actually consume) ───
interface BackendRun {
  run_id: string;
  id?: string;
  phase: string;
  score: number | null;
  project?: string;
  target?: string;
  model?: string;
  campaign_id?: string;
  started_at?: string | null;
  timestamp?: string | null;
  paused?: PausedState | null;
  hitl_mode?: string;
  confidence?: number | null;
  completed?: boolean;
}

interface BackendCampaign {
  id: string;
  name: string | null;
  status: string | null;
  hitl_mode?: string;
  children?: number;
  completed?: number;
  failed?: number;
  started_at?: string | null;
  created_at?: string | null;
  budget?: { used: number; total: number; percentage: number; state: string };
  grid?: Record<string, unknown[]>;
}

interface BackendHealth {
  services?: Record<string, "ok" | "degraded" | "down">;
  uptime_s?: number;
  version?: string;
  // legacy nested shape (still returned for Python SDK compat)
  orchestrator?: unknown;
  hindsight?: { status?: string };
}

// ── Transforms ───────────────────────────────────────────────────────
function toRun(b: BackendRun): Run {
  const hitlMode = (b.hitl_mode as HitlMode) || "full_auto";
  return {
    id: b.id ?? b.run_id,
    project: b.project ?? "",
    campaign_id: b.campaign_id ?? "",
    phase: b.phase as Phase,
    score: b.score ?? null,
    model: b.model ?? "",
    target: b.target ?? "",
    started_at: b.started_at ?? b.timestamp ?? new Date(0).toISOString(),
    paused: b.paused ?? null,
    hitl_mode: hitlMode,
    confidence: b.confidence ?? undefined,
  };
}

function toCampaign(b: BackendCampaign): Campaign {
  const budget = b.budget ?? { used: 0, total: 0, percentage: 0, state: "healthy" };
  const stateMap: Record<string, "healthy" | "warn" | "err"> = {
    healthy: "healthy", ok: "healthy",
    warn: "warn", warning: "warn",
    err: "err", breach: "err", paused: "err",
  };
  return {
    id: b.id,
    name: b.name ?? b.id,
    hitl_mode: (b.hitl_mode as HitlMode) ?? "full_auto",
    status: (b.status as Campaign["status"]) ?? "running",
    children: b.children ?? 0,
    completed: b.completed ?? 0,
    failed: b.failed ?? 0,
    started_at: b.started_at ?? b.created_at ?? new Date(0).toISOString(),
    budget: {
      used: budget.used,
      total: budget.total,
      percentage: budget.percentage,
      state: stateMap[budget.state] ?? "healthy",
    },
    grid: b.grid ?? {},
  };
}

function toHealth(b: BackendHealth): Health {
  const s = b.services ?? {};
  const safe = (k: string): Health["orchestrator"] =>
    (s[k] as Health["orchestrator"]) ?? "down";
  return {
    orchestrator: safe("orchestrator"),
    ollama: safe("ollama"),
    hindsight: safe("hindsight"),
    postgres: safe("postgres"),
    redis: safe("redis"),
    tempo: safe("tempo"),
    prometheus: safe("prometheus"),
    dvc: safe("dvc"),
    uptime_s: b.uptime_s ?? 0,
    version: b.version ?? "0.0.0",
  };
}

// ── Endpoints ────────────────────────────────────────────────────────

export async function getHealth(): Promise<Health> {
  if (USE_MOCKS) { await sleep(80); return structuredClone(MOCK_HEALTH); }
  const raw = await get<BackendHealth>("/health");
  return toHealth(raw);
}

export async function getMetrics(): Promise<Metrics> {
  if (USE_MOCKS) { await sleep(80); return structuredClone(MOCK_METRICS); }
  return get<Metrics>("/metrics.json");
}

export async function getRuns(): Promise<Run[]> {
  if (USE_MOCKS) { await sleep(120); return structuredClone(MOCK_RUNS); }
  const data = await get<{ runs: BackendRun[] }>("/runs");
  return (data.runs ?? []).map(toRun);
}

export async function getRun(id: string): Promise<Run | undefined> {
  if (USE_MOCKS) { await sleep(80); return structuredClone(MOCK_RUNS.find((r) => r.id === id)); }
  const raw = await get<BackendRun>(`/status/${id}`);
  return toRun(raw);
}

export async function getCampaigns(): Promise<Campaign[]> {
  if (USE_MOCKS) { await sleep(120); return structuredClone(MOCK_CAMPAIGNS); }
  const data = await get<{ campaigns: BackendCampaign[] }>("/campaigns");
  return (data.campaigns ?? []).map(toCampaign);
}

export async function intervene(
  runId: string,
  body: IntervenePayload,
): Promise<{ ok: true; run_id: string; action: string; applied_at: string }> {
  if (USE_MOCKS) {
    await sleep(220);
    return { ok: true, run_id: runId, action: body.action, applied_at: new Date().toISOString() };
  }
  // Backend response: { run_id, action, queued }. Normalize to the
  // {ok, run_id, action, applied_at} shape the UI's intervene flow expects.
  const raw = await post<{ run_id: string; action: string; queued: boolean }>(
    `/runs/${runId}/intervene`,
    body,
  );
  return {
    ok: true,
    run_id: raw.run_id,
    action: raw.action,
    applied_at: new Date().toISOString(),
  };
}

export async function verifyManifest(runId: string): Promise<{ verified: boolean; sha256: string }> {
  if (USE_MOCKS) { await sleep(1100); return { verified: true, sha256: "8f2c…b3a1" }; }
  // Backend response: { run_id, valid, status, mismatches }.
  const raw = await get<{
    run_id: string;
    valid: boolean;
    status: string;
    mismatches: string[];
  }>(`/runs/${runId}/manifest/verify`);
  return { verified: raw.valid, sha256: raw.status };
}
