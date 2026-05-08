# CONSUMERS.md — building a consumer project

This orchestrator is a **generic platform**. Domain code (aerodynamics
optimization, CFD, RF DF ML, music generation, protein folding, anything
specific) belongs in a *consumer project* that imports the public Python
SDK ([`ai-orchestrator-client`](https://pypi.org/project/ai-orchestrator-client/))
and posts campaigns over the orchestrator's REST + WebSocket surface.

If you're tempted to merge domain logic into this repo, stop and re-read
the "What this project is — and isn't" section of [CLAUDE.md](CLAUDE.md).

## Reference implementation

[`examples/example-consumer/`](examples/example-consumer/) is a working,
domain-neutral consumer (trivial math optimization). Copy that directory
and replace its prompt + sweep parameter with whatever you want to vary.

## The public surface

A consumer needs only these symbols from `ai_orchestrator_client`:

| Symbol | What it does |
|---|---|
| `OrchestratorClient(base_url=..., auth=...)` | Sync client; context manager. |
| `AsyncOrchestratorClient(...)` | Async equivalent (same surface, `async/await`). |
| `BearerTokenAuth(token)` | Phase 1.7 bearer-token auth. |
| `CampaignCreate`, `CampaignTemplate` | Pydantic request shapes. |
| `client.start_campaign(req)` → `CampaignAck` | POST /campaigns. |
| `client.get_campaign(id)` → `Campaign` | GET /campaigns/{id}. |
| `Campaign.iter_runs(client)` | Stream runs as Prefect populates them. Solves the empty-`runs[]` race after `start_campaign`. |
| `client.get_evidence(id)` | GET /campaigns/{id}/evidence — Phase 1.2 bundle. |
| `client.download_evidence_crate(id)` | GET /campaigns/{id}/evidence.crate.zip — RO-Crate ZIP. |
| `client.verify_campaign_merkle(id)` | Phase 1.5 Merkle integrity check. |
| `client.iter_logs(run_id)` (async only) | Tail `/ws` log + status events for a run. |

The full export list is in
[`ai_orchestrator_client/__init__.py`](https://github.com/ernesto01louis/ai-orchestrator-client/blob/main/ai_orchestrator_client/__init__.py)
under `__all__`.

## Minimum-viable consumer

```python
from ai_orchestrator_client import (
    BearerTokenAuth, CampaignCreate, CampaignTemplate, OrchestratorClient,
)

req = CampaignCreate(
    name="my-sweep",
    hypothesis="result is invariant across two seeds",  # REFORMS §1
    template=CampaignTemplate(
        project_name="myproj-{seed}",
        prompt="Print 'hello from seed {seed}'.",
        planner_model="qwen2.5-coder:14b",
        generator_models=["qwen2.5-coder:14b"],
        judge_model="qwen2.5-coder:14b",
        deploy_target="local",  # one of your configured ssh_targets
    ),
    params={"seed": [1, 2]},
)

with OrchestratorClient(base_url="http://orchestrator:8000") as client:
    ack = client.start_campaign(req)
    campaign = client.get_campaign(ack.campaign_id)
    for run in campaign.iter_runs(client):
        print(run.run_id, run.params, run.phase)
    verify = client.verify_campaign_merkle(ack.campaign_id)
    assert verify.valid
```

## Rules of the road

1. **Never import orchestrator-internal modules.** Anything under `core.`,
   `api.`, `orchestration.`, `evidence.`, `memory_pkg.`, `llm.`,
   `execution.`, `notifications.`, `gates.`, or `tools.` is private.
   The smoke test at
   [`tests/examples/test_example_consumer_smoke.py`](tests/examples/test_example_consumer_smoke.py)
   enforces this with a source-text guard.
2. **Pin the SDK with a range** (e.g. `ai-orchestrator-client>=0.1.0a0,<0.2`)
   so a consumer can pick up MCP-contract MINOR bumps without breaking.
3. **Hypothesis is required.** REFORMS §1 pre-registration —
   `CampaignCreate` rejects empty/whitespace-only values. State the
   question your campaign answers; the evidence bundle's calculators
   operate against it.
4. **Templates live in your repo, not this one.** Copy
   `examples/example-consumer/template.yaml` and edit. The orchestrator
   doesn't hot-load templates from external paths today (Phase 1.1
   limitation).
5. **Use `Campaign.iter_runs` for streaming.** It handles the
   empty-`runs[]` race after `start_campaign` (the runner thread takes a
   moment to populate combos). Don't roll your own polling loop.

## See also

- [examples/example-consumer/README.md](examples/example-consumer/README.md) — runnable reference
- [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md) — MCP-side surface (Phase 1.7 contract `1.0.0`)
- [ARCHITECTURE.md](ARCHITECTURE.md) — full data-flow diagram
- [ROADMAP.md](ROADMAP.md) — what's coming
