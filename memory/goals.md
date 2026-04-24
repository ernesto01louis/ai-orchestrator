# Goals

> This file tracks high-level project objectives and roadmap.
> It helps the orchestrator understand WHY tasks are being run,
> and enables context recovery after gaps in development.

## Active goals

### Build a self-hosted AI code orchestrator
- **Status**: In progress
- **Phase**: Strengthening memory system
- **Started**: 2026-03-20
- **Description**: An autonomous system that plans, generates, tests, judges, optimizes, and deploys code to Raspberry Pi targets using local Ollama models.
- **Key decisions made**:
  - qwen2.5:72b for planning and judging (on judge LXC)
  - qwen2.5-coder:32b + deepseek-coder:33b for generation (on main LXC)
  - Sandbox at /tmp, persistent deploy to ~/ai-projects/
  - Multi-language support: Python, Bash, JavaScript
  - Server-aware sandbox with port detection

## Roadmap

> Items roughly in priority order. Check items off as they're completed.

- [x] Core pipeline (plan → generate → judge → optimize → deploy)
- [x] Logic bug fixes (optimizer re-judging, lambda closure, troubleshooter retry)
- [x] Robustness (error handling, file locking, SSH safety)
- [x] Async endpoint with polling
- [x] Multi-file project support
- [x] Structured output (Ollama schema enforcement for planner/judge)
- [x] Persistent deployment (sandbox → ~/ai-projects/ promotion)
- [x] Multi-language support (Python, Bash, JavaScript)
- [x] Server-aware sandbox execution
- [x] Port awareness + system dependency installer + sudo allowlist
- [x] Positive/negative memory + model performance tracking
- [ ] 5-layer memory architecture (identity, primer, live context, hindsight, obsidian)
- [ ] Web UI dashboard
- [ ] Docker sandboxing for privileged tasks
- [ ] NAS snapshots as safety net
- [ ] Hindsight integration (retain/recall/reflect with local Ollama)
- [ ] Obsidian vault integration (knowledge graph + manual notes)
- [ ] Model performance-based dynamic assignment
- [ ] Prompt storage that improves over time

## Completed goals

None yet — the orchestrator project is the first and ongoing goal.
