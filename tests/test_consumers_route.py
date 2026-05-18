"""Tests for the Phase 3.6 external consumer registry (/consumers).

Covers the registry CRUD, capability dispatch, the data-plane push
endpoints, the capability gate, and bearer-auth enforcement. Endpoint
logic is exercised by calling the route functions directly (fast, no
middleware); auth is checked against a minimal app wired with the real
``BearerTokenAuthMiddleware``.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from api.routes import consumers as mod
from api.routes.consumers import (
    CapabilityInvokeRequest,
    ConsumerEvidenceRequest,
    ConsumerMemoryRequest,
    ConsumerNotifyRequest,
    ConsumerRegisterRequest,
    ConsumerVaultRequest,
    consumer_heartbeat,
    consumer_notify,
    consumer_push_evidence,
    consumer_write_memory,
    consumer_write_vault,
    deregister_consumer,
    get_consumer,
    invoke_capability,
    list_consumers,
    register_consumer,
    router,
)
from core.auth import BearerTokenAuthMiddleware
from memory_pkg import load_consumers, save_consumers


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot consumers.json, run each test on an empty registry,
    then restore — tests never clobber real on-disk state."""
    original = load_consumers()
    save_consumers({})
    try:
        yield
    finally:
        save_consumers(original)


def _register(consumer_id="rfdf", capabilities=None, callback_token="tok"):
    return register_consumer(
        ConsumerRegisterRequest(
            consumer_id=consumer_id,
            name="rf-direction-finding",
            base_url="http://127.0.0.1:8731",
            capabilities=capabilities or ["rf.doa.run"],
            callback_token=callback_token,
            description="test consumer",
        )
    )


# ── registry CRUD ────────────────────────────────────────────────────


def test_register_then_get():
    ack = _register()
    assert ack["status"] == "registered"
    rec = get_consumer("rfdf")
    assert rec["consumer_id"] == "rfdf"
    assert rec["capabilities"] == ["rf.doa.run"]
    # callback_token never leaks; presence flag instead
    assert "callback_token" not in rec
    assert rec["has_callback_token"] is True


def test_register_is_idempotent_upsert():
    first = _register()
    second = _register(capabilities=["rf.classify"])
    assert second["status"] == "updated"
    # registered_at preserved across the update
    assert second["consumer"]["registered_at"] == first["consumer"]["registered_at"]
    assert get_consumer("rfdf")["capabilities"] == ["rf.classify"]


def test_register_rejects_unsafe_consumer_id():
    with pytest.raises(HTTPException) as exc:
        register_consumer(
            ConsumerRegisterRequest(
                consumer_id="../escape", name="x", base_url="http://x"
            )
        )
    assert exc.value.status_code == 400


def test_list_and_delete():
    _register("rfdf")
    _register("aero")
    listed = list_consumers()
    assert listed["total"] == 2
    assert {c["consumer_id"] for c in listed["consumers"]} == {"rfdf", "aero"}
    deregister_consumer("rfdf")
    assert list_consumers()["total"] == 1


def test_get_unknown_is_404():
    with pytest.raises(HTTPException) as exc:
        get_consumer("ghost")
    assert exc.value.status_code == 404


def test_heartbeat_stamps_last_heartbeat():
    _register()
    assert get_consumer("rfdf")["last_heartbeat"] is None
    consumer_heartbeat("rfdf")
    assert get_consumer("rfdf")["last_heartbeat"] is not None


# ── capability dispatch ──────────────────────────────────────────────


def test_invoke_unknown_capability_is_404():
    _register(capabilities=["rf.doa.run"])
    with pytest.raises(HTTPException) as exc:
        invoke_capability("rf.nope", CapabilityInvokeRequest(payload={}))
    assert exc.value.status_code == 404


def test_invoke_proxies_to_consumer(monkeypatch):
    _register(capabilities=["rf.doa.run"], callback_token="sek")

    captured = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"bearing_deg": 47.0}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(mod.requests, "post", _fake_post)
    out = invoke_capability(
        "rf.doa.run", CapabilityInvokeRequest(payload={"freq": 868})
    )
    assert out["result"] == {"bearing_deg": 47.0}
    assert captured["url"] == "http://127.0.0.1:8731/capabilities/rf.doa.run"
    assert captured["json"] == {"freq": 868}
    assert captured["headers"]["Authorization"] == "Bearer sek"


def test_invoke_consumer_error_is_502(monkeypatch):
    _register(capabilities=["rf.doa.run"])

    class _Resp:
        status_code = 500

    monkeypatch.setattr(
        mod.requests, "post", lambda *a, **k: _Resp()
    )
    with pytest.raises(HTTPException) as exc:
        invoke_capability("rf.doa.run", CapabilityInvokeRequest(payload={}))
    assert exc.value.status_code == 502


def test_invoke_timeout_is_504(monkeypatch):
    _register(capabilities=["rf.doa.run"])

    def _raise(*a, **k):
        raise mod.requests.Timeout()

    monkeypatch.setattr(mod.requests, "post", _raise)
    with pytest.raises(HTTPException) as exc:
        invoke_capability("rf.doa.run", CapabilityInvokeRequest(payload={}))
    assert exc.value.status_code == 504


# ── data-plane push + capability gate ────────────────────────────────


def test_memory_push_requires_capability(monkeypatch):
    _register(capabilities=[])  # no memory.write
    monkeypatch.setattr(mod, "hindsight_retain", lambda *a, **k: {"ok": True})
    with pytest.raises(HTTPException) as exc:
        consumer_write_memory("rfdf", ConsumerMemoryRequest(content="x"))
    assert exc.value.status_code == 403


def test_memory_push_succeeds(monkeypatch):
    _register(capabilities=["memory.write"])
    monkeypatch.setattr(mod, "hindsight_retain", lambda *a, **k: {"ok": True})
    out = consumer_write_memory("rfdf", ConsumerMemoryRequest(content="detected"))
    assert out == {"status": "success", "retained": True}


def test_vault_push_succeeds(monkeypatch):
    _register(capabilities=["vault.write"])
    monkeypatch.setattr(
        mod, "vault_write_consumer_note", lambda *a, **k: "/vault/x.md"
    )
    out = consumer_write_vault(
        "rfdf", ConsumerVaultRequest(title="calib", body="matrix", tags=["rf"])
    )
    assert out["status"] == "success"
    assert out["path"] == "/vault/x.md"


def test_notify_push_succeeds(monkeypatch):
    _register(capabilities=["notify.send"])
    sent = {}
    monkeypatch.setattr(
        mod, "send_notification",
        lambda title, message, **k: sent.update(title=title, message=message),
    )
    out = consumer_notify(
        "rfdf", ConsumerNotifyRequest(title="drift", message="cal drift")
    )
    assert out["status"] == "sent"
    assert sent["title"] == "[rfdf] drift"


def test_evidence_push_persists_bundle(monkeypatch, tmp_path):
    _register(capabilities=["evidence.push"])
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    out = consumer_push_evidence(
        "rfdf",
        ConsumerEvidenceRequest(
            bundle_id="bundle-1", bundle={"quality": "citation-grade"}
        ),
    )
    assert out["status"] == "stored"
    written = tmp_path / "campaigns" / "consumer-rfdf" / "bundle-1" / "bundle.json"
    assert written.is_file()
    assert '"quality"' in written.read_text()


def test_evidence_push_rejects_unsafe_bundle_id(monkeypatch, tmp_path):
    _register(capabilities=["evidence.push"])
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    with pytest.raises(HTTPException) as exc:
        consumer_push_evidence(
            "rfdf",
            ConsumerEvidenceRequest(bundle_id="../etc", bundle={}),
        )
    assert exc.value.status_code == 400


def test_push_unknown_consumer_is_404():
    with pytest.raises(HTTPException) as exc:
        consumer_write_memory("ghost", ConsumerMemoryRequest(content="x"))
    assert exc.value.status_code == 404


# ── bearer-auth enforcement ──────────────────────────────────────────


def test_consumers_endpoints_require_auth():
    """The /consumers router carries no public-path exemption — every
    endpoint is rejected without a valid bearer token."""
    fast = FastAPI()
    fast.include_router(router)
    fast.add_middleware(BearerTokenAuthMiddleware, token="s3kret")
    client = TestClient(fast)

    assert client.get("/consumers").status_code == 401
    assert client.get(
        "/consumers", headers={"Authorization": "Bearer s3kret"}
    ).status_code == 200
