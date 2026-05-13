# AI Orchestrator

> Autonomous research orchestrator that lets a solo engineer run AI-driven
> scientific campaigns with memory, evidence, and reproducibility — so
> "my AI proved X works better than Y" becomes a rigorous, citable claim
> instead of a demo.

A FastAPI platform that routes LLMs, maintains a 5-layer memory system,
sandboxes code execution on remote SSH targets, and exposes its
capabilities to external research projects via Python / REST / MCP
contracts. Self-hosted on Proxmox LXCs; designed to survive periods of
neglect.

**Status:** Phases 0 through 3 shipped (17 tagged releases,
`v0.1.0-phase0` → `v0.3.4-phase3.4`). Phase 2.6 (operator console)
is in flight on `feat/phase2.6-new-ui-foundation`. See
[ROADMAP.md](ROADMAP.md) for the per-phase task list and
[VISION.md](VISION.md) for the platform-not-hub principle.

---

## Features that exist today

**Research-platform capabilities (Phase 1):**

- **Campaigns** — parameterized multi-run experiments. `POST /campaigns`
  with a Pydantic template + param grid; per-campaign pause / resume /
  abort. `hypothesis` is REQUIRED (REFORMS §1 pre-registration).
- **Citation-grade evidence bundles** — RO-Crate 1.2 / WRROC, signed
  DSSE envelopes (Ed25519 via PyNaCl), REFORMS + NeurIPS auto-fill,
  Model Cards (Mitchell 2019), Datasheets for Datasets (Gebru 2018),
  plus a standalone Python verifier (`python -m evidence.verify`).
- **Prefect 3.x** as the workflow engine — `run_orchestration` and
  `run_campaign` are flows; agent functions are tasks. Server-down
  fallback to in-process execution.
- **DVC on TrueNAS** for data versioning of `references/` + `campaigns/`.
- **Per-run + per-campaign SHA256 manifests** with a Merkle root,
  verify-on-read routes, and a `orchestrator` CLI.
- **Published Python SDK** —
  [`ai-orchestrator-client`](https://pypi.org/project/ai-orchestrator-client/)
  is the consumer contract. See [CONSUMERS.md](CONSUMERS.md).
- **MCP contract `1.0.0`** with bearer-token auth at `/mcp` + REST + `/ws`.

**Durability + observability (Phase 2):**

- **Postgres** durable mirror (LXC 202; JSON-first dual-write).
- **Redis** ephemeral state (LXC 203; RUN_STATUS mirror + WS pub/sub).
- **OpenTelemetry → Tempo + Grafana** (LXC 204 + LXC 205; per-run
  trace dashboard).
- **Budget tracking** — per-LLM-call cost accrual, threshold
  notifications, auto-pause on 100% breach.
- **SkyPilot cloud-burst** (dormant — operator activates by dropping
  RunPod / Vast.ai creds and flipping `sky.enabled=true`).

**Advanced features (Phase 3):**

- **HITL intervention modes** — five modes per campaign
  (`full_auto` / `gate_only` / `checkpoint` / `step_by_step` /
  `co_pilot`); `POST /runs/{id}/intervene` with ntfy + Gotify
  action-button notifications.
- **SmartPause** — planner self-reports a `confidence: float`;
  auto-pause + notify when confidence drops below the threshold and
  `hitl_mode != "full_auto"`.
- **NoteDiscovery-grounded planner** — the planner queries the
  operator's NoteDiscovery vault before proposing a campaign;
  relevant note snippets feed the planner's system prompt and the
  evidence bundle's `references` array (emitted as RO-Crate
  `citation` entities).
- **Example consumer** at [`examples/example-consumer/`](examples/example-consumer/)
  + [CONSUMERS.md](CONSUMERS.md). Copy and replace the prompt.

**Foundation (Phase 0):**

- **5-layer memory** — identity (manual), primer (auto-rewritten per
  run), live context, [Hindsight](#) knowledge graph, Obsidian vault.
- **Dual Ollama routing** with model-aware URL cache and judge-model
  circuit breaker; single-flight refresh.
- **Sandboxed execution** on configurable SSH targets (Python / Bash /
  JavaScript), with verify-then-deploy and persistent project layout.
- **Tool registry** + dispatcher with a learned-safety **Gates** layer.
- **MCP server** at `/mcp` for external LLM clients (Claude Desktop, etc.).
- **Vault writer** publishing per-run / per-project / per-model /
  per-target / per-error / daily notes to a NoteDiscovery host and a
  NAS mirror.
- **Live `/ws` stream** of every log line and status transition.
- **Prometheus `/metrics`** with disciplined cardinality (no `run_id`
  labels).

## Documentation

| Doc | Audience |
|---|---|
| [VISION.md](VISION.md) | Why this exists; the "platform-not-hub" principle. |
| [CONSUMERS.md](CONSUMERS.md) | Public-API surface for consumer projects + minimum-viable consumer snippet. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Module layout + per-run data flow. |
| [ROADMAP.md](ROADMAP.md) | Phase-by-phase task list. |
| [CLAUDE.md](CLAUDE.md) | Internal guide for AI assistants editing this repo. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Local dev, conventions, where things go. |
| [RUNBOOK.md](RUNBOOK.md) | Operational tasks (service control, restore, secret rotation). |
| [SECURITY.md](SECURITY.md) | Threat model, secret handling, vuln reporting. |

## Quickstart

```bash
git clone https://github.com/ernesto01louis/ai-orchestrator.git
cd ai-orchestrator
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # edit Ollama URLs + SSH targets
cp .env.example .env                 # fill in GOTIFY_TOKEN etc.
uvicorn app:app --host 0.0.0.0 --port 8000
```

Then `curl http://127.0.0.1:8000/health` should return green.

See [RUNBOOK.md](RUNBOOK.md) for service-control and end-to-end
smoke commands.

## License

[Apache 2.0](LICENSE).
