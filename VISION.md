# VISION.md — why this orchestrator exists

> The promised "platform-not-hub" doc. If you're reading this from
> the README's documentation table — this is it.

## The one-line version

**The orchestrator is generic infrastructure for running AI-driven
research campaigns. Anything domain-specific belongs in a consumer
project, never here.**

## The problem this solves

A solo engineer wants to do real research — aerodynamics, RF, music
generation, protein folding, anything — and have an LLM swarm do the
heavy lifting. Off-the-shelf agent frameworks give you the loop but
none of the things that make a research result *citable*:

- A 5-layer memory system that survives across runs, projects, and weeks
- A sandbox you can trust to execute generated code without melting your
  laptop
- Per-run + per-campaign **SHA256 manifests** with a **Merkle root**
- **Citation-grade evidence bundles** (RO-Crate 1.2 / WRROC, signed
  with Ed25519, REFORMS + NeurIPS auto-fill, Model Cards, Datasheets
  for Datasets)
- A Python SDK so any consumer can drive it without coupling to its
  internals

The orchestrator is the platform that gives you all of those. It
deliberately knows nothing about your domain.

## The platform-not-hub principle

There are two ways this codebase could go wrong, and one of them is
what we explicitly avoid:

| Pattern | Looks like | Outcome |
|---|---|---|
| **Hub** (avoid) | `core/aero/`, `def compute_cl_cd(...)`, NACA-airfoil parsers in `references_pkg/`, `if domain == "rf":` branches in `orchestration/` | One operator's project leaks into every other operator's runs. The orchestrator becomes unmaintainable the moment a second consumer project appears. |
| **Platform** (the goal) | The orchestrator exposes generic primitives — campaigns, runs, evidence, memory, SSH targets, references. Consumer projects (in *their own repos*) import the SDK, define their own campaign templates with their own prompts, and never touch this codebase. | The orchestrator stays small. Each consumer ships independently. Three different operators with three different domains share the same orchestrator without merge conflicts. |

The test for "does this change belong in the orchestrator?" is:

> *Would an unrelated research project (protein folding, algorithmic
> trading, music generation) also benefit from this change?*

- **Yes** → belongs here. Memory layers, evidence bundle, SkyPilot
  bursts, HITL modes, NoteDiscovery grounding — all generic.
- **No** → belongs in a consumer project. Aerodynamics solvers, RF DF
  models, audio plugins, antenna designs — anything tied to a
  specific scientific or engineering domain.

If you're tempted to introduce a "plugin" abstraction to bridge the
gap: read [docs/PLUGINS.md](docs/PLUGINS.md) first. Some narrowly-scoped
*tooling* integrations (image generation, Blender control, eval
harnesses) live as optional `requirements-*.txt` extras and are
documented there. They're plugins because they're *interchangeable
research-tools*, not because they're a single operator's domain.

## What counts as platform code

Anything that goes in this repo must be:

- **Domain-neutral.** No file path, function name, prompt, schema
  field, or comment references aerodynamics, CFD, RF, antennas,
  torchsig, audio, proteins, finance, or any other vertical.
- **Reusable by ≥3 hypothetical consumers.** If you can only name one
  consumer who would use a feature, it's a consumer-project feature,
  not a platform feature.
- **Testable without domain data.** The orchestrator's test suite
  uses trivial math examples (`-(x - 3.0)² + sin(x)`) precisely so
  the platform stays unentangled from real domain corpora.

`examples/example-consumer/` is the canonical demonstration:
domain-neutral, math-only, imports nothing from the orchestrator's
internals — only the published `ai-orchestrator-client` SDK.
[CONSUMERS.md](CONSUMERS.md) captures the public contract a consumer
project gets to depend on.

## What counts as consumer code

A consumer project is a *separate repo* that imports the orchestrator
through one of three contracts:

1. **REST** — `POST /campaigns`, `GET /campaigns/{id}/tree`, etc.
   Stable since Phase 1.1.
2. **WebSocket `/ws`** — log + status broadcasts. Stable since Phase 0.
3. **Python SDK** — [`ai-orchestrator-client`](https://pypi.org/project/ai-orchestrator-client/)
   on PyPI. Stable across orchestrator MINOR versions; major version
   bumps document any breaking change.

The consumer owns its prompts, its scientific assumptions, its data,
its citation list. The orchestrator owns the loop, the memory, the
sandbox, and the evidence.

## What we explicitly do NOT build

From [CLAUDE.md](CLAUDE.md):

- MLflow / Aim / W&B (use `model_stats`)
- Hydra (already three config systems; don't add a fourth)
- Kubernetes (Proxmox LXC topology is sufficient forever)
- Custom workflow engine (use Prefect)
- Custom vector database (Hindsight + embedding cache cover it)
- Custom agent framework (the existing loop is mature)
- Multi-tenancy (solo project; not needed)
- **Domain-specific code** (aero, CFD, RF, antennas, specific hardware)

## Why multi-orchestrator federation was dropped

The original roadmap had Phase 3.5: *multi-orchestrator federation* —
the idea was that several orchestrators could share campaigns + evidence
across a tailnet. We dropped it because:

1. **A solo operator does not need it.** One orchestrator per homelab
   handles every consumer project we've imagined. The bottleneck is
   GPU memory, not orchestrator capacity.
2. **It adds a coordination layer** (CRDTs / consensus / shared
   state) for ~zero current users. Sigstore + DSSE already give us
   verifiable cross-orchestrator artifacts when we eventually need
   them.
3. **Per-orchestrator independence is a feature.** Each instance keeps
   its own memory, its own gates, its own model stats. Federation
   would dilute that on the wrong side of the cost / benefit line.

If three operators eventually want to share campaigns: revisit. Until
then: dropped.

## Phase orientation (as of 2026-05-11)

| Phase | What | Status |
|---|---|---|
| 0 | Refactor + safety net | DONE (`v0.1.0-phase0`) |
| 1.1–1.8 | Research-platform capabilities | DONE (`v0.1.1` → `v0.1.8`) |
| 2.1–2.5 | Durability + observability + cloud burst | DONE (`v0.2.1` → `v0.2.5`) |
| 2.6 | New UI | In flight on `feat/phase2.6-new-ui-foundation` |
| 3.1–3.4 | HITL, SmartPause, NoteDiscovery, example consumer | DONE (`v0.3.1` → `v0.3.4`) |
| 3.5 | Multi-orchestrator federation | **Dropped** (this doc) |

[ROADMAP.md](ROADMAP.md) is the canonical task list with commit
references; this doc is the *why*.

## When this doc lies

[CLAUDE.md](CLAUDE.md) and [ROADMAP.md](ROADMAP.md) are auto-loaded by
every session, so they drift faster than VISION. If you're a future
contributor and this file disagrees with the codebase, trust the code,
update the doc.
