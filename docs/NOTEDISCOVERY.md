# NoteDiscovery contract — Phase 3.3 (REST)

> Originally scoped against an MCP wrapper around NoteDiscovery; the
> live container at `192.168.2.203:8010` is the FastAPI app directly,
> with no MCP endpoint. Phase 3.3 calls the REST API instead. The
> orchestrator becomes a NoteDiscovery **HTTP client**, not an MCP
> client. Same goal (literature/notes-grounded planner), simpler
> plumbing.

## Live target

- **Host:** Docker LXC at `192.168.2.203:8010`
- **Service:** NoteDiscovery `0.19.1` (FastAPI app)
- **Health:** `GET /health` → `{status: "healthy", app: "NoteDiscovery", version: "..."}`
- **Auth:** declared in OpenAPI (`HTTPBearer` / `APIKeyHeader`) but
  not enforced as of `0.19.1`; the orchestrator sends an optional
  `X-API-Key` header from `.env` (`NOTEDISCOVERY_API_KEY`) for
  forward-compatibility.

## Endpoints we use

### `GET /api/search?q=<query>&limit=<n>&offset=<m>`

Search the operator's NoteDiscovery vault by content. Match
highlighting is HTML; the orchestrator strips it before persisting.

**Request:**
```
GET /api/search?q=learning+rate&limit=8
```

**Response:**
```json
{
  "results": [
    {
      "name": "2026-04-30_lr_sweep",
      "path": "research/2026-04-30_lr_sweep.md",
      "folder": "research",
      "matches": [
        {"line_number": 2, "context": "...optimal <mark>learning rate</mark> 1e-4..."}
      ]
    }
  ],
  "query": "learning rate",
  "pagination": {"limit": 8, "offset": 0, "total": 42, "has_more": true}
}
```

**Notes:**
- `results[].matches[].context` contains `<mark class="search-highlight">…</mark>` HTML around hits — caller strips.
- `total` may exceed `limit`; the planner only consumes the top `limit`.
- Returns 422 on malformed query; 200 with empty `results` on no match.

### `GET /api/notes/{path}` (optional, for fetching full content)

Used when the planner wants more than the snippet — e.g. to seed a
specific parameter from a cited number. Used sparingly; the snippets
are usually enough.

### `GET /health`

Used at orchestrator startup for the fail-tolerant healthcheck. Cheap
(< 50 ms). Failure logs a warning and continues — the planner falls
back to its existing memory stack.

## What the orchestrator persists

Per-campaign: `memory/<campaign_id>/planner_research.json` —

```json
{
  "campaign_id": "uuid",
  "query": "...prompt-derived query...",
  "host": "http://192.168.2.203:8010",
  "fetched_at": "2026-05-08T17:00:00Z",
  "result_count": 8,
  "results": [
    {"name": "...", "path": "...", "snippet": "..."}
  ],
  "param_seeds": {"learning_rate": [1e-4, 5e-5]},
  "error": null
}
```

Plus, the Phase 1.2 evidence bundle gains a `references: list[Reference]`
field where each `Reference` maps a NoteDiscovery `path` to the
campaign's RO-Crate via the standard `citation` schema property.

## Non-goals (explicit)

- We do **not** rewrite NoteDiscovery's auth or schema.
- We do **not** re-implement MCP transport. The original plan said
  "Phase 1.7 MCP contract makes this a tool call"; that's true for
  servers we host (orchestrator at `/mcp`), not for arbitrary
  third-party services without an MCP shim.
- We do **not** mirror NoteDiscovery's full vault. The orchestrator
  only stores the search results it consumed for a given campaign.

## When to revisit

- If NoteDiscovery `>= 0.20` ships an MCP endpoint, drop the REST
  client and replace with `mcp.client.streamable_http`. Same call
  signature in `core/note_discovery.py`; no orchestrator-level
  change needed.
- If we need offline / batch literature search, add a separate
  client (e.g. Semantic Scholar, OpenAlex). Don't mix into this
  module.
