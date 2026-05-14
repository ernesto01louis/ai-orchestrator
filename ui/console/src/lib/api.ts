// REST client. When VITE_USE_MOCKS=1 the fetchers resolve against the
// fixtures in mocks.ts instead of hitting the FastAPI backend, so the
// UI runs standalone for design review.
//
// In dev, /api/* is proxied to VITE_API_BASE (see vite.config.ts).

import type {
  Campaign,
  Health,
  IntervenePayload,
  Metrics,
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

// ── Endpoints ────────────────────────────────────────────────────────

export async function getHealth(): Promise<Health> {
  if (USE_MOCKS) { await sleep(80); return structuredClone(MOCK_HEALTH); }
  return get<Health>("/health");
}

export async function getMetrics(): Promise<Metrics> {
  if (USE_MOCKS) { await sleep(80); return structuredClone(MOCK_METRICS); }
  // /metrics is Prometheus-format; assume FastAPI exposes a JSON-shaped
  // sibling for the dashboard, e.g. /metrics.json.
  return get<Metrics>("/metrics.json");
}

export async function getRuns(): Promise<Run[]> {
  if (USE_MOCKS) { await sleep(120); return structuredClone(MOCK_RUNS); }
  return get<Run[]>("/runs");
}

export async function getRun(id: string): Promise<Run | undefined> {
  if (USE_MOCKS) { await sleep(80); return structuredClone(MOCK_RUNS.find((r) => r.id === id)); }
  return get<Run>(`/status/${id}`);
}

export async function getCampaigns(): Promise<Campaign[]> {
  if (USE_MOCKS) { await sleep(120); return structuredClone(MOCK_CAMPAIGNS); }
  return get<Campaign[]>("/campaigns");
}

export async function intervene(
  runId: string,
  body: IntervenePayload,
): Promise<{ ok: true; run_id: string; action: string; applied_at: string }> {
  if (USE_MOCKS) {
    await sleep(220);
    return { ok: true, run_id: runId, action: body.action, applied_at: new Date().toISOString() };
  }
  return post(`/runs/${runId}/intervene`, body);
}

export async function verifyManifest(_runId: string): Promise<{ verified: boolean; sha256: string }> {
  if (USE_MOCKS) { await sleep(1100); return { verified: true, sha256: "8f2c…b3a1" }; }
  return get(`/runs/${_runId}/manifest/verify`);
}
