# ROADMAP.md — AI Orchestrator

> Complete task list. When you finish an item, mark `[x]` and note the
> commit. Re-read the "Do not build" list in [CLAUDE.md](CLAUDE.md)
> before adding anything new.

---

## Phase 0 — Refactor and safety net — **COMPLETE** (v0.1.0-phase0)

This phase reordered the original plan: secrets and tests came BEFORE
the module split, with each extraction gated by green tests + a live
service smoke. See `git log v0.1.0-phase0` for the 14 commits.

### 0.a-0.f Foundation
- [x] `git init`, `.gitignore`, scaffolding files (LICENSE, CONTRIBUTING.md,
      SECURITY.md, ARCHITECTURE.md, config.example.json, .env.example,
      requirements.txt). ui/.git archived to /root/ as recovery backup.
- [x] Gotify token rotated out of `config.json` into `.env` (gitignored,
      chmod 600). python-dotenv loaded at startup.
- [x] Initial commit — verified no secrets in history.
- [x] pytest scaffold + 25 characterization tests against the live
      orchestrator (26 with ws-broadcast inversion in 0.e).
- [x] Fixed `_ws_broadcast` cross-thread async bug
      (captured-loop + `run_coroutine_threadsafe` pattern).
- [x] Tightened SAFE_FILENAME regex (no `..`, no `/`).
- [x] Deleted 5 abandoned refactor stubs.
- [x] Reconciled hardcoded paths in dream.py and gates.py (now import
      from core.paths with hardcoded fallback).

### 0.g Module extraction (8 commits)
- [x] `core/` — paths, config, locks, runtime (RUN_STATUS, _ws_broadcast, log)
- [x] `notifications/send.py` — Gotify + ntfy + per-event helpers
- [x] `llm/` — ollama, repair, extract (URL routing moved here, breaking
      the bidirectional dep with core)
- [x] `execution/` + `references_pkg/` — SSH, sandbox, verify, deps,
      language, persistent deploy + PDF/file conversion
- [x] `tools/` — registry, dispatcher, gates integration
- [x] `memory_pkg/` — positive/negative/stats/embedding/layers/sessions/
      targets/hindsight/vault (named *_pkg to avoid clash with the
      /memory/ data dir). Vault↔memory coupling deferred to follow-up.
- [x] `orchestration/` — run_orchestration loop, agent functions,
      context builders, OrchestrateRequest, agent schemas
- [x] `api/routes.py` — all 82 routes on a single APIRouter,
      included via `app.include_router()` in app.py

Result: app.py 7,523 → 303 lines (-7,220, -96%).

### 0.h Lint, CI, docs, tag
- [x] Install ruff + mypy.
- [x] Fix all F821 undefined-name errors surfaced by ruff (real bugs in
      execution/tools/memory_pkg/orchestration/api/references_pkg from
      the surgical extraction).
- [x] `.github/workflows/ci.yml` — pytest + ruff + mypy on push/PR.
- [x] Populate ARCHITECTURE.md, RUNBOOK.md, CONTRIBUTING.md, SECURITY.md.
- [x] Update CLAUDE.md to reflect post-0.g layout.
- [x] Refresh requirements.txt.
- [x] Tag `v0.1.0-phase0`.

### Deferred to follow-up (not blocking Phase 1)
- [ ] Sub-split execution/, memory_pkg/, orchestration/, api/ into the
      file-per-area layout the original plan called for. Single-file
      packages work; this is purely cosmetic refactor with no behavior
      change. Do it when one of these files crosses a maintenance
      threshold or when a Phase 1 feature naturally lands in the right
      sub-file.
- [ ] Break vault→memory coupling (vault writers should accept
      pre-loaded data as parameters instead of calling load_*).
- [ ] Backup/restore: rsync from orchestrator LXC to TrueNAS, offsite copy,
      `RESTORE.md`, quarterly restore tests. (RUNBOOK.md has the manual
      procedure already.)
- [ ] Push the repo to GitHub, enable branch protection on main.

---

## Phase 1 — Research-platform capabilities (~8 weeks)

### 1.1 Campaign abstraction — DONE
- [x] Pydantic `Campaign` model in `core/campaign.py`
- [x] `campaigns/` directory with YAML templates (`campaigns/example.yaml`)
- [x] `POST /campaigns`, `GET /campaigns`, `GET /campaigns/{id}/tree`
- [x] `POST /campaigns/{id}/{pause,resume,abort}`
- [x] Campaign runner in `orchestration/campaign.py`
- [x] Vault writes campaign note + subnotes per run
- [x] Tests: lifecycle, pause/resume, abort, evidence emission (11 new tests, 37 total green)

### 1.2 Evidence bundle schema
- [ ] Pydantic `EvidenceBundle` in `core/evidence.py`
- [ ] Pluggable evidence calculators
- [ ] Bundle written to `campaigns/{id}/evidence.json`
- [ ] Signed manifest at `campaigns/{id}/manifest.json`
- [ ] Optional GPG signing

### 1.3 Prefect 3.x integration
- [ ] Install Prefect in a new LXC
- [ ] Wrap `run_orchestration` as a Prefect flow
- [ ] Subflows for generator parallelism, optimizer, troubleshooter
- [ ] Configure retries (3 attempts, exponential backoff)
- [ ] Use Prefect's resume-after-crash for control-flow state
- [ ] Document handoff in ARCHITECTURE.md

### 1.4 DVC on TrueNAS
- [ ] `pip install dvc[ssh]`
- [ ] `dvc remote add -d truenas ssh://truenas.local/mnt/tank/orchestrator-dvc`
- [ ] Track `references/`, campaign outputs, datasets
- [ ] Pre-commit hook: `dvc status` before commit

### 1.5 SHA256 artifact manifests
- [ ] Every run file gets SHA256 in `manifest.json`
- [ ] Campaign-level manifest (Merkle root)
- [ ] Verify on read; mark corrupted on mismatch
- [ ] CLI tool: `orchestrator verify-run <run_id>`

### 1.6 Python client library — PRIMARY CONSUMER CONTRACT
- [ ] Create separate repo `ai-orchestrator-client`
- [ ] Sync + async `OrchestratorClient`
- [ ] Methods: `run`, `start_campaign`, `wait_for_completion`
- [ ] `Campaign.iter_runs()` streaming
- [ ] Auth hooks (API tokens)
- [ ] Published to PyPI

### 1.7 MCP contract hardening
- [ ] Document all MCP tools at `/mcp`
- [ ] Version the MCP contract; bump major on breaking changes
- [ ] Add API-token auth
- [ ] Per-tool metadata
- [ ] Test: external MCP client can discover and call tools

### 1.8 Operational improvements
- [ ] Single-flight lock on `_refresh_url_cache`
- [ ] Audit subprocess calls for consistent timeout
- [ ] Pydantic validation of `config.json` at startup
- [ ] Log rotation in `LOG_DIR` (daily gzip, keep 90 days)
- [ ] `prometheus_client` basic metrics

---

## Phase 2 — Durability and observability (~8 weeks)

### 2.1 Postgres for durable state
- [ ] Postgres in its own LXC (independent backups)
- [ ] Schema: campaigns, runs, evidence_bundles, model_stats_daily
- [ ] Alembic for migrations
- [ ] Write-through: JSON file + Postgres row
- [ ] Reconcile on startup

### 2.2 Redis for ephemeral state
- [ ] Redis in a new LXC
- [ ] Move active `RUN_STATUS` to Redis
- [ ] Move `_ws_clients` coordination to Redis pubsub
- [ ] Move `_url_cache` and `_embed_cache` to Redis

### 2.3 OpenTelemetry
- [ ] `opentelemetry-instrumentation-fastapi`
- [ ] Self-host Tempo or Jaeger
- [ ] Grafana reads Tempo + Prometheus
- [ ] Wrap `log()`, `ssh_command`, LLM calls with spans
- [ ] Per-run trace view in Grafana

### 2.4 Budget tracking
- [ ] Extend Campaign with `budget_used`
- [ ] Token approximation per LLM call
- [ ] Cloud GPU hours × rate
- [ ] Thresholds at 50/80/100%
- [ ] Auto-pause on 100%

### 2.5 SkyPilot for cloud-burst GPU
- [ ] `pip install skypilot[runpod,vast]`
- [ ] YAML specs in `sky/`
- [ ] `POST /runs/{id}/burst`
- [ ] Cost tracking hooks into 2.4
- [ ] Auto `sky stop` + idle-timeout failsafe

### 2.6 New UI
- [ ] Framework decision (React/Vue/Svelte)
- [ ] Thin client, REST + WebSocket only
- [ ] Pages: Runs, Campaigns, Memory Search, Gates, Models, Targets, Vault, Config, Live Logs

---

## Phase 3 — Advanced (~6 weeks + ongoing)

### 3.1 HITL intervention modes
- [ ] `hitl_mode`: full_auto / gate_only / checkpoint / step_by_step / co_pilot
- [ ] ntfy action buttons for approve/reject

### 3.2 SmartPause
- [ ] Planners return `confidence: float`
- [ ] If confidence < 0.7 and mode != full_auto, auto-pause

### 3.3 NoteDiscovery-grounded planner
- [ ] Planner queries NoteDiscovery MCP before proposing a campaign
- [ ] Seeds parameterization from literature
- [ ] Evidence bundle cites papers used

### 3.4 Example consumer project
- [ ] `examples/example-consumer/` — trivial math optimization, no domain
- [ ] Reference implementation for `CONSUMERS.md`

### 3.5 (Removed) Multi-orchestrator federation
- Per VISION.md: "probably unnecessary." Dropped.

---

## Ongoing operational hygiene

- [ ] Monthly: backup restore test
- [ ] Monthly: review negative memory and Gates promotions
- [ ] Monthly: review model stats — retire losers, promote winners
- [ ] Quarterly: security audit of Gates blocklist
- [ ] Quarterly: dependency updates (Dependabot or Renovate)
- [ ] As needed: update CLAUDE.md and ARCHITECTURE.md when reality diverges

---

## Publication milestones

- [ ] **Month 3**: blog post on the 5-layer memory system
- [ ] **Month 5**: arXiv preprint on the orchestrator architecture
- [ ] **Month 8+**: papers with consumer projects as case studies

---

## Success criteria for "orchestrator is done enough to rely on"

- [x] Phase 0 complete, tagged v0.1.0-phase0
- [ ] Can start a campaign from YAML and get a signed evidence bundle
- [ ] Python client library on PyPI, used by ≥1 external project
- [ ] ≥80% test coverage, CI green for 30 consecutive days
- [ ] Backup restore tested successfully
- [ ] One external contributor has merged a clean PR
