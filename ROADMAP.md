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
- [x] Backup/restore documentation: `RESTORE.md` is the authoritative
      procedure (Scenarios A + B + quarterly drill); `scripts/backup.sh`
      is the rsync-with-link-dest snapshot runner. Open sub-items inside
      RESTORE.md: activate the nightly cron, set up an offsite copy,
      run the first quarterly drill. (RUNBOOK.md still has the day-to-day
      ops; RESTORE.md is the disaster-recovery doc.)
- [ ] Push the repo to GitHub, enable branch protection on main.

---

## Phase 1 — Research-platform capabilities (~8 weeks)

### 1.1 Campaign abstraction — DONE
- [x] Pydantic `Campaign` model in `core/campaign.py`
- [x] `campaign_templates/` directory with YAML templates (`campaign_templates/example.yaml`)
- [x] `POST /campaigns`, `GET /campaigns`, `GET /campaigns/{id}/tree`
- [x] `POST /campaigns/{id}/{pause,resume,abort}`
- [x] Campaign runner in `orchestration/campaign.py`
- [x] Vault writes campaign note + subnotes per run
- [x] Tests: lifecycle, pause/resume, abort, evidence emission (11 new tests, 37 total green)

### 1.2 Evidence bundle schema — DONE (citation-grade)
- [x] Pydantic `EvidenceBundle` in `core/evidence.py` — wire-compatible
      with **RO-Crate 1.2** + **WRROC** profile, **in-toto Statement v1**,
      **SLSA Provenance v1.0**, **DSSE** envelope
- [x] Pluggable evidence calculators (pluggy + entry points; 5 builtins:
      stats, lineage, compute, code_fingerprint, hardware_fingerprint)
- [x] Bundle written to `campaigns/{id}/evidence.json` + RO-Crate at
      `campaigns/{id}/ro-crate-metadata.json`
- [x] Signed manifest at `campaigns/{id}/manifest.json` plus DSSE-wrapped
      `manifest.json.dsse` (Ed25519 via PyNaCl, single host-wide key)
- [x] **REFORMS** (Kapoor et al., *Sci Adv* 2024) + **NeurIPS Q4-Q8**
      checklists — half auto-filled, half user-fillable Markdown stubs
- [x] **Model Cards** (Mitchell et al. 2019) per LLM target;
      **Datasheets for Datasets** (Gebru et al. 2018) per data input
- [x] `hypothesis` field is now REQUIRED at campaign creation
      (REFORMS §1 pre-registration)
- [x] 4 REST routes: `GET .../evidence`, `.crate.zip`, `.../verify`,
      `POST .../refresh`
- [x] Standalone Python verifier (`python -m evidence.verify
      --crate-dir`) — pure stdlib + PyNaCl, no orchestrator runtime needed
- [x] 32 new tests (schema, rocrate, signing, calculators, e2e); 71/71 green
- [x] *Phase 1.2.1 shipped 2026-05-06*: self-contained HTML viewer at
      `evidence/html_viewer.py` — `build_html(bundle)` returns a single page
      with bundle JSON embedded as `<script type="application/json">`,
      vanilla JS rendering, all CSS inline. Builder emits `evidence.html`
      alongside `evidence.json` so it's covered by the signed manifest.
      Renders header/hypothesis, fingerprints, LLM targets, runs (with
      drill-down LLM calls and code executions), calculators, REFORMS +
      NeurIPS responses, model cards, datasheets, artifacts. No CDN
      dependency — works under `file://`.
- [x] *Closed in 1.3 (Scope α)*: per-LLM-call telemetry capture — `LLM_CALL_LOG`
      buffer populated by Prefect `on_task_completion` state hook for tasks
      tagged `"llm-call"`; drained per run in `evidence/builder.py` into
      `RunRecord.llm_calls`. Scope β (citation-grade fidelity for `call_id`,
      `role`, `target.host`, `response_text`, `started_at`) tracked at
      `docs/superpowers/followups/phase-j-beta-llm-call-fidelity.md`.
- [ ] *Deferred to Phase 2*: self-hosted Sigstore (Fulcio + Rekor) —
      DSSE envelope abstracts trust root, older bundles stay verifiable

### 1.3 Prefect 3.x integration — DONE (v0.1.3-phase1.3, PR #4 + #5)
- [x] Prefect 3.6.29 server on dedicated LXC 201 (`prefect-server`,
      LAN 192.168.2.182, Tailscale 100.76.57.6)
- [x] `run_orchestration` and `run_campaign` wrapped as `@flow`; agent
      functions (`planner_agent`, `judge_score`, `generate_candidate`,
      `optimizer_agent`, `troubleshoot`) wrapped as `@task`
- [x] Generator parallelism via `generate_candidate.map(...)` (replaces the
      prior `ThreadPoolExecutor` block); `unmapped()` for dict args because
      Prefect 3.x iterates dicts by default
- [x] Retries — flow-level `run_orchestration(retries=1, retry_delay_seconds=60)`,
      memory/evidence helper tasks `retries=2`, agent tasks `retries=0`
      (the troubleshoot loop already retries from the orchestration side)
- [x] Two execution modes via `config.json` `prefect.execution_mode`:
      `in_process` (default, daemon-thread runs the @flow) and `deployment`
      (worker pulls from `orchestrator-pool` via systemd `prefect-worker.service`)
- [x] Server-down fallback to daemon-thread spawn (`.fn` invocation) when
      Prefect API unreachable; inline `_update_run_status(...)` calls preserved
      so the WebSocket UI stays alive on this path
- [x] Real `flow_run_id` captured via `on_running` state hook into
      `RUN_STATUS`/`CAMPAIGN_STATUS` (replaces the earlier fake-UUID issue
      that made pause/resume/cancel silently no-op)
- [x] State hooks drive `RUN_STATUS`/`CAMPAIGN_STATUS` updates AND populate
      `LLM_CALL_LOG` for evidence-bundle telemetry (closes deferred 1.2.x item)
- [x] 11 new tests (95 → 103) plus 3 real-server tests gated by `prefect_real`
      pytest marker; CI `prefect-integration` job spins up a Prefect server
      in a background step and runs `pytest -m prefect_real`
- [x] Docs: ARCHITECTURE.md (submission flow + execution modes + topology),
      RUNBOOK.md (Prefect ops procedures), CLAUDE.md (module layout + EXISTS)
- [x] *Scope β shipped 2026-05-06 in PR #5*: citation-grade `LlmCall` fidelity —
      `call_id` (Prefect `task_run.id`), `agent_role` (threaded through 12 call
      sites), `target.host` (parsed from `task_run.parameters['url']`),
      `model_digest` + `model_size_bytes` (cached `/api/show`), `response_text`,
      `started_at` (`task_run.start_time`) all captured by the state hook;
      `evidence/builder.py:_record_to_llm_call` placeholders dropped.
      Followup doc at `docs/superpowers/followups/phase-j-beta-llm-call-fidelity.md`
      flipped to "DONE".
- [ ] *Deferred (operational)*: install Tailscale on orchestrator LXC 200
      so `prefect.api_url` can move from LAN IP to tailnet — script staged at
      `root@192.168.2.13:/root/install_ts_lxc200.sh`

### 1.4 DVC on TrueNAS — IN PROGRESS (branch `feat/dvc-truenas`)
- [x] `pip install dvc[ssh]` — pinned `dvc[ssh]>=3.67,<4` in `requirements.txt`
- [x] `dvc init` + `.dvcignore` (excludes venv/, caches, vault/, secrets)
- [x] Default remote: `ssh://dvc-orchestrator@192.168.2.222/mnt/f3/orchestrator-dvc`
      (using LAN IP because `truenas.local` doesn't resolve from LXC 200;
      dedicated user instead of root for least-privilege access)
- [x] `scripts/dvc_track.sh` — idempotent one-shot to `dvc add references/ campaigns/`
      and `dvc push`; honours `PATHS=` env override
- [x] Opt-in pre-commit hook at `scripts/git-hooks-available/pre-commit-dvc-status`
      with activation instructions in `RUNBOOK.md`. NOT auto-installed —
      pre-commit hooks must stay opt-in for this repo.
- [x] TrueNAS-side provisioning (Louis): SSH service enabled,
      `dvc-orchestrator` user with `Disable Password` + ed25519 pubkey from
      `/root/.ssh/id_ed25519_dvc.pub`, dataset `f3/orchestrator-dvc` owned
      by `dvc-orchestrator:dvc-orchestrator`. Pool name is `f3` (not `tank`)
      so remote URL = `ssh://dvc-orchestrator@192.168.2.222/mnt/f3/orchestrator-dvc`.
- [x] End-to-end smoke 2026-05-06: `dvc push` of a 39-byte test file landed at
      `/mnt/f3/orchestrator-dvc/files/md5/<2-char-prefix>/<rest>`. Cache
      layout matches DVC's content-addressed store; SSH path + key auth
      verified. Smoke artifact left on remote intentionally as proof of life.
- [ ] Bulk DVC snapshot of references/ + campaigns/ (operational; see RUNBOOK § "Phase 1.5 first-time DVC snapshot")

### 1.5 SHA256 artifact manifests — DONE (v0.1.5-phase1.5, shipped 2026-05-06)
- [x] Every run file gets SHA256 in `manifest.json`
- [x] Campaign-level manifest (Merkle root)
- [x] Verify on read; mark corrupted on mismatch
- [x] CLI tool: `orchestrator verify-run <run_id>`

### 1.6 Python client library — DONE (in-tree at `/opt/ai-orchestrator-client/`, 2026-05-07; PyPI publish pending operator action)
- [x] Create separate repo `ai-orchestrator-client` (22 commits on `main`)
- [x] Sync + async `OrchestratorClient` / `AsyncOrchestratorClient` with method parity
- [x] Methods: `run`, `start_campaign`, `wait_for_completion`, plus full
      campaign/evidence/verify surface and idempotent control
- [x] `Campaign.iter_runs(client)` streaming (sync + async dispatch on type)
- [x] Async `iter_logs(run_id)` via `/ws` (Phase F)
- [x] Auth hooks: `AuthProvider` Protocol + `BearerTokenAuth` shell
      (no-op vs Phase 1.6 server, honored by Phase 1.7 unchanged)
- [x] 133 tests (ruff + mypy --strict + pytest), CI matrix py3.11+3.12,
      Trusted-Publishing release workflow ready
- [ ] Published to PyPI — gated on operator: register Trusted Publisher
      on PyPI, push GitHub remote, push `v0.1.0a0` tag (see
      `/opt/ai-orchestrator-client/RELEASING.md`)

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
