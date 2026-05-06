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
campaigns/                               YAML templates + per-campaign evidence crates
scripts/install_signing_key.sh           one-shot Ed25519 key setup
scripts/install_prefect.sh               LXC 201 bootstrap (Prefect server install).
scripts/install_prefect_worker.sh        orchestrator-side bootstrap (registers deployments).
prefect.yaml                             deployment manifest (orchestrate + campaign deployments).
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
1.4 DVC on TrueNAS — IN PROGRESS (branch `feat/dvc-truenas`). DVC client
    + remote scaffolded on the orchestrator LXC; remote points at
    `ssh://dvc-orchestrator@192.168.2.222/mnt/f3/orchestrator-dvc` over
    a dedicated TrueNAS user. Helper at `scripts/dvc_track.sh`. End-to-end
    `dvc push` blocked on TrueNAS-side provisioning (SSH service + user +
    dataset). See `RUNBOOK.md` "Data versioning with DVC" for full setup.
1.5 SHA256 artifact manifests
1.6 **Python client library** (separate `ai-orchestrator-client` package)
1.7 MCP contract hardening (version, auth, documented tools)
1.8 Op fixes (URL cache single-flight, log rotation, config validation,
    Prometheus metrics)

### Phase 2 — durability + observability
Postgres + Redis + OTel/Tempo/Grafana + budget tracking + SkyPilot + new UI.

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

*Last updated: 2026-05-06, Phase 1.3 shipped (v0.1.3-phase1.3, PR #4 + #5
merged); Phase 1.4 (DVC on TrueNAS) in progress on branch `feat/dvc-truenas`.
When you complete a phase or significantly change architecture, update
this file before starting the next work item.*
