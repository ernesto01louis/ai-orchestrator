# Architecture

Stub — to be filled out during Phase 0.h with a full module-layout diagram and data-flow walkthrough.

Until then, see [CLAUDE.md](CLAUDE.md) for the "what exists" inventory and [VISION.md](VISION.md) for the design philosophy.

## Target module layout (Phase 0 deliverable)

```
app.py                  # FastAPI wiring + lifespan + router includes (~150 lines)
core/                   # paths, config, locks, run status, logging, ws_broadcast
llm/                    # ollama clients, JSON repair, code extraction
notifications/          # gotify/ntfy senders and formatters
execution/              # ssh, verify, language handlers, sandbox, deploy, inspector, deps
tools/                  # registry + dispatch with gates integration
references/             # PDF/file upload + markdown conversion + vision description
memory/                 # embedding, stats, layers, sessions, targets, hindsight, vault, context
orchestration/          # run_orchestration loop, agents (planner/judge/generator/optimizer/troubleshooter), context builders
api/                    # FastAPI routers, split by area
agents/                 # already extracted — per-role configs + loader
dream.py, gates.py, mcp_server.py  # already extracted — keep at root
```

Data files remain under `memory/` (JSON), `vault/` (Obsidian markdown), `references/` (PDFs), `projects/` (generated artifacts).
