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

**Status:** Phase 0 complete (`v0.1.0-phase0`). Not yet a stable platform —
campaign + evidence-bundle abstractions land in Phase 1.

---

## Features that exist today

- **5-layer memory:** identity (manual), primer (auto-rewritten per run),
  live context, [Hindsight](#) knowledge graph, Obsidian vault.
- **Dual Ollama routing** with model-aware URL cache and judge-model circuit breaker.
- **Sandboxed execution** on configurable SSH targets (Python / Bash /
  JavaScript), with verify-then-deploy and persistent project layout.
- **Tool registry** + dispatcher with a learned-safety **Gates** layer
  (failed commands auto-promote to blocking gates after N occurrences).
- **MCP server** at `/mcp` for external LLM clients (Claude Desktop, etc.).
- **Vault writer** that publishes per-run / per-project / per-model /
  per-target / per-error / daily / index notes to a NoteDiscovery host
  and a NAS mirror.
- **Live `/ws` stream** of every log line and status transition.

## What's coming (Phase 1)

Campaigns (parameterized research missions), signed evidence bundles,
Prefect 3.x as the workflow engine, DVC for data versioning, SHA256
artifact manifests, and a published Python client library
(`ai-orchestrator-client` on PyPI) — the consumer contract that lets
external research projects depend on the orchestrator without pulling
in any of its internals.

See [ROADMAP.md](ROADMAP.md) for the full phase plan.

## Documentation

| Doc | Audience |
|---|---|
| [VISION.md](VISION.md) | Why this exists; the "platform-not-hub" principle. *(coming Phase 1)* |
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
