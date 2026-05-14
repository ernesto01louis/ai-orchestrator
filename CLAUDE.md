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
examples/example-consumer/               Phase 3.4 reference consumer (math, no domain) —
                                         imports only ai-orchestrator-client SDK + PyYAML
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
- `POST /agents/reload` — REST endpoint that calls `reload_all()` at
  runtime. The "hot-reload" claim is honest: edit a prompt or schema
  on disk, POST to /agents/reload, the next agent call picks up the
  change. No restart required.

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

**NoteDiscovery-grounded planner (Phase 3.3):**
- ``core/note_discovery.py`` — REST client (not MCP, despite the
  original plan; the live container at ``192.168.2.203:8010``
  exposes a plain FastAPI app, no ``/mcp`` endpoint). Three public
  callables: ``is_enabled()`` (three-condition gate),
  ``healthcheck()`` (cheap GET /health probe), and
  ``search_notes(query, top_k)`` returning typed ``Note`` dataclasses
  with HTML-stripped snippets.
- ``orchestration._planner_research(prompt, run_id)`` queries
  NoteDiscovery at the start of every ``planner_agent`` call and
  prepends a "RELEVANT EXISTING NOTES" block to the planner's
  memory context. The full query→results trace is persisted to
  ``memory/<run_id>/planner_research.json``. Inert when
  ``note_discovery.enabled=false``; fail-tolerant on every error
  path (search failure / parse failure / persist failure).
- ``EvidenceBundle.references: list[Reference]`` — Phase 3.3
  extension to the Phase 1.2 schema. Populated by
  ``evidence.builder.build_bundle`` which deduplicates the
  ``planner_research.json`` traces across all runs in a campaign,
  preferring shorter (more summary-like) snippets. Round-trips
  losslessly via the existing ``ai_orchestrator:bundle`` carrier;
  RO-Crate emission additionally surfaces them as standard
  ``CreativeWork`` entities under the root Dataset's ``citation``
  array (RO-Crate 1.2 / WRROC).
- Startup healthcheck in ``app.py:_lifespan`` after the SkyPilot
  daemon. Logs ``note_discovery: reachable`` on success, a warning
  on failure; never fatal.
- Prom metrics: counter
  ``orchestrator_notediscovery_queries_total{outcome}`` (success /
  empty / failure / disabled) and histogram
  ``orchestrator_notediscovery_query_duration_seconds`` (9 buckets,
  50ms → 30s).
- Param seeding from literature was deferred — the operator's
  NoteDiscovery vault is personal notes, not numerical
  hyperparameters. The LLM planner consumes the snippets in its
  system prompt and proposes ``params`` with that context; the
  programmatic regex-extractor is left for a future iteration.
- Ships dormant by default (``note_discovery.enabled=false``);
  operator action: confirm GET /health, flip the flag, restart.

**HITL intervention modes (Phase 3.1):**
- ``CampaignTemplate.hitl_mode`` selects per-campaign interventionism;
  default ``"full_auto"`` keeps existing behaviour. SDK 0.1.0a1
  mirrors the field for typed campaign creation.
- Five modes — see ``core.hitl._mode_pauses_at_phase`` for the
  pause-or-skip truth table:
  | Mode          | Pauses at                                   |
  |---------------|---------------------------------------------|
  | full_auto     | (never)                                     |
  | gate_only     | gate_denied                                 |
  | checkpoint    | post_planner / post_generator / post_judge / post_optimizer / gate_denied |
  | step_by_step  | + post_llm                                  |
  | co_pilot      | + pre_llm  (operator can edit the prompt)   |
- ``core.hitl.hitl_checkpoint(run_id, phase)`` is the generic gate;
  flags ``RUN_STATUS[run_id]["paused"]="hitl:<phase>"``, fires
  ``notify_intervention`` (ntfy + Gotify with Approve / Reject HTTP
  action buttons), then blocks in ``wait_for_intervention``. Returns
  the operator's payload (``{action, prompt?}``) so co_pilot's LLM
  wrappers can thread ``prompt`` overrides into the next call.
- ``POST /runs/{id}/intervene`` — accepts
  ``{action: approve|reject|edit, prompt?: str}``. 400 / 404 / 409
  for malformed bodies / unknown run / queue full. Drains onto the
  per-run ``INTERVENTION_QUEUE`` AND clears the paused flag (so
  SmartPause-style pollers wake up too).
- ``POST /runs/{id}/resume`` — Phase 3.2 minimal unblock; still here
  as a fallback when ntfy action buttons aren't wired.
- ``gates.check_gate`` denials route through HITL when the run's
  ``hitl_mode != "full_auto"``. Operator ``approve`` overrides the
  gate this once; the rule itself isn't disabled.
- ``llm.ollama.query_ollama`` and ``query_ollama_structured`` wrap
  every LLM call with ``hitl_checkpoint(run_id, "pre_llm")`` (covers
  co_pilot) and ``hitl_checkpoint(run_id, "post_llm")`` (covers
  step_by_step). ``co_pilot`` ``action=edit`` payloads thread the
  override prompt into the request.
- Prom counter ``orchestrator_hitl_total{mode,phase,outcome}``
  (bounded ~210 combinations).
- Phase 3.2 SmartPause's ``_get_run_hitl_mode`` stub is now a thin
  wrapper around ``core.hitl.get_run_hitl_mode`` — SmartPause goes
  live the moment a campaign sets ``hitl_mode != "full_auto"``.

**SmartPause (Phase 3.2):**
- Planner schema gains an optional ``confidence: float`` in [0, 1].
  ``planner_agent`` clamps + defaults to 1.0 when missing
  (structured-success and unstructured-fallback paths) and to 0.0
  in the hardcoded "both attempts failed" path. Self-reported by
  the model; treat as a hint, not ground truth.
- ``orchestration._smartpause_check(run_id, plan)`` runs immediately
  after the planner returns. Inert when ``smartpause.enabled=false``,
  when ``hitl_mode == "full_auto"`` (today: every campaign — Phase
  3.1 ships per-campaign ``hitl_mode``), or when confidence ≥
  ``smartpause.confidence_threshold``. Otherwise: sets
  ``RUN_STATUS[run_id]["paused"]="smartpause"``, fires a notification
  with a Resume action button at ``/runs/{run_id}/resume``, and blocks
  in a polling loop until the flag clears or
  ``pause_timeout_seconds`` (1h default) elapses.
- ``_get_run_hitl_mode(run_id)`` is a stub that always returns
  ``"full_auto"`` until Phase 3.1 swaps it for a real campaigns.json
  lookup.
- ``POST /runs/{run_id}/resume`` is the unblock route: idempotent,
  404 on unknown run, sets ``RUN_STATUS[run_id]["paused"]=None``.
  Phase 3.1 will add the richer ``/runs/{id}/intervene`` (approve /
  reject / edit); ``/resume`` is the minimum needed for SmartPause
  to be useful in 3.2-only deployments.
- Prom counter ``orchestrator_smartpause_total{outcome}`` with
  outcomes in {paused, resumed, timed_out, skipped_full_auto,
  skipped_above, skipped_disabled}.

**Chunking primitive (repo-screening spike, 2026-05-11, DORMANT):**
- `core/chunking.py` exposes `chunk_text(text, *, site=...) -> list[str]`
  and `chunk_texts(...)` backed by `chonkie.RecursiveChunker`. The
  dormant path (`chunking.enabled=false`, default) returns `[text]` as
  a single-element list so callers can use `chunk_text(...)`
  unconditionally without branching on the gate.
- `ChunkingConfig` Pydantic model in `core/config_schema.py` exposes
  `enabled` / `chunker` / `chunk_size` / `chunk_overlap`. Default
  `chunk_size=1024` suits PDF-sized reference docs; vault-shaped
  corpora benefit from a smaller value (measurement showed `128` was
  the sweet spot for the 747-note vault).
- Prom counter `orchestrator_chunking_chunks_total{site,chunker}` —
  bumped from `chunk_text(...)` only when chunking is enabled and
  produces ≥1 chunk. Bounded cardinality (no `run_id` /
  `campaign_id`).
- Measurement harness at `scripts/measure_chunking_hit_rate.py`
  compares chonkie cache-key persistence vs naive whole-text hashing
  under a one-line edit on any corpus directory. Exit code 0 if
  mean persistence > 50%.
- **NOT wired into the live embedding pipeline.** Promoting chunking
  past "available primitive" changes cache-key semantics in
  `memory_pkg.generate_embedding` and `find_similar` (per-chunk
  matching vs per-document) — that's a separate phase.

**Web ingest primitive (repo-screening spike, 2026-05-12, DORMANT):**
- `references_pkg/web.py` exposes one public function — `ingest_url(url)
  -> IngestResult` — that POSTs to a self-hosted firecrawl `/v2/scrape`
  endpoint and persists the returned markdown under
  `references/web/<sha256>.md`. Files flow through the existing
  `load_reference_content` pipeline like a PDF.
- `WebIngestConfig` schema in `core/config_schema.py`: `enabled` /
  `base_url` / `timeout_seconds` / `skip_if_exists`. Default
  `base_url=http://192.168.2.189:3002` (LXC 206 `firecrawl-server`).
- Three-condition `is_enabled()` gate (config + base_url + requests).
  Any HTTP / timeout / malformed-response error is trapped and
  returned as an `IngestResult` with `status='http_error'` and
  `path=None`. The wrapper never raises.
- The orchestrator never crawls autonomously — `ingest_url` is
  operator/tool-initiated only (no scheduler hooks, no agent loop
  calls). `skip_if_exists` makes re-calls idempotent: same URL hashes
  to the same filename, won't overwrite a curated note unless flipped
  off explicitly.
- Saved markdown gets a small YAML frontmatter (`source_url`, `title`,
  `status_code`) so the file is self-describing.
- Startup healthcheck in `app.py:_lifespan` mirrors the Phase 3.3
  NoteDiscovery shape — single-line log on success, warning on
  unreachable, never fatal.
- Prom instruments in `core/metrics.py`:
    * Counter `orchestrator_web_ingest_total{outcome}` — outcome in
      {success, skipped_exists, http_error, empty, disabled,
      invalid_url}.
    * Histogram `orchestrator_web_ingest_duration_seconds` —
      0.1s-120s buckets. No URL or content-hash labels (Phase 1.8
      cardinality discipline).
- LXC 206 (firecrawl-server, 192.168.2.189, 4c/8GB/30GB) provisioned
  via `scripts/install_firecrawl.sh`. Docker + compose + the firecrawl
  self-host stack (api/playwright/redis/rabbitmq/nuq-postgres).
- **No callsite in the orchestrator wires `ingest_url` in yet.**
  Promoting it past "available primitive" (e.g. into a planner
  research step the way Phase 3.3 NoteDiscovery did) is a separate
  phase.

**Eval primitive (repo-screening spike, 2026-05-11, DORMANT):**
- `eval_pkg/scoring.py` exposes `score_response(input, actual_output,
  *, criteria, ...) -> EvalScore`. Backed by deepeval's G-Eval with
  an Ollama judge (`llama3:8b` on the Phase 1.3 judge node by default).
- Named `eval_pkg/` not `eval/` since `eval` shadows the Python
  builtin (and matches the existing `references_pkg` / `memory_pkg`
  collision-avoidance pattern).
- `EvalConfig` schema in `core/config_schema.py`: `enabled` /
  `judge_model` / `judge_base_url` / `threshold` /
  `case_timeout_seconds`.
- Three-condition `is_enabled()` gate (config + deepeval + ollama).
  Any deepeval / judge error is trapped and returned as a zero-score
  `EvalScore` with `error=True`; the wrapper never raises.
- **Opt-in install:** deepeval lives in `requirements-eval.txt` (not
  the base `requirements.txt`) for the same reason SkyPilot does:
  heavy dep tree (openai, posthog, pyfiglet, sentry-sdk, pytest-xdist,
  aiohttp, ...) for a primitive that ships dormant and is never
  invoked inside a run loop. Operators run
  `pip install -r requirements-eval.txt` to activate.
- Prom instruments in `core/metrics.py`:
    * Histogram `orchestrator_eval_score{metric,judge_model}` —
      observed only for real judge calls (error outcomes do NOT push
      0.0 in, so dashboard p50/p95 isn't skewed by failures).
    * Counter `orchestrator_eval_outcomes_total{metric,judge_model,
      outcome}` with outcome in {passed, failed, disabled,
      empty_input, error}.
- Measurement harness at `scripts/measure_eval_quality.py` runs G-Eval
  against a fixed 8-case canned suite (4 good + 4 bad across
  arithmetic, definition recall, instruction following, refusal) and
  prints discrimination accuracy. Exit code 0 if accuracy >= 0.8.
- **NOT wired into the live pipeline.** The orchestrator never auto-
  invokes `score_response()` inside a run loop — per-LlmCall eval
  doubles cost and most calls have no ground truth. Operators invoke
  the harness manually or build a downstream "eval campaign" concept
  that runs against a curated test suite.

**Consumer pattern (Phase 3.4):**
- `examples/example-consumer/` is the reference: a domain-neutral
  trivial-math optimization that imports only `ai-orchestrator-client`
  + PyYAML, posts via `OrchestratorClient.start_campaign`, streams
  via `Campaign.iter_runs(client)`, downloads the Phase 1.2 evidence
  bundle, and verifies the Phase 1.5 Merkle root.
- `CONSUMERS.md` at the repo root captures the full public surface
  + minimum-viable consumer snippet + the rules of the road
  ("never import orchestrator internals", "pin the SDK with a range",
  "hypothesis is required").
- `tests/examples/test_example_consumer_smoke.py` enforces the
  consumer-internal-import guard via source-text scan.

**Operator notes — caveats + policies:**

*HITL config precedence.* Three sources for `hitl_mode`, evaluated in
this order:

1. **`CampaignTemplate.hitl_mode`** — per-campaign, set at campaign
   creation. Highest precedence.
2. **`hitl.default_mode`** in `config.example.json` / live `config.json`
   — system-wide fallback for runs not bound to a campaign (one-shot
   `/orchestrate` requests). Default `"full_auto"`.
3. **`"full_auto"`** — hardcoded final fallback used when both above
   are absent or invalid (see `core.hitl.get_run_hitl_mode`).

`RUN_STATUS[run_id]["hitl_mode"]` is the *read-back* of the chosen
value for the UI / log — NOT a source-of-truth. Don't write back to
it expecting the orchestrator to re-route on the next checkpoint.

*Degraded evidence on Prefect server-down fallback.* When the Prefect
server at LXC 201 is unreachable, `prefect_io/__init__.py` falls back
to a daemon-thread spawn that calls `run_orchestration.fn(...)`
directly. This bypass keeps runs executing, but it **skips the
`state_hooks.on_task_completion` hook** that normally populates
`LLM_CALL_LOG` with citation-grade `LlmCall` records. Evidence
bundles emitted from fallback runs have empty `runs[].llm_calls`
arrays — they pass DSSE + Merkle verification but lose the
per-call telemetry. The fallback is for emergency continuity, not
publishable-grade output. (Phase 2 tech-debt PR adds a stub
`LlmCallRecord` synth on this path so bundles are non-empty; the
records are flagged `call_id="fallback-<uuid>"`, `model_digest="unavailable"`
to make the degradation visible to verifiers.)

*OTel beta-pin policy.* The OpenTelemetry SDK is pinned at
`0.62b1` / `1.41.1` — a coordinated **beta-channel** group across
four packages (`opentelemetry-api`, `opentelemetry-sdk`,
`opentelemetry-proto`, `opentelemetry-exporter-otlp-proto-grpc`)
plus the `opentelemetry-instrumentation-{fastapi,requests,asgi}`
group at the matching beta. When bumping:

1. **Bump all four core packages together**, plus the three
   instrumentation packages. Mixing minors silently breaks the
   trace-context propagation contract.
2. **Verify against a known-good Tempo container** (we pin Tempo
   2.6.1 for the same reason — `13.x` had the reset-admin-password
   regression). Spin up `grafana/tempo:2.6.1` locally, point the
   orchestrator at it, generate a trace with `curl /health` + a real
   `/orchestrate` run, confirm both arrive in Tempo with
   `service.name=ai-orchestrator`.
3. **Watch the FastAPI auto-instrumentation specifically** — it has
   the highest churn rate of the four. A bump that breaks it shows
   up as missing HTTP-route spans (LLM spans + manual spans keep
   working, so silence in `/api/search?tags=service.name=...` is
   the failure signature).

Dependabot config (`.github/dependabot.yml`) groups OTel updates so
patch + minor land together; major bumps remain manual.

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
- **Domain-flavored capabilities baked into the base install**
  (image gen, 3D rendering, hardware simulators). These go in
  [docs/PLUGINS.md](docs/PLUGINS.md) behind ``enabled=false`` flags
  + their own ``requirements-*.txt`` extras. Pattern: blender-mcp,
  cloud_image_gen, deepeval G-Eval, SkyPilot cloud-burst.

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

*Last updated: 2026-05-08, Phase 3.3 (NoteDiscovery-grounded planner)
shipped on `feat/phase3.3-notediscovery`, tag `v0.3.3-phase3.3`.
Original plan called for an MCP wrapper around NoteDiscovery; the
live container at 192.168.2.203:8010 turned out to expose a plain
REST API (NoteDiscovery 0.19.1, no /mcp endpoint), so 3.3 pivoted
to a thin requests-based REST client — same goal, simpler plumbing.
Six atomic commits (3.3.1+3.3.2 ``docs/NOTEDISCOVERY.md`` contract +
``core/note_discovery.py`` with is_enabled / healthcheck /
search_notes; 3.3.3 ``_planner_research`` step in planner_agent +
``memory/<run_id>/planner_research.json`` trace; 3.3.4
``EvidenceBundle.references`` field + RO-Crate ``citation``
emission via builder de-dup pass; 3.3.5 SKIPPED — operator vault is
personal notes, programmatic param-seeding would be noise; 3.3.6
startup healthcheck + Prom counter/histogram + RUNBOOK; 3.3.7 17
regression tests + CLAUDE.md + ROADMAP). +17 net new tests
(530 → 547). Ships dormant; operator flips
``note_discovery.enabled=true`` after confirming GET /health on the
container. Phase 3 advanced is now complete (3.4 / 3.2 / 3.1 / 3.3
all shipped); 3.5 federation dropped per VISION.md. Phase 2.6
(New UI) is next, with substantially more behavior to surface than
at the original deferral point.

Phase 3.1 (HITL intervention modes, prior release) shipped
on `feat/phase3.1-hitl-modes`, tag `v0.3.1-phase3.1`. Seven atomic
commits (3.1.1 ``hitl_mode`` field on ``CampaignTemplate`` +
``HITLConfig`` + ``core/hitl.py`` lookup/queue + replaces the SmartPause
stub with the real lookup; 3.1.2 ``POST /runs/{id}/intervene`` route +
``notify_intervention`` ntfy actions; 3.1.3 ``hitl_checkpoint`` at the
four phase boundaries — checkpoint mode goes live; 3.1.4 ``pre_llm`` +
``post_llm`` wraps in ``llm/ollama.py`` — step_by_step + co_pilot go
live; 3.1.5 ``gates.check_gate`` denials route through HITL —
gate_only goes live; 3.1.6 35 regression tests covering the
mode-pauses-at-phase truth table, the campaigns.json lookup, the
gate, and the route; 3.1.7 docs + SDK companion 0.1.0a1). +35 net
new tests (495 → 530). The companion ``ai-orchestrator-client``
0.1.0a1 (separate repo) mirrors ``CampaignTemplate.hitl_mode`` for
typed campaign creation. Phase 2.6 (New UI) remains deferred until
after 3.3.

Phase 3.2 (SmartPause, prior release) shipped on
`feat/phase3.2-smartpause`, tag `v0.3.2-phase3.2`. Four atomic commits
(3.2.1 ``SmartPauseConfig`` + ``agents/planner/schema.json`` confidence
field + ``planner_agent`` clamp normalisation; 3.2.2
``_smartpause_check`` helper with notify + polling-loop block, plus
``POST /runs/{id}/resume`` unblock route; 3.2.3 12 regression tests;
3.2.4 ``orchestrator_smartpause_total{outcome}`` Prom counter + docs).
+12 net new tests (483 → 495). Inert in current behaviour because
``_get_run_hitl_mode`` is a stub returning ``"full_auto"`` for every
run; Phase 3.1 swaps the stub for a real campaigns.json lookup, at
which point SmartPause becomes active without any callsite changes.
Second sub-phase of Phase 3; 3.1 HITL modes is next, then 3.3
NoteDiscovery-grounded planner.

Phase 3.4 (Example consumer project, prior release) shipped
on `feat/phase3.4-example-consumer`, tag `v0.3.4-phase3.4`. Three atomic
commits (3.4.1 `examples/example-consumer/` scaffold — README +
`template.yaml` + `run.py`; 3.4.2 smoke tests via `inprocess_client`
including a source-text guard against orchestrator-internal imports;
3.4.3 `CONSUMERS.md` + ROADMAP/CLAUDE updates). +4 net new tests
(479 → 483). The example imports only `ai-orchestrator-client` + PyYAML;
copy the directory and replace the prompt + sweep parameter for any
domain. Phase 2.6 (New UI) remains deferred until after Phase 3.

Phase 2.5 (SkyPilot cloud-burst, prior release) shipped
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
the next work item.

2026-05-11 — Repo-screening pass added four roadmap entries (no code):
Phase 2.5.1 (vLLM serving inside SkyPilot bursts, dormant until 2.5
activates), Phase 4.x (prollytree-backed memory branching for HITL
co_pilot — backlog only), and two 999.x backlog entries (`skill_dispatch`
agent role from deer-flow, HITL partial-state preservation from
agentscope). See `/root/.claude/plans/ill-paste-some-github-spicy-fern.md`
for the full screening rationale.

2026-05-11 — Chonkie chunking spike landed as an available primitive.
``core/chunking.py`` wraps ``chonkie.RecursiveChunker`` behind a
``chunking.enabled=false`` config gate. NOT wired into the live
embedding pipeline — promoting chunking past "available primitive"
means changing cache-key semantics across ``memory_pkg``, which is
a separate phase. Measurement on the 747-note vault corpus at
chunk_size=128 → mean 80.6% cache-key persistence under a one-line
edit, vs 0% for the naive whole-text baseline (win rate 100%). On
larger reference docs (CLAUDE/ROADMAP/RUNBOOK/ARCHITECTURE) at
chunk_size=1024 default → mean 80.0% persistence, win rate 100%. Right
chunk_size is corpus-shape dependent; the schema default (1024) suits
PDF-sized reference docs, not vault notes. See
``scripts/measure_chunking_hit_rate.py``.

2026-05-11 — Repo-screening deepeval spike landed as an available
primitive. `eval_pkg/scoring.py` wraps deepeval G-Eval with an Ollama
judge behind a `eval.enabled=false` config gate. NOT wired into the
live pipeline. Live measurement on a domain-neutral 8-case suite
(arithmetic, definition recall, instruction following, refusal) with
the llama3:8b judge: **87.5% discrimination accuracy** (7/8 correct,
~20s per case wall-clock). All 4 bad outputs correctly fail; one
overly-terse good answer scored 0.0 on first run (run-to-run variance
inherent to small judges on short outputs). Promoting eval into a
"Phase 4.x eval campaign" — a first-class campaign type with curated
prompts + expected outputs, scoreboard alongside model_stats.json,
optional evidence-bundle calculator — is a separate phase.

2026-05-12 — Repo-screening firecrawl spike landed as an available
primitive + live LXC. `references_pkg/web.py` exposes
`ingest_url(url) -> IngestResult` backed by a self-hosted firecrawl
`/v2/scrape` endpoint on LXC 206 (`firecrawl-server`, 192.168.2.189,
4 cores / 8 GB RAM / 30 GB disk, Docker + compose with
api/playwright/redis/rabbitmq/nuq-postgres). Dormant by default
(`web_ingest.enabled=false`). Live end-to-end measurement: 3/3
successful ingests (example.com 0.5s/180B, PEP 8 1.6s/57KB, Python
json docs 0.9s/42KB), markdown round-trips cleanly with YAML
frontmatter (source_url/title/status_code) into
`references/web/<sha256>.md`. NOT wired into any callsite — promoting
to a planner research step (the way Phase 3.3 NoteDiscovery did) is
a separate phase. LXC 206 provisioning is one-shot via
`scripts/install_firecrawl.sh`.*
