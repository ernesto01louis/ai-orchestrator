"""Example consumer entrypoint — Phase 3.4.

Loads a campaign template from YAML, posts it to a running orchestrator
through the ``ai-orchestrator-client`` SDK, streams new runs as they
appear, downloads the Phase 1.2 evidence bundle on completion, and
verifies the Phase 1.5 Merkle root.

This script imports only from ``ai_orchestrator_client`` — it never
reaches into the orchestrator's internal modules. That's the whole
point of the example: it's the public contract, frozen.

Run::

    pip install -r requirements.txt
    python run.py                                # localhost:8000
    ORCHESTRATOR_URL=http://host:8000 python run.py
    ORCHESTRATOR_TOKEN=secret python run.py
    python run.py --template path/to/other.yaml
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml
from ai_orchestrator_client import (
    BearerTokenAuth,
    CampaignCreate,
    OrchestratorClient,
)


def load_template(path: Path) -> CampaignCreate:
    """Parse a campaign-template YAML file into a ``CampaignCreate``."""
    return CampaignCreate(**yaml.safe_load(path.read_text()))


def run_example(
    client: OrchestratorClient,
    request: CampaignCreate,
    *,
    poll_interval_seconds: float = 2.0,
    max_poll_interval_seconds: float = 10.0,
) -> int:
    """Drive a campaign end-to-end against ``client``.

    Returns 0 on success (Merkle valid), 1 on Merkle failure.
    Separated from ``main`` so tests can inject a pre-built client.
    """
    print("-> POST /campaigns")
    ack = client.start_campaign(request)
    print(f"   campaign_id={ack.campaign_id}  run_count={ack.run_count}")

    campaign = client.get_campaign(ack.campaign_id)
    print("-> streaming runs as they appear:")
    for run in campaign.iter_runs(
        client,
        poll_interval_seconds=poll_interval_seconds,
        max_poll_interval_seconds=max_poll_interval_seconds,
    ):
        print(f"   new: run_id={run.run_id}  params={run.params}  phase={run.phase}")

    evidence = client.get_evidence(ack.campaign_id)
    artifact_count = len(evidence.get("artifacts", [])) if isinstance(evidence, dict) else 0
    print(f"-> evidence bundle ready  artifacts={artifact_count}")

    verify = client.verify_campaign_merkle(ack.campaign_id)
    print(f"-> verify_campaign_merkle: valid={verify.valid}  status={verify.status}")
    return 0 if verify.valid else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--template",
        default=str(Path(__file__).parent / "template.yaml"),
        help="Path to a campaign-template YAML (default: ./template.yaml)",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("ORCHESTRATOR_URL", "http://127.0.0.1:8000"),
        help="Orchestrator base URL (default: $ORCHESTRATOR_URL or localhost:8000)",
    )
    args = parser.parse_args(argv)

    token = os.environ.get("ORCHESTRATOR_TOKEN")
    auth = BearerTokenAuth(token) if token else None
    request = load_template(Path(args.template))

    print(f"-> orchestrator: {args.url}")
    with OrchestratorClient(base_url=args.url, auth=auth) as client:
        return run_example(client, request)


if __name__ == "__main__":
    sys.exit(main())
