/**
 * REST seam — every HTTP call against the FastAPI backend goes through
 * here. The Vite dev server proxies `/api/*` to the orchestrator (see
 * vite.config.ts), so callers don't need to know the upstream URL.
 *
 * Production builds set `VITE_ORCHESTRATOR_URL` at build time; if unset
 * the same-origin `/api/*` paths are used (the FastAPI app serves the
 * built UI under `/console` so same-origin works out of the box).
 *
 * Errors:
 *   - Non-2xx → throws `ApiError` with status + body.
 *   - Network failure → throws the underlying fetch error.
 * react-query handles retry / backoff at the call-site level.
 */

import type {
  Campaign,
  Health,
  IntervenePayload,
  Metrics,
  Run,
} from "./types"

const API_BASE = (import.meta.env.VITE_ORCHESTRATOR_URL ?? "/api").replace(/\/$/, "")

export class ApiError extends Error {
  status: number
  body: unknown
  constructor(status: number, message: string, body: unknown = null) {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.body = body
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`
  let resp: Response
  try {
    resp = await fetch(url, {
      headers: { Accept: "application/json", ...(init?.headers ?? {}) },
      ...init,
    })
  } catch (err) {
    throw err
  }
  if (!resp.ok) {
    let body: unknown = null
    try {
      body = await resp.json()
    } catch {
      try {
        body = await resp.text()
      } catch {
        /* ignore */
      }
    }
    throw new ApiError(resp.status, `HTTP ${resp.status} ${resp.statusText}`, body)
  }
  // 204 No Content
  if (resp.status === 204) return null as T
  return (await resp.json()) as T
}

// ─── Health + metrics ─────────────────────────────────────────────────

export const apiGetHealth = () => request<Health>("/health")
export const apiGetMetrics = () => request<Metrics>("/metrics_console")

// ─── Runs ─────────────────────────────────────────────────────────────

export const apiGetRuns = () => request<Run[]>("/runs")
export const apiGetRun = (id: string) => request<Run>(`/runs/${id}`)
export const apiResumeRun = (id: string) =>
  request<unknown>(`/runs/${id}/resume`, { method: "POST" })

export function apiIntervene(id: string, payload: IntervenePayload) {
  return request<unknown>(`/runs/${id}/intervene`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
}

// ─── Campaigns ────────────────────────────────────────────────────────

export const apiGetCampaigns = () => request<Campaign[]>("/campaigns")

export const apiPauseCampaign = (id: string) =>
  request<unknown>(`/campaigns/${id}/pause`, { method: "POST" })

export const apiResumeCampaign = (id: string) =>
  request<unknown>(`/campaigns/${id}/resume`, { method: "POST" })

export const apiAbortCampaign = (id: string) =>
  request<unknown>(`/campaigns/${id}/abort`, { method: "POST" })

export const apiGetCampaignBudget = (id: string) =>
  request<Campaign["budget"]>(`/campaigns/${id}/budget`)

export const apiGetCampaignEvidence = (id: string) =>
  request<Record<string, unknown>>(`/campaigns/${id}/evidence`)

// Streams the RO-Crate ZIP — caller turns the response into a Blob.
export async function apiDownloadEvidenceCrate(id: string): Promise<Blob> {
  const resp = await fetch(`${API_BASE}/campaigns/${id}/evidence.crate.zip`)
  if (!resp.ok) {
    throw new ApiError(resp.status, `HTTP ${resp.status} ${resp.statusText}`)
  }
  return resp.blob()
}
