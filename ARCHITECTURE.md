# Architecture

> Snapshot at the close of Phase 0 (v0.1.0-phase0). When the layout
> changes, update this file before the next phase starts.

## Module layout

```
app.py                  (~300 lines)  FastAPI wiring + lifespan + MCP mount + import wall
core/                                   Foundational primitives (no orchestrator deps)
  paths.py                              Filesystem path constants; mkdir()s on import.
  config.py                             Loads config.json once; exposes derived constants.
  locks.py                              locked_read_json / locked_write_json (fcntl-based).
  runtime.py                            RUN_STATUS, _ws_broadcast, log, set_main_loop.
llm/                                    LLM clients + helpers
  ollama.py                             Ollama HTTP client + model-aware URL routing.
  repair.py                             repair_json + safe_parse_json.
  extract.py                            extract_code, extract_files, format_files_for_prompt.
notifications/                          Gotify / ntfy senders
  send.py                               send_notification + per-event helpers.
execution/__init__.py   (~1000 lines)  SSH primitives, sandbox runners, language handlers,
                                        verification, dependency detection, persistent deploy.
references_pkg/__init__.py              PDF/file conversion + vision-model image description.
                                        (Named *_pkg to avoid clash with /opt/.../references/ data dir.)
tools/__init__.py                       Tool registry + dispatcher (gates-checked).
memory_pkg/__init__.py  (~2000 lines)  Memory layers: positive/negative recall, embeddings,
                                        per-model stats, identity/primer/goals (L1/L2/Goal),
                                        sessions, per-target identity, Hindsight client (L4),
                                        vault writer (L5).
                                        (Named *_pkg to avoid clash with /opt/.../memory/ data dir.)
orchestration/__init__.py (~1500 lines) run_orchestration loop, planner/judge/generator/
                                        optimizer/troubleshooter agent functions, context
                                        builders, live-state queries, OrchestrateRequest model.
                                        Hub module — depends on every other package.
api/                                    HTTP + WebSocket routes
  routes.py             (~1900 lines)  82 routes on a single APIRouter, included via
                                        app.include_router() in app.py.
agents/                                 Per-role agent configs (already extracted)
  loader.py                             AgentConfig class, load_agent, reload_all.
  <role>/                               system_prompt.md, user_prompt.md, schema.json, etc.
dream.py, gates.py,                     Already extracted at repo root, kept there for
mcp_server.py                           historical compatibility (mcp_server is mounted as
                                        a sub-app on /mcp).
tests/                                  pytest scaffold
  conftest.py                           live + http fixtures.
  test_smoke_http.py                    24 HTTP characterization tests.
  test_inprocess.py                     2 in-process tests (repair_json + ws_broadcast).
```

Data files (gitignored, runtime state):

```
config.json                Local config (secrets in .env).
.env                       Secrets (chmod 600, gitignored).
memory/                    JSON state + L1/L2/Goal markdown.
vault/                     L5 Obsidian vault (writes only, never read by app).
references/                Uploaded reference docs (PDF + converted markdown).
projects/                  Per-project deploy artifacts.
logs/                      Per-run log files.
gates.json                 Gates state (ephemeral).
```

## Data flow per run

```
POST /orchestrate
  └─> api.routes.orchestrate
        └─> orchestration.run_orchestration  (in a daemon thread)
              ├─ memory_pkg.gather_live_context  (L3)
              ├─ memory_pkg.build_full_planner_context  (L1+L2+L3+L4)
              ├─ orchestration.planner_agent
              │    └─ llm.ollama.query_ollama_structured  (planner schema)
              ├─ tools.run_tools  (gates-checked)
              │    └─ execution.ssh_command
              ├─ ThreadPoolExecutor (parallel candidates):
              │    └─ orchestration.generate_candidate
              │         └─ llm.ollama.query_ollama  (free-form)
              ├─ orchestration.judge_score
              │    └─ llm.ollama.query_ollama_structured  (judge schema)
              ├─ orchestration.optimizer_agent  (if score < target)
              ├─ execution.verify_files / verify_code  (local then SSH)
              ├─ execution.sandbox_execute / sandbox_execute_server
              ├─ orchestration.troubleshoot  (if execution fails)
              ├─ execution.persistent_deploy  (on success)
              ├─ memory_pkg.update_memory  / update_negative_memory
              ├─ memory_pkg.update_model_stats
              ├─ memory_pkg.rewrite_primer  (L2 update)
              ├─ memory_pkg.hindsight_retain  (L4 update)
              ├─ memory_pkg.vault_after_run  (L5 write)
              ├─ notifications.notify_run_complete
              └─ core.runtime._update_run_status (completed=True)
                   └─ _persist_run_index  (durable snapshot)
```

WebSocket clients on `/ws` receive every `{"type": "log", ...}` and
`{"type": "status", ...}` event in real time, posted from the run thread
via `core.runtime._ws_broadcast`.

## Memory layers

| Layer | File | Owner | Update cadence |
|---|---|---|---|
| L1 identity | `memory/identity.md` | manual edit | rarely |
| L2 primer | `memory/primer.md` | `rewrite_primer()` | every run |
| L3 live context | gathered in-memory | `gather_live_context()` | per run |
| L4 Hindsight | external (`192.168.2.203:8888`) | `hindsight_retain()` | per run |
| L5 vault | `vault/**/*.md` | `vault_after_run()` | per run + sync |

Per-target identity (`memory/targets/<name>.md`) and goals
(`memory/goals.md`) are auxiliary to L1.

## External services

| Service | LXC IP | Purpose |
|---|---|---|
| Ollama main | 192.168.2.216:11434 | Generator + optimizer + troubleshooter (qwen2.5-coder:32b) |
| Ollama judge | 192.168.2.219:11434 | Judge + planner (qwen2.5:72b) |
| Hindsight | 192.168.2.203:8888 | L4 memory store |
| ntfy / Gotify | 192.168.2.203:8090 / :80 | Notifications |
| TrueNAS | 192.168.2.222 | Vault NAS mirror; future Postgres backups |

## Phase 1.3: Prefect 3.x integration

### Submission flow

```
POST /orchestrate
  → api.routes.orchestrate
  → prefect_io.submit_orchestration(req, run_id)
    in_process mode (default) → daemon thread runs run_orchestration (the @flow,
                                Prefect engine drives state hooks)
    deployment mode          → prefect.deployments.run_deployment enqueues;
                                the systemd prefect-worker.service executes
    server-down fallback     → daemon thread runs run_orchestration.fn (bypasses
                                Prefect entirely; inline _update_run_status() calls
                                keep the WebSocket UI alive)
  → run_orchestration(req, run_id)  # Prefect @flow
    on_running hook: capture flow_run.id into RUN_STATUS[run_id]["flow_run_id"]
    @task agent functions execute (planner, judge, generator.map(...), optimizer, troubleshoot)
    on_task_completion hook for tasks tagged "llm-call":
      append LlmCallRecord to LLM_CALL_LOG (run_id-keyed)
    on_completion / on_failure / on_cancellation hook:
      mirror state to RUN_STATUS / CAMPAIGN_STATUS for the WebSocket UI
```

### Execution modes (config.prefect.execution_mode)

| Mode | Behavior |
|---|---|
| `in_process` (default) | daemon thread spawns the @flow in-process; Prefect server tracks state via REST |
| `deployment` | `prefect.deployments.run_deployment` enqueues; the systemd `prefect-worker.service` on the orchestrator LXC executes |

### Server-down fallback

When `prefect_io._healthcheck()` returns False at app startup OR submission fails with a connect error, `prefect_io` falls back to a raw daemon-thread spawn that calls the `.fn` attribute of the @flow directly, bypassing Prefect entirely. Inline `_update_run_status(...)` calls retained inside `run_orchestration`/`run_campaign` keep the WebSocket UI alive on this path.

### Topology

| Service | LXC IP | Tailscale | Purpose |
|---|---|---|---|
| prefect-server | 192.168.2.182:4200 | 100.76.57.6:4200 | Prefect 3.6.29 server (SQLite-backed, systemd-managed) |
| orchestrator | 192.168.2.* | not yet installed | Submits via prefect_io.submit_*; `config.json` `prefect.api_url` points at LAN IP |

### Evidence bundle integration (Phase J α)

`evidence/builder.py:_build_run_records()` calls `LLM_CALL_LOG.drain(run.run_id)` per run and maps each `LlmCallRecord` to a `LlmCall` Pydantic model attached to `RunRecord.llm_calls`. Phase J Scope α uses placeholders for `call_id` (uuid4), `role` ("generator"), `target.host/digest/size`, `response_text`, and `started_at` (approximated from now()−duration_ms). Citation-grade fidelity is tracked in `docs/superpowers/followups/phase-j-beta-llm-call-fidelity.md`.
