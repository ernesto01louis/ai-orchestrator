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
                                         builders, OrchestrateRequest model, agent schemas
api/routes.py             ~1900 lines    All 82 routes + WebSocket on a single APIRouter
agents/                                  Per-role configs + agents/loader.py
dream.py, gates.py,                      Already extracted, kept at root for compat
mcp_server.py
tests/                                   pytest scaffold (26 tests)
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

### Phase 1 — research-platform capabilities (next)

1.1 Campaign abstraction (generic, domain-agnostic)
1.2 Evidence bundle schema
1.3 Prefect 3.x as workflow engine
1.4 DVC on TrueNAS for data versioning
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

*Last updated: end of Phase 0.g (v0.1.0-phase0). When you complete a
phase or significantly change architecture, update this file before
starting the next work item.*
