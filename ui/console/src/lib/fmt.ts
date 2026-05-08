/**
 * Display formatters used throughout the console.
 *
 * All "ago"-style helpers return "—" on null/empty input so callers
 * don't have to guard each call site.
 */

export function fmtAgo(iso: string | null | undefined): string {
  if (!iso) return "—"
  const ms = Date.now() - new Date(iso).getTime()
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s ago`
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}h ${m}m ago`
}

export function fmtDuration(iso: string | null | undefined): string {
  if (!iso) return "—"
  const ms = Date.now() - new Date(iso).getTime()
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 60) return `${s}s`
  if (s < 3600) {
    return `${Math.floor(s / 60)}m${(s % 60).toString().padStart(2, "0")}s`
  }
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}h${m.toString().padStart(2, "0")}m`
}

export function fmtNum(n: number | null | undefined): string {
  if (n == null) return "—"
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B"
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M"
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k"
  return n.toString()
}

/**
 * `run_01HZK6QF8E2XAH9R4M` → `run_…AH9R4M`
 *
 * Used in tables and small badges where the full ULID is too wide.
 * The full id is always available on click / in tooltips.
 */
export function shortId(id: string | null | undefined): string {
  if (!id) return ""
  if (id.length <= 14) return id
  return id.slice(0, 4) + "…" + id.slice(-6)
}
