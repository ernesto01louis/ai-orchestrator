# example-consumer

Reference implementation of a project that consumes the AI Orchestrator
through its **public** contract: the
[`ai-orchestrator-client`](https://pypi.org/project/ai-orchestrator-client/)
PyPI SDK plus the orchestrator's REST + WebSocket surface.

The task is a trivial math optimization on `-(x - 3.0)² + sin(x)`. There
is no domain content — copy this directory and replace the prompt + sweep
parameter with whatever you actually want to vary (aero, CFD, RF, music,
protein folding, anything). The orchestrator side stays unchanged.

## What it shows

| Step | SDK call |
|---|---|
| Build a request from a YAML template | `CampaignCreate(**yaml.safe_load(...))` |
| Post a campaign | `client.start_campaign(req)` |
| Stream runs as they appear | `campaign.iter_runs(client)` |
| Download the evidence bundle | `client.get_evidence(campaign_id)` |
| Verify Phase 1.5 Merkle integrity | `client.verify_campaign_merkle(campaign_id)` |

## Run it

1. Edit `template.yaml` — set `deploy_target` to a configured SSH target
   on your orchestrator (look at your `config.json` `ssh_targets`).

2. Install the SDK:

   ```sh
   pip install -r requirements.txt
   ```

3. Point at the orchestrator and run:

   ```sh
   # Localhost
   python run.py

   # Remote orchestrator
   ORCHESTRATOR_URL=http://192.168.2.218:8000 python run.py

   # With bearer-token auth (Phase 1.7)
   ORCHESTRATOR_TOKEN=$your_token python run.py
   ```

   Use `--template some/other.yaml` to point at a different template.

## Expected shape of the output

```
-> POST /campaigns  (server: http://127.0.0.1:8000)
   campaign_id=...  run_count=2
-> streaming runs as they appear:
   new: run_id=...  params={'seed': 1}  phase=planner
   new: run_id=...  params={'seed': 2}  phase=planner
-> evidence bundle ready  artifacts=N
-> verify_campaign_merkle: valid=True  status=valid
```

The stream terminates when every run reaches a terminal phase AND the
last poll yielded zero new runs (`Campaign.iter_runs` solves the
empty-`runs[]` race after `start_campaign`).

## What this is *not*

* Not a place to dump domain code. The orchestrator is **generic** —
  domain logic belongs in a consumer project that imports the SDK, like
  this one. See `CONSUMERS.md` at the repo root.
* Not a hard dependency on PyYAML — `yaml` is used here only to load the
  template. Your consumer can build a `CampaignCreate` any way it likes
  (TOML, JSON, code, programmatic param expansion, etc.).
* Not async. The async surface is identical (`AsyncOrchestratorClient`);
  see `/opt/ai-orchestrator-client/examples/async_campaign.py` for the
  async pattern, or `examples/stream_logs.py` in the SDK repo for log
  tailing through `/ws`.

## Public-API surface used

The example imports only:

```python
from ai_orchestrator_client import (
    BearerTokenAuth,
    CampaignCreate,
    OrchestratorClient,
)
```

All the model types (`CampaignTemplate`, `CampaignAck`, `CampaignTreeRun`,
…) are reachable through the same package and documented at
<https://github.com/ernesto01louis/ai-orchestrator-client>.
