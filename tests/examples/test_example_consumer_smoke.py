"""Smoke tests for examples/example-consumer (Phase 3.4).

The example is the public-contract reference: it imports only from
``ai_orchestrator_client``. These tests guard the contract:

1. **Import test** — module imports cleanly; ``main``, ``run_example``,
   and ``load_template`` are exposed.
2. **Internal-import guard** — the example never reaches into
   orchestrator-internal packages (``core``, ``api``, ``orchestration``,
   …). Plain source-text scan; cheap and unambiguous.
3. **Template parses to a valid SDK request** — ``load_template`` on
   the shipped YAML returns a ``CampaignCreate`` whose validators pass.
4. **In-process route acceptance** — POST the loaded request through
   the FastAPI ``inprocess_client`` (TestClient) with the
   ``deploy_target`` rewritten to whatever the test env exposes; the
   campaigns route returns 200.

We deliberately don't drive ``run_example`` through ``OrchestratorClient``
+ ``httpx.ASGITransport`` — ASGITransport doesn't support ``Client.close``
on sync, and standing up a real uvicorn server for a smoke test is
overkill. Tests 1-4 cover the consumer-facing contract end-to-end short
of the wire.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "example-consumer"


def _load_example_module() -> Any:
    """Import examples/example-consumer/run.py without polluting sys.path."""
    path = EXAMPLE_DIR / "run.py"
    assert path.exists(), f"missing example: {path}"
    spec = importlib.util.spec_from_file_location("_example_consumer_run", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def test_example_imports_and_exposes_contract() -> None:
    """The example module imports and exposes the public callables."""
    mod = _load_example_module()
    try:
        assert callable(mod.main), "examples/example-consumer/run.py must expose main()"
        assert callable(mod.run_example), "must expose run_example()"
        assert callable(mod.load_template), "must expose load_template()"
        assert mod.load_template.__doc__, "load_template needs a docstring"
    finally:
        sys.modules.pop("_example_consumer_run", None)


def test_example_does_not_import_orchestrator_internals() -> None:
    """The example must depend ONLY on ai_orchestrator_client + PyYAML.

    A grep guard — if a future maintainer reaches into ``core.X`` /
    ``api.X`` / similar, this test fails loudly. The whole point of
    the example is that it's the public contract.
    """
    source = (EXAMPLE_DIR / "run.py").read_text()
    forbidden_prefixes = (
        "from core",
        "from api",
        "from orchestration",
        "from memory_pkg",
        "from evidence",
        "from llm",
        "from execution",
        "from notifications",
        "from gates",
        "from tools",
        "import core",
        "import api",
        "import orchestration",
    )
    for line in source.splitlines():
        stripped = line.strip()
        for bad in forbidden_prefixes:
            assert not stripped.startswith(bad), (
                f"example must not import orchestrator-internal modules: {stripped!r}"
            )


def test_load_template_returns_valid_campaign_create() -> None:
    """``load_template`` on the shipped YAML must produce a valid
    ``CampaignCreate``: hypothesis non-empty, params dict, template
    fields populated."""
    mod = _load_example_module()
    try:
        from ai_orchestrator_client import CampaignCreate

        req = mod.load_template(EXAMPLE_DIR / "template.yaml")

        assert isinstance(req, CampaignCreate)
        assert req.name == "example-consumer-quadratic"
        assert req.hypothesis.strip(), "hypothesis must be non-blank (REFORMS §1)"
        assert "seed" in req.params, "expected seed param sweep"
        assert isinstance(req.params["seed"], list) and len(req.params["seed"]) >= 2
        assert req.template.project_name == "example_consumer_seed_{seed}"
        assert req.template.generator_models, "needs at least one generator model"
        assert req.template.deploy_target, "deploy_target must be set in the YAML"
    finally:
        sys.modules.pop("_example_consumer_run", None)


@pytest.fixture
def smoke_client(inprocess_client: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """``orchestration.campaign.run_campaign`` no-op'd so the campaign
    sits queued without consuming Ollama. Mirrors tests/test_campaigns.py.
    """
    import orchestration.campaign as oc

    def _noop(_id: str) -> None:
        return None

    monkeypatch.setattr(oc, "run_campaign", _noop)
    return inprocess_client


def test_loaded_template_is_accepted_by_campaigns_route(
    smoke_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request that ``load_template`` produces must be accepted by
    the live ``POST /campaigns`` route. Closes the contract loop without
    needing an out-of-process orchestrator.
    """
    mod = _load_example_module()
    try:
        from core.config import SSH_TARGETS  # noqa: WPS433 — test-only

        req = mod.load_template(EXAMPLE_DIR / "template.yaml")
        # Override the YAML's pi-1 default to whatever the test config
        # actually has — same trick as tests/test_campaigns.py.
        req.template.deploy_target = next(iter(SSH_TARGETS.keys()))

        body = req.model_dump()
        resp = smoke_client.post("/campaigns", json=body)

        assert resp.status_code == 200, resp.text
        data = resp.json()
        cid = data["campaign_id"]
        try:
            assert data["run_count"] == len(req.params["seed"])
            assert data["status"] == "started"

            # Read-back proves the record persisted.
            r = smoke_client.get(f"/campaigns/{cid}")
            assert r.status_code == 200
            assert r.json()["name"] == req.name
        finally:
            from memory_pkg import load_campaigns, save_campaigns

            campaigns = load_campaigns()
            campaigns.pop(cid, None)
            save_campaigns(campaigns)
    finally:
        sys.modules.pop("_example_consumer_run", None)
