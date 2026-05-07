# CLAUDE.md — AI Orchestrator

> Auto-loaded by Claude Code at session start. Describes what EXISTS,
> what's being built, and the conventions that keep the codebase coherent.
> Update this file whenever reality diverges from it — a stale CLAUDE.md
> misleads every session until it's fixed.

## What this project is — and isn't

**This is a GENERIC autonomous-orchestrator PLATFORM.** It routes LLMs,
maintains a 5-layer memory system, sandboxes code execution, and exposes
capabilities to external research projects via Python/REST/MCP contracts.

**It is NOT a hub for personal research projects.** Consumer projects
(aerodynamics optimization, RF DF ML, …) live in their own repos, import
the Python client library, and never have domain-specific code merged
into this orchestrator.

**Test for "does this change belong here?":**
- Would an unrelated research project (protein folding, algorithmic
  trading, music generation) also benefit? → yes: belongs here.
- Does it reference aerodynamics, CFD, RF, antennas, torchsig, or any
  specific domain? → no: belongs in a consumer project.

License: **Apache 2.0** (`LICENSE` file at repo root).

## Module layout (post Phase 0.g)

```
app.py                      ~300 lines  FastAPI wiring + lifespan + MCP mount + import wall
core/                                    Foundational primitives
  paths.py / config.py / locks.py / runtime.py
llm/                                     Ollama client, JSON repair, code extraction
  ollama.py / repair.py / extract.py
notifications/send.py                    Gotify (primary) + ntfy (fallback)
execution/__init__.py     ~1000 lines    SSH, sandbox, verify, language handlers, deps,
                                         persistent_deploy
references_pkg/__init__.py               PDF / file conversion + vision-model image desc.
                                         (named *_pkg to avoid clash with /references/ data dir)
tools/__init__.py                        Tool registry + dispatcher (gates-checked)
memory_pkg/__init__.py    ~1970 lines    All five memory layers (positive/negative recall,
                                         stats, identity/primer/goals, sessions, targets,
                                         Hindsight client, vault writer)
                                         (named *_pkg to avoid clash with /memory/ data dir)
orchestration/__init__.py ~1500 lines    run_orchestration loop, planner/judge/generator/
                                         optimizer/troubleshooter agent functions, context
                                         builders, OrchestrateRequest model, agent schemas.
                                         Agent fns + helpers wrapped as Prefect @task,
                                         run_orchestration is a Prefect @flow (Phase 1.3).
orchestration/campaign.py                run_campaign as Prefect @flow with subflow per combo.
prefect_io/                              Façade between FastAPI / orchestration and Prefect 3.x:
  __init__.py                            submit_orchestration / submit_campaign /
                                         pause_flow_run / resume_flow_run / cancel_flow_run /
                                         _healthcheck. Falls back to daemon-thread spawn when
                                         Prefect server is unreachable.
  state_hooks.py                         Flow + task state hooks → RUN_STATUS, CAMPAIGN_STATUS,
                                         and LLM_CALL_LOG (LlmCall capture for evidence bundle).
core/llm_call_log.py                     LlmCallLogger + LlmCallRecord — populated by the
                                         on_task_completion hook for tasks tagged "llm-call".
api/routes.py             ~2000 lines    All routes + WebSocket on a single APIRouter
                                         (campaign + evidence routes appended). /orchestrate
                                         and /campaigns POST go through prefect_io;
                                         pause/resume/abort routes call the matching Prefect
                                         REST API. /status/<run_id> exposes the real
                                         flow_run_id once captured by the on_running hook.
evidence/                                Phase 1.2 citation-grade evidence bundle:
  hookspecs.py / __init__.py             pluggy plugin host
  builtin/{stats,lineage,compute,        5 builtin calculators
    code_fingerprint,hardware}.py
  rocrate.py                             RO-Crate 1.2 / WRROC emitter (round-trips)
  signing.py                             DSSE envelope + Ed25519 (PyNaCl)
  checklists.py                          REFORMS + NeurIPS auto-fill, Model Cards,
                                         Datasheets-for-Datasets
  builder.py                             build_bundle(campaign_id) pipeline
  verify.py                              standalone verifier CLI
agents/                                  Per-role configs + agents/loader.py
dream.py, gates.py,                      Already extracted, kept at root for compat
mcp_server.py
campaign_templates/                      YAML campaign templates (git-tracked)
campaigns/                               Per-campaign evidence crates (DVC-tracked)
scripts/install_signing_key.sh           one-shot Ed25519 key setup
scripts/install_prefect.sh               LXC 201 bootstrap (Prefect server install).
scripts/install_prefect_worker.sh        orchestrator-side bootstrap (registers deployments).
prefect.yaml                             deployment manifest (orchestrate + campaign deployments).
manifest/__init__.py                     Per-run + campaign SHA256 manifests with Merkle root,
                                         verify-on-read helpers (Phase 1.5)
cli/main.py                              orchestrator CLI (argparse): verify-run / verify-campaign
                                         (Phase 1.5)
tests/                                   pytest scaffold (103 default + 3 prefect_real tests)
tests/integration/                       Real-server integration tests (gated by prefect_real marker).
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full data-flow diagram.

## What EXISTS

This list is detailed on purpose — if you're about to "add" one of these,
stop and read the existing implementation first.

**Memory (5 layers, all implemented):**
- L1 identity.md — persistent character/rules
- L2 primer.md — auto-rewritten after every run
- L3 live context — gathered at run start
- L4 Hindsight — full integration
- L5 Obsidian vault — per-run/project/model/target/error/daily notes,
  syncs to NoteDiscovery host and NAS mirror

Plus per-target identity, session log, goals.md with patching API,
positive memory with semantic similarity, negative memory with separate
index, model stats with per-language/per-role/per-project-type tracking.

**LLM layer:**
- Dual-server routing with TTL cache (`llm.ollama.resolve_chat_url` /
  `resolve_generate_url`)
- `/api/chat` with JSON schema enforcement
- `/api/generate` for free-form code
- Circuit breaker on judge (primary down → secondary fallback)
- JSON repair + safe parse
- Embeddings via `nomic-embed-text` with file-backed cache

**Execution:**
- SSH target abstraction with shlex quoting + StrictHostKeyChecking
- Language handlers for Python / Bash / JavaScript (with aliases)
- Per-language dependency detection (AST for Python, regex for Node)
- Verify local-first, SSH-fallback
- Sandbox + server-aware sandbox with port detection/reassignment
- System-level dep auto-install with sudo allowlist
- Persistent deploy to `~/ai-projects/{name}` with run.sh wrapper

**Agents (hot-reloadable config):**
- `agents/<role>/` per-role configs (planner, judge, generator,
  optimizer, troubleshooter, tool_dispatch)
- Each role: system_prompt.md, user_prompt.md, schema.json, optional
  language variants
- `agents/loader.py` — `load_agent`, `reload_all`, `list_roles`

**Tools:**
- Registry in `tool_registry.json` (CRUD via `/tools` endpoints)
- Dispatcher uses `agents/tool_dispatch` to decide what to run
- Argument sanitization with shlex.quote
- Destructive-command blocklist (`tools._TOOL_CMD_BLOCKLIST`)
- MCP server mounted at `/mcp` (Starlette sub-app)
- Gates integration (every tool call passes through `check_gate`)

**Gates (safety learning):** see [gates.py](gates.py).

**Dream (memory consolidation):** see [dream.py](dream.py).

**Reference documents (RAG):** PDF → markdown via pymupdf4llm with image
description from a vision model when available.

**Workflow engine (Phase 1.3 — DONE):**
- Prefect 3.x server on dedicated LXC 201 (`prefect-server`, LAN 192.168.2.182, Tailscale 100.76.57.6)
- `run_orchestration` and `run_campaign` are `@flow`-decorated; agent functions
  are `@task`-decorated with `retries=2`/`retries=0` per role
- Two execution modes: `in_process` (default, daemon-thread runs the flow) +
  `deployment` (Prefect worker pulls from `orchestrator-pool`)
- Server-down fallback: when Prefect API is unreachable, falls back to raw
  daemon-thread spawn (`.fn` invocation) — no functional regression
- State hooks drive `RUN_STATUS`/`CAMPAIGN_STATUS` updates AND populate
  `LLM_CALL_LOG` for evidence-bundle telemetry (closes Phase 1.2.x deferred item, Scope α)
- `prefect-integration` CI job spins up Prefect 3 in a background step and runs `pytest -m prefect_real`

**Operations:**
- `.env`-based secrets (loaded via python-dotenv at startup)
- WebSocket `/ws` with thread-safe broadcast (Phase 0.e fix)
- Run index persistence
- Pause/restart control endpoints
- Health endpoint
- File locking (`fcntl.flock`) on every JSON read/write

**API surface:** 82 routes, all on `api.routes.router`, included via
`app.include_router()`.

**Per-run + campaign integrity (Phase 1.5):**
- SHA256 manifest at `projects/<project>/runs/<run_id>/manifest.json` — automatic, end of run
- Merkle root at `campaigns/<campaign_id>/merkle.json` — automatic, end of campaign
- HTTP verify: `GET /runs/{run_id}/verify`, `GET /campaigns/{id}/verify-merkle`, `manifest_status` in `/status`
- CLI: `orchestrator verify-run <run_id>` / `verify-campaign <campaign_id>`
- Reuses `evidence.signing.sha256_file` (no duplication)

**MCP contract hardening (Phase 1.7):**
- Bearer-token auth via `core/auth.py` (RFC 6750). Set
  `ORCHESTRATOR_API_TOKEN` to enable; covers REST + `/mcp` + `/ws`.
- `mcp_server.MCP_CONTRACT_VERSION = "1.0.0"`. Bump MAJOR on breaking
  changes, MINOR on additions, PATCH on doc-only.
- `orchestrator://contract` resource: machine-readable enumeration of
  tools / resources / templates / prompts, with `ToolAnnotations` and
  per-tool `meta` (`category`, `requires_target`). Drift-free —
  introspects FastMCP at call time.
- Human docs: `docs/MCP_TOOLS.md`.

**Postgres durable store (Phase 2.1, opt-in):**
- Config: `postgres.enabled=false` default. Set `POSTGRES_DSN` in `.env`
  and `postgres.enabled=true` in `config.json` to activate.
- Engine: `core/db.py` (sync SQLAlchemy 2.0 + `psycopg[binary,pool]`
  3.x). `is_enabled()` is the gate every callsite checks.
- Schema: `alembic/versions/0001_initial_schema.py` — 5 tables
  (`campaigns`, `runs`, `llm_calls`, `evidence_bundles`,
  `model_stats_daily`). All PKs are TEXT to match UUID-shaped JSON IDs.
  `manifest_sha256/status` and `merkle_root/status` capture Phase 1.5
  attestation in the schema.
- Write-through: `core/db_writethrough.py` is the single chokepoint.
  JSON first, Postgres second; failure logs + Prom counter, never
  raises. `save_campaigns(data, changed_ids=...)` scopes the upsert
  to the campaigns the caller actually mutated.
- Reconcile: `core/db_reconcile.py:reconcile_all` sweeps
  `memory/run_index.json`, `memory/campaigns.json`,
  `campaigns/<id>/` (for evidence-bundle metadata via
  `manifest.json.dsse` sha256), and `memory/model_stats.json` (one
  `source='reconcile_seed'` row per model). Hooked into `app.py:_lifespan`
  via `await asyncio.to_thread`.
- Metrics: `orchestrator_postgres_writethrough_total{table,outcome}`,
  `orchestrator_postgres_reconcile_rows_total{table}`,
  `orchestrator_postgres_reconcile_duration_seconds` on `/metrics`.

**Redis ephemeral store (Phase 2.2, opt-in):**
- Config: `redis.enabled=false` default. Set `REDIS_URL` in `.env`
  and `redis.enabled=true` in `config.json` to activate.
- Client: `core/redis_client.py` (sync `redis-py` 7.x).
  `is_enabled()` gates every callsite. `decode_responses=True` so
  callers get `str`, not `bytes`. Socket timeouts from config so a
  wedged Redis can't stall handlers.
- RUN_STATUS mirror: `core/runtime._mirror_run_status_to_redis`
  is called from `_init_run_status` / `_update_run_status` after the
  in-process write. JSON-encoded HSET + EXPIRE in a pipeline. Failures
  log + Prom counter, never raise. The in-process `RUN_STATUS` dict
  remains the hot-path read source — Redis is the cross-process /
  survives-restart mirror.
- Hydrate-on-startup: `core/runtime.hydrate_run_status_from_redis()`
  hooks into `app.py:_lifespan` after Phase 2.1 reconcile. Runs in
  `asyncio.to_thread`. Zero impact when disabled.
- WS pub/sub: `core/runtime._WS_BROADCAST_CHANNEL = "ws_broadcast"`.
  Every `_ws_broadcast` publishes a `{origin, msg}` envelope; a
  daemon subscriber thread (`start_ws_broadcast_subscriber`) consumes
  via `pubsub.get_message(timeout=1.0)` poll loop and re-delivers to
  local `_ws_clients`. `_INSTANCE_ID` (per-process random hex, env
  override `ORCHESTRATOR_INSTANCE_ID`) filters self-publishes so
  single-process setups don't double-deliver.
- Caches: `core/redis_cache.py` exposes `url_cache_get_all/store`
  (Ollama model→server hash) and `embed_cache_get/set` (SHA256-keyed
  embedding strings). `llm/ollama._refresh_url_cache` checks Redis
  first; `memory_pkg.generate_embedding` tries Redis, falls back to
  the JSON cache, lazy-promotes JSON hits into Redis. Both fall
  through silently when Redis is disabled.
- Metrics: `orchestrator_redis_run_status_writes_total
  {operation,outcome}` on `/metrics` (operation in
  {init,update,hydrate}; outcome in {success,failure}).
- LXC bring-up: `scripts/install_redis.sh` (Debian 12 + Redis 7.0 +
  AOF + `requirepass`). RUNBOOK § "Redis ephemeral store".

**OpenTelemetry tracing (Phase 2.3, opt-in):**
- Config: `otel.enabled=false` default. Set `OTEL_ENDPOINT` in `.env`
  and `otel.enabled=true` in `config.json` to activate.
- SDK: `opentelemetry-api/sdk/proto/exporter-otlp-proto-grpc==1.41.1`
  + `opentelemetry-instrumentation-{fastapi,requests,asgi}==0.62b1`.
  All beta-channel pins matched; bump as a group.
- Init: `core/otel.py:init_tracing(app)` — idempotent.
  `TraceIdRatioBased` sampling, `BatchSpanProcessor`, `OTLPSpanExporter`
  (gRPC, insecure for LAN). Auto-instruments FastAPI + requests.
  Fail-tolerant: any init exception logs + returns False, never
  raises into `_lifespan`.
- Manual spans:
    * `llm/ollama.query_ollama` → `llm.generate` span
    * `llm/ollama.query_ollama_structured` → `llm.chat` span
      (both with attrs llm.{model,url,endpoint_kind,role,
      eval_count,response_chars,outcome} + orchestrator.run_id)
    * `execution.ssh_command` → `ssh.command` span (ssh.{target,
      host,username,command_preview,returncode,stdout_bytes,
      stderr_bytes,outcome,timeout_seconds})
    * `core/runtime.log()` — adds an `orchestrator.log` event to
      whatever span is currently active on the calling thread.
      Zero-cost when no span is active (OTel's INVALID_SPAN
      sentinel `add_event` is a no-op).
- LXC bring-up: `scripts/install_tempo.sh` (Debian 12 + Tempo
  2.6.x single-binary, OTLP/gRPC :4317, OTLP/HTTP :4318, query
  :3200, local-blocks 14d retention) +
  `scripts/install_grafana.sh` (Debian 12 + Grafana 12.4.3 from
  apt.grafana.com — pinned to dodge the 13.0.1 reset-admin-password
  regression — auto-provisioned datasources for Tempo +
  Prometheus, "AI Orchestrator — Per-run traces" dashboard).
  RUNBOOK §§ "OpenTelemetry tracing" + "Grafana dashboards".

**Budget tracking (Phase 2.4, opt-in):**
- Config: ``budget.enabled=false`` default. Set rates per model in
  ``budget.rates_per_million_tokens`` (USD per 1M prompt + completion
  tokens; ``default`` is the fallback). ``thresholds_pct`` list
  controls notification crossings (default ``[50, 80, 100]``);
  100 ALSO triggers auto-pause.
- Schema: alembic 0002_budget_tracking adds
  ``campaigns.{budget_total_usd, budget_used_usd, budget_state,
  budget_thresholds_emitted}`` + ``llm_calls.{prompt_tokens,
  cost_usd}``. CHECK constraint on ``budget_state ∈ {ok, warning,
  breach, paused}``.
- Calculator: ``core.budget.cost_usd_for(model, prompt, completion)``
  is a pure lookup × multiplication. ``evaluate_thresholds`` returns
  a ``BudgetEval`` describing next state, newly-crossed percentages,
  and ``should_pause``.
- Accrual: ``core.budget.accrue_to_campaign(run_id, cost)`` is called
  from ``prefect_io/state_hooks.on_task_completion`` AFTER the
  ``LlmCall`` row is mirrored. Linear-scans ``campaigns.json`` to
  find the owning campaign, increments ``budget_used_usd``, fires
  notifications + Prom counter for newly-crossed thresholds, and
  pauses the campaign on 100% breach (in-process flag + Prefect
  ``pause_flow_run``).
- Route: ``GET /campaigns/{id}/budget`` returns
  ``{campaign_id, enabled, budget_used_usd, budget_total_usd,
  percentage_used, budget_state, thresholds_emitted, thresholds_pct}``.
- Metrics: ``orchestrator_budget_threshold_total{threshold,state}``
  on ``/metrics``.

**SkyPilot cloud-burst (Phase 2.5, opt-in):**
- Config: ``sky.enabled=false`` default. Set ``RUNPOD_API_KEY`` /
  ``vastai`` creds and flip the flag.
- Wrapper: ``core/sky.py`` lazy-imports the SDK; ``is_enabled()`` is
  a 3-condition gate (config + SDK importable + ``yaml_dir`` exists).
  ``BurstRequest`` / ``BurstHandle`` dataclasses, ``launch_burst`` /
  ``stop_burst`` / ``status_burst`` / ``list_active_bursts`` /
  ``cost_report_for_cluster``. Per-burst USD ceiling
  (``sky.max_burst_cost_usd``) rejected at launch.
- YAML specs: ``sky/llm-burst.yaml`` (Ollama on GPU, long-running) +
  ``sky/torch-eval.yaml`` (PyTorch one-shot). Add new specs by
  basename and they're available to the burst route.
- Routes: ``POST /runs/{id}/burst`` launches, ``GET /runs/{id}/bursts``
  lists active, ``POST /runs/{id}/bursts/{cluster}/stop`` stops AND
  accrues actual cost to the campaign budget via Phase 2.4
  ``core.budget.accrue_to_campaign``.
- Idle-stop daemon: ``start_idle_stop_daemon`` (called from
  ``app.py:_lifespan``) polls every 60s. Stops clusters past
  ``sky.idle_timeout_minutes`` of no activity. ``timeout=0``
  disables.
- Three-tier cost discipline: per-burst ceiling at launch +
  per-campaign budget at accrual + idle-stop failsafe.

**Operational hardening (Phase 1.8):**
- Config validation: `core/config_schema.py` `OrchestratorSettings` —
  Pydantic v2 model loaded by `core/config.py` at import time. Bad
  config fails fast with `SystemExit("[core.config] Invalid config in
  <path>: …")` instead of `KeyError` deep in a request path.
- URL cache single-flight: `llm/ollama._refresh_url_cache` is wrapped
  in a `threading.Lock` with double-checked locking. N concurrent
  callers fan down to one `/api/tags` HTTP roundtrip per TTL window.
- Subprocess timeouts: all 14 production subprocess calls already
  passed `timeout=`. The audit fixed three places that propagated
  `TimeoutExpired` uncaught (`execution.verify_local`,
  `execution.deploy_file`, `tools.run_command` — last tightened from
  `except Exception` to `except (SubprocessError, OSError)`).
- Log rotation: `core/log_rotation.py` `rotate_logs()` (gzip > 1d,
  delete `.log.gz` > 90d). Daemon thread in `app.py` lifespan +
  `orchestrator rotate-logs` CLI subcommand with `--dry-run`.
- Prometheus metrics at `/metrics` (auth-bypassed): four instruments
  in `core/metrics.py` — Counter `orchestrator_runs_total{status}`,
  Histogram `orchestrator_agent_task_seconds{role,model}`, Counter
  `orchestrator_llm_calls_total{role,model,outcome}`, Gauge
  `orchestrator_active_runs`. **No `run_id` label** (cardinality
  discipline; Grafana correlates by run_id via logs/traces, not labels).

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the full phase-by-phase task list.

### Phase 0 — DONE (v0.1.0-phase0)

`app.py` shrunk from 7,523 → 303 lines. Module split per the table above.
Tests, secrets in `.env`, `_ws_broadcast` cross-thread fix, dead stub
removal, ruff/mypy/CI scaffold all landed. See `git log v0.1.0-phase0`.

### Phase 1 — research-platform capabilities

1.1 Campaign abstraction (generic, domain-agnostic) — DONE (v0.1.1-phase1.1)
1.2 Evidence bundle schema — DONE (v0.1.2-phase1.2): citation-grade
    RO-Crate 1.2 / WRROC bundles with in-toto + SLSA + DSSE attestation,
    REFORMS + NeurIPS checklists, Model Cards, Datasheets, pluggable
    calculators, Ed25519 signing
1.3 Prefect 3.x as workflow engine — DONE (v0.1.3-phase1.3, merged via
    PR #4 + #5). Prefect server on LXC 201 (`prefect-server`, LAN
    192.168.2.182, tailnet 100.76.57.6). `execution_mode` defaults to
    `in_process`; `deployment` mode requires starting the systemd worker.
    Scope β (citation-grade LlmCall fidelity) shipped on PR #5: every
    `LlmCall` carries real `call_id` (Prefect task_run.id), `agent_role`,
    `host:port`, `model_digest` (`/api/show`-cached sha256), `model_size_bytes`,
    `response_text`, and `started_at`.
1.4 DVC on TrueNAS — DONE (v0.1.4-phase1.4). DVC client + remote
    scaffolded on the orchestrator LXC; remote points at
    `ssh://dvc-orchestrator@192.168.2.222/mnt/f3/orchestrator-dvc` over
    a dedicated TrueNAS user. Helper at `scripts/dvc_track.sh`. See
    `RUNBOOK.md` "Data versioning with DVC" for full setup.
1.5 SHA256 artifact manifests — DONE (v0.1.5-phase1.5): per-run SHA256
    manifest.json + per-campaign Merkle merkle.json, lazy verify-on-read,
    `orchestrator` CLI, +46 unit/hook/route/cli tests (167 total).
1.6 Python client library — DONE in-tree (2026-05-07): separate repo at
    `/opt/ai-orchestrator-client/`, 22 commits on `main`, `0.1.0a0`
    version. Sync + async clients with full HTTP surface, `/ws` log
    streaming, `Campaign.iter_runs(client)` polling generator, typed
    Pydantic mirrors with OpenAPI drift check, BearerTokenAuth shell.
    133 tests (ruff + mypy --strict + pytest), GitHub Actions CI matrix,
    Trusted-Publishing release workflow. PyPI publish pending operator
    (register publisher + push GitHub remote + push `v0.1.0a0` tag — see
    `/opt/ai-orchestrator-client/RELEASING.md`).
1.7 MCP contract hardening — DONE (v0.1.7-phase1.7, shipped 2026-05-07):
    contract version `1.0.0` exposed at `orchestrator://contract`,
    `docs/MCP_TOOLS.md` documents all 9 tools / 10 resources / 1 resource template / 3 prompts,
    per-tool `ToolAnnotations` + freeform `meta` (`category`,
    `requires_target`), bearer-token auth via `core/auth.py` honoring the
    Phase 1.6 client SDK's `BearerTokenAuth` shape unchanged. External
    MCP smoke test in `tests/test_mcp_external_client.py`.
1.8 Op fixes — DONE (v0.1.8-phase1.8, 2026-05-07): config validation
    (Pydantic), URL cache single-flight (threading.Lock + double-check),
    subprocess `TimeoutExpired` handling at 3 callsites, log rotation
    (gzip > 1d / delete > 90d, daemon thread + CLI), Prometheus metrics
    at `/metrics` (auth-bypassed). 6 atomic commits, +43 tests
    (203 → 246), ruff + mypy --strict clean on all touched files.

### Phase 2 — durability + observability

2.1 Postgres for durable state — DONE (v0.2.1-phase2.1, 2026-05-07,
    PR #11 merge `c8375c1`). Postgres in its own LXC as a **secondary**
    durable store; JSON files under `memory/`/`runs/`/`campaigns/`
    remain canonical. 5 tables (`campaigns`, `runs`, `llm_calls`,
    `evidence_bundles`, `model_stats_daily`) under Alembic. Sync
    SQLAlchemy 2.0 + `psycopg[binary,pool]` 3.x. Dual-write chokepoint
    at `core/db_writethrough.py` — JSON-first, Postgres-second, log+swallow
    on failure (never raises out of Prefect `@task` bodies). Wired into
    `_persist_run_index`, `save_campaigns` (with `changed_ids` scoping),
    `state_hooks.on_task_completion`, `build_bundle`, and
    `update_model_stats`. Reconcile-on-startup at `core/db_reconcile.py`
    sweeps the 4 canonical JSON files plus `campaigns/<id>/` RO-Crate
    dirs. Three Prom instruments on `/metrics`:
    `orchestrator_postgres_writethrough_total{table,outcome}`,
    `orchestrator_postgres_reconcile_rows_total{table}`,
    `orchestrator_postgres_reconcile_duration_seconds`. Ships dormant —
    `postgres.enabled=false` default; operator action:
    `scripts/install_postgres.sh` → `alembic upgrade head` → flip flag.
2.2 Redis for ephemeral state — DONE (v0.2.2-phase2.2, 2026-05-07).
    LXC 203 `redis-server` (192.168.2.186, Debian 12 + redis-server
    7.0.x, AOF on, `requirepass` auth, `allkeys-lru` eviction). Five
    commits across `feat/phase2.2-redis`:
    (2.2.1a) dormant Redis client + config wiring;
    (2.2.1b) `scripts/install_redis.sh` + RUNBOOK section;
    (2.2.2) RUN_STATUS write-through mirror + `hydrate_run_status_from_redis`
    on startup;
    (2.2.3) WS broadcast pub/sub fan-out via `_WS_BROADCAST_CHANNEL`,
    instance-ID origin filter, poll-based subscriber loop;
    (2.2.4) `core/redis_cache` for `_url_cache` + `_embed_cache` with
    TTL.
    Each layer no-ops when `redis.enabled=false`; flip the flag once
    LXC 203 is reachable. One Prometheus counter
    (`orchestrator_redis_run_status_writes_total{operation,outcome}`).
    +57 net new tests (324 → 381 passing on the default suite, plus
    7 `redis_real` tests against the live LXC). Live since 2026-05-07.
2.3 OpenTelemetry — DONE (v0.2.3-phase2.3, 2026-05-07). LXC 204
    `tempo-server` (192.168.2.187, Tempo 2.6.1) + LXC 205
    `grafana-server` (192.168.2.188, Grafana 12.4.3 pinned). Five
    commits across `feat/phase2.3-otel`:
    (2.3.1) dormant OTel SDK + FastAPI/requests auto-instrumentation;
    (2.3.2) manual spans on log() / ssh_command / two query_ollama*
    entrypoints with domain attrs (orchestrator.run_id, llm.{model,
    role,outcome}, ssh.{target,host,returncode});
    (2.3.3) `scripts/install_tempo.sh` + LXC 204 + RUNBOOK +
    end-to-end verified (5 traces with rootServiceName=ai-orchestrator);
    (2.3.4) `scripts/install_grafana.sh` + LXC 205 + auto-provisioned
    Tempo + Prometheus datasources + "AI Orchestrator — Per-run
    traces" dashboard (UID orchestrator-per-run, TraceQL filter on
    orchestrator.run_id variable).
    Each layer no-ops when `otel.enabled=false`; flip the flag once
    Tempo is reachable. +23 net new tests on the default suite (381
    → 404). Live since 2026-05-07.
2.4 Budget tracking — DONE (v0.2.4-phase2.4, 2026-05-07). Five
    atomic commits across `feat/phase2.4-budget`:
    (2.4.1) ``BudgetConfig`` schema + ``core.budget`` cost calculator
    + alembic 0002 migration adding ``campaigns.budget_*`` and
    ``llm_calls.prompt_tokens`` / ``cost_usd``. Migration applied
    LIVE on LXC 202.
    (2.4.2 + 2.4.3) state hook computes per-LLM-call cost and accrues
    to the parent campaign via ``core.budget.accrue_to_campaign``;
    threshold transitions fire Gotify/ntfy notifications and
    auto-pause the campaign on 100% breach.
    (2.4.4) ``GET /campaigns/{id}/budget`` route + Prom counter
    ``orchestrator_budget_threshold_total{threshold,state}``.
    +33 net new tests (404 → 437). Live since 2026-05-07:
    ``budget.enabled=true`` in ``config.json``, default rates
    ``$0`` for local Ollama (electricity below the measurement
    threshold), thresholds ``[50, 80, 100]``. Operators raise rates
    per-model when their consumer projects route through paid
    providers.
2.5 SkyPilot for cloud-burst GPU — DONE (v0.2.5-phase2.5,
    2026-05-07, dormant ship). Four atomic commits across
    `feat/phase2.5-skypilot`:
    (2.5.1) ``BurstConfig`` schema + ``core.sky`` lazy-importing
    wrapper with ``BurstRequest`` / ``BurstHandle`` dataclasses,
    ``launch_burst`` / ``stop_burst`` / ``status_burst`` /
    ``list_active_bursts``, ``SkyDisabledError``. Per-burst USD ceiling
    enforced at launch. ``skypilot[runpod,vast]>=0.12,<0.13``
    pinned in requirements.
    (2.5.2) ``sky/llm-burst.yaml`` + ``sky/torch-eval.yaml`` starter
    specs + RUNBOOK section.
    (2.5.3) ``POST /runs/{id}/burst`` + companion list / stop routes.
    Stop accrues actual cost to the parent campaign via Phase 2.4
    ``core.budget.accrue_to_campaign``.
    (2.5.4) idle-stop daemon polls every minute and stops clusters
    past ``sky.idle_timeout_minutes``; ``timeout=0`` disables.
    Wired into ``app.py:_lifespan`` after the OTel init.
    +73 net new tests (404 → 479). Ships dormant
    (``sky.enabled=false``); operators configure provider creds
    (e.g. ``RUNPOD_API_KEY``), run ``sky check``, flip the flag.
2.6 New UI — pending.

### Phase 3 — advanced
HITL modes, SmartPause, NoteDiscovery-grounded planner, example consumer.

## Do NOT build

- MLflow / Aim / W&B — `model_stats` is better-suited.
- Hydra — already three config systems; don't add a fourth.
- Kubernetes — Proxmox LXC topology is sufficient forever.
- Custom workflow engine — use Prefect.
- Custom vector database — Hindsight + embedding cache cover this.
- Custom agent framework — the existing loop is mature.
- Multi-tenancy — solo project; not needed.
- **Domain-specific code** (aero, CFD, RF, antennas, specific hardware) —
  consumer projects only.

## Conventions

- Python 3.11+, type hints on new code, `mypy --strict` on the freshly
  extracted modules (`core/`, `llm/`, `notifications/`, `tools/`,
  `execution/`).
- FastAPI handlers are sync where they have to call into sync helpers
  (most do); async only when there's a real win.
- Tests next to or under `tests/`; `pytest -q` must stay green and < 1s.
- Conventional Commits (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`,
  `chore:`).
- Branch: `<type>/<short-slug>`.
- Never commit to `main`. PRs require green CI + one approval.
- File locking discipline: every JSON file at `memory/` uses
  `core.locks.locked_read_json` / `locked_write_json`. Never open
  these files without a lock.

## When in doubt

1. Domain flavor → push back, belongs in a consumer project.
2. An open-source tool exists → use it; don't rewrite.
3. Not in [ROADMAP.md](ROADMAP.md) → add it to the roadmap first.
4. Can't be tested → redesign until it can.
5. Re-read "What EXISTS" above before adding something that's already there.
6. Orchestrator vs. consumer: default to consumer.

---

*Last updated: 2026-05-07, Phase 2.5 (SkyPilot cloud-burst) shipped
on `feat/phase2.5-skypilot`, tag `v0.2.5-phase2.5`. Four atomic
commits (2.5.1 ``core.sky`` lazy-import wrapper + ``BurstConfig``
+ ``skypilot[runpod,vast]`` pin; 2.5.2 ``sky/llm-burst.yaml`` +
``sky/torch-eval.yaml`` + RUNBOOK; 2.5.3 ``POST /runs/{id}/burst``
+ companion list / stop routes with Phase 2.4 budget accrual; 2.5.4
idle-stop daemon polling every 60s) plus docs. +75 net new tests
(404 → 479). Ships **dormant** by operator's choice: `sky.enabled=false`
default, no live cloud account configured. Operator action when
ready: configure provider creds (e.g. `RUNPOD_API_KEY`), run
`sky check`, flip the flag, restart. Three-tier cost discipline
(per-burst ceiling + per-campaign budget + idle-stop failsafe).

Phase 2.4 (Budget tracking, prior release) shipped on
`feat/phase2.4-budget`, tag `v0.2.4-phase2.4`. Five atomic commits
(2.4.1 BudgetConfig + cost calculator + alembic 0002; 2.4.2-3
state-hook accrual + threshold transitions + auto-pause on 100%
breach; 2.4.4 ``GET /campaigns/{id}/budget`` route; docs).
+33 net new tests (404 → 437), end-to-end verified by flipping
``budget.enabled=true`` in live ``config.json`` and confirming the
route returns ``{enabled: true, budget_used_usd: 0.0, ...}`` for an
existing campaign. Default rates are $0 for local Ollama;
operators set per-model rates when consumer projects route through
paid providers. No new infrastructure — extends the existing
Postgres mirror + state-hook pipeline.

Phase 2.3 (OpenTelemetry tracing, prior release) shipped
on `feat/phase2.3-otel`, tag `v0.2.3-phase2.3`. Six atomic commits
(2.3.1 dormant SDK + auto-instrument; 2.3.2 manual spans on log /
ssh / LLM; 2.3.3 install_tempo.sh + LXC 204 LIVE; 2.3.4
install_grafana.sh + LXC 205 LIVE + datasources + per-run dashboard;
docs sweep). +23 net new tests (381 → 404), end-to-end verified by
generating /health traffic and watching traces flow into Tempo via
`/api/search?tags=service.name=ai-orchestrator`. Tempo 2.6.1 + Grafana
12.4.3 (pinned — 13.0.1 has reset-admin-password regression).
Live since 2026-05-07: LXC 204 (`tempo-server`, 192.168.2.187) +
LXC 205 (`grafana-server`, 192.168.2.188), `otel.enabled=true`,
`OTEL_ENDPOINT=192.168.2.187:4317` in .env.

Phase 2.2 (Redis ephemeral state, prior release) shipped
on `feat/phase2.2-redis`, tag `v0.2.2-phase2.2`. Five atomic commits
(2.2.1 dormant client + config; 2.2.1 install_redis.sh + RUNBOOK;
2.2.2 RUN_STATUS write-through mirror + hydrate-on-startup;
2.2.3 WS broadcast pub/sub fan-out; 2.2.4 url_cache/embed_cache via
core/redis_cache) plus a fix for the WS subscriber's idle socket
timeout. +57 net new tests (324 → 381), 7 redis_real tests against
live LXC 203. JSON / in-process state remains canonical; Redis is
the cross-process coordination layer that unblocks horizontal scale.
Live since 2026-05-07: LXC 203 (`redis-server`, 192.168.2.186) up,
`redis.enabled=true`, REDIS_URL in .env, ai-orchestrator.service
restarted cleanly with redis_ws_subscriber alive on idle pub/sub.

Phase 2.1 (Postgres durable state, prior release) shipped in PR #11
(merge `c8375c1`, tag `v0.2.1-phase2.1`): 13 atomic commits,
+77 tests (247 → 324), ruff + mypy --strict clean on every Phase 2.1
source module. JSON remains canonical; Postgres is the queryable
mirror that unblocks Phase 2.4 budget aggregates and Phase 2.6 UI. Ships
dormant; operator stands up the LXC and flips `postgres.enabled=true`.

Phase 1.8 (operational hardening, prior release) shipped:
config validation (Pydantic), URL cache single-flight, subprocess
`TimeoutExpired` handling, log rotation, Prometheus `/metrics`. 6
atomic commits on `feat/phase1.8-ops-hardening`, +43 tests (203 → 246),
ruff + mypy --strict clean on all touched files. Bearer-token auth
from Phase 1.7 still gates everything except `/health`, `/metrics`,
`/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc` (the
six-entry public-path bypass set in `core/auth.DEFAULT_PUBLIC_PATHS`).
When you complete a phase or
significantly change architecture, update this file before starting
the next work item.*
