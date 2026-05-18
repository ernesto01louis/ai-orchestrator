"""External consumer registry + capability dispatch (Phase 3.6).

A *consumer* is an external research project (rf-direction-finding,
aero-research-platform, …) that registers with the orchestrator and
declares a set of capabilities. Registration is the discovery surface:
``GET /consumers`` lets the planner (and operators) see which projects
offer which capabilities.

This module covers the registry (register / list / get / delete /
heartbeat) and capability dispatch — ``POST /capabilities/{cap}/invoke``
proxies a call to whichever registered consumer offers the capability.
The data-plane push endpoints (memory / vault / notify / evidence) are
appended by a later Phase 3.6 commit but share the helpers here.

Every endpoint is bearer-auth gated by the existing
``BearerTokenAuthMiddleware`` — there is no public-path entry for
``/consumers``. ``consumers.json`` is JSON-canonical (file-locked); the
``callback_token`` a consumer supplies is an outbound credential the
orchestrator presents when invoking that consumer, so it is stored
usable and redacted out of every GET response.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import CONSUMERS_DISPATCH_TIMEOUT_SECONDS
from core.metrics import observe_consumer_invocation, observe_consumer_registration
from memory_pkg import load_consumers, save_consumers

router = APIRouter()

# consumer_id is used in filesystem-adjacent contexts (vault note names,
# log lines); keep it to a safe, traversal-free character set.
_CONSUMER_ID = re.compile(r"^(?!.*\.\.)[a-zA-Z0-9_\-\.]{1,64}$")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_consumer_id(consumer_id: str) -> str:
    if not _CONSUMER_ID.match(consumer_id or ""):
        raise HTTPException(
            status_code=400,
            detail=(
                "consumer_id must be 1-64 chars of [A-Za-z0-9_-.] "
                "with no '..' sequence"
            ),
        )
    return consumer_id


def _public_view(record: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a consumer record safe to serialise to clients —
    the outbound ``callback_token`` is redacted to a presence flag."""
    view = {k: v for k, v in record.items() if k != "callback_token"}
    view["has_callback_token"] = bool(record.get("callback_token"))
    return view


def _get_consumer_or_404(consumer_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the registry and return (registry, record) or raise 404."""
    registry = load_consumers()
    record = registry.get(consumer_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"No consumer registered as '{consumer_id}'"
        )
    return registry, record


class ConsumerRegisterRequest(BaseModel):
    """Body for ``POST /consumers/register``.

    ``callback_token`` is optional — a consumer that only pushes data in
    (never receives dispatched capability calls) can omit it.
    """

    consumer_id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)
    capabilities: list[str] = Field(default_factory=list)
    callback_token: str | None = None
    description: str = ""


@router.post("/consumers/register", status_code=201)
def register_consumer(req: ConsumerRegisterRequest) -> dict[str, Any]:
    """Register a consumer, or update an existing registration.

    Idempotent upsert keyed on ``consumer_id``. On update the original
    ``registered_at`` is preserved; ``capabilities`` / ``base_url`` /
    ``callback_token`` are replaced wholesale with the new payload.
    """
    consumer_id = _validate_consumer_id(req.consumer_id)
    registry = load_consumers()
    existing = registry.get(consumer_id)
    now = _utcnow_iso()

    record: dict[str, Any] = {
        "consumer_id": consumer_id,
        "name": req.name,
        "base_url": req.base_url.rstrip("/"),
        "capabilities": sorted(set(req.capabilities)),
        "callback_token": req.callback_token,
        "description": req.description,
        "registered_at": existing.get("registered_at", now) if existing else now,
        "updated_at": now,
        "last_heartbeat": existing.get("last_heartbeat") if existing else None,
        "last_health": existing.get("last_health") if existing else None,
    }
    registry[consumer_id] = record
    save_consumers(registry)

    outcome = "updated" if existing else "registered"
    observe_consumer_registration(outcome)
    return {"status": outcome, "consumer": _public_view(record)}


@router.get("/consumers")
def list_consumers() -> dict[str, Any]:
    """List registered consumers — the capability-discovery surface."""
    registry = load_consumers()
    consumers = [_public_view(r) for r in registry.values()]
    consumers.sort(key=lambda c: c.get("consumer_id", ""))
    return {"total": len(consumers), "consumers": consumers}


@router.get("/consumers/{consumer_id}")
def get_consumer(consumer_id: str) -> dict[str, Any]:
    """Detail for one consumer, including last heartbeat / health probe."""
    _registry, record = _get_consumer_or_404(consumer_id)
    return _public_view(record)


@router.delete("/consumers/{consumer_id}")
def deregister_consumer(consumer_id: str) -> dict[str, Any]:
    """Remove a consumer from the registry."""
    registry, _record = _get_consumer_or_404(consumer_id)
    registry.pop(consumer_id, None)
    save_consumers(registry)
    observe_consumer_registration("deregistered")
    return {"status": "deregistered", "consumer_id": consumer_id}


@router.post("/consumers/{consumer_id}/heartbeat")
def consumer_heartbeat(consumer_id: str) -> dict[str, Any]:
    """Liveness ping — a consumer calls this to stamp ``last_heartbeat``."""
    registry, record = _get_consumer_or_404(consumer_id)
    now = _utcnow_iso()
    record["last_heartbeat"] = now
    registry[consumer_id] = record
    save_consumers(registry)
    return {"status": "ok", "consumer_id": consumer_id, "heartbeat_at": now}


# ── capability dispatch ──────────────────────────────────────────────


def _find_capability_provider(capability: str) -> dict[str, Any] | None:
    """Return the registered consumer offering ``capability``, or None.

    First match wins — the registry is a flat map and a capability is
    expected to be offered by at most one consumer in practice.
    """
    for record in load_consumers().values():
        if capability in (record.get("capabilities") or []):
            return record
    return None


class CapabilityInvokeRequest(BaseModel):
    """Body for ``POST /capabilities/{capability}/invoke`` — an opaque
    JSON payload forwarded verbatim to the consumer's capability handler."""

    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/capabilities/{capability}/invoke")
def invoke_capability(
    capability: str, req: CapabilityInvokeRequest
) -> dict[str, Any]:
    """Dispatch a capability call to the consumer that offers it.

    Looks up the provider, POSTs the payload to
    ``{base_url}/capabilities/{capability}`` presenting the consumer's
    stored ``callback_token`` as a bearer token, and returns the
    consumer's response. The outbound call is bounded by
    ``consumers.dispatch_timeout_seconds`` so a slow consumer can never
    hang this request.

    404 — no registered consumer offers the capability.
    502 — the consumer returned an error or unparseable body.
    504 — the consumer did not respond within the dispatch timeout.
    """
    provider = _find_capability_provider(capability)
    if provider is None:
        observe_consumer_invocation(capability, "not_found")
        raise HTTPException(
            status_code=404,
            detail=f"No registered consumer offers capability '{capability}'",
        )

    url = f"{provider['base_url']}/capabilities/{capability}"
    headers = {}
    token = provider.get("callback_token")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.post(
            url,
            json=req.payload,
            headers=headers,
            timeout=CONSUMERS_DISPATCH_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        observe_consumer_invocation(capability, "timeout")
        raise HTTPException(
            status_code=504,
            detail=f"Consumer '{provider['consumer_id']}' timed out",
        ) from None
    except requests.RequestException as exc:
        observe_consumer_invocation(capability, "consumer_error")
        raise HTTPException(
            status_code=502,
            detail=f"Consumer '{provider['consumer_id']}' unreachable: {exc}",
        ) from None

    if resp.status_code >= 400:
        observe_consumer_invocation(capability, "consumer_error")
        raise HTTPException(
            status_code=502,
            detail=(
                f"Consumer '{provider['consumer_id']}' returned "
                f"{resp.status_code}"
            ),
        )

    try:
        result = resp.json()
    except ValueError:
        observe_consumer_invocation(capability, "consumer_error")
        raise HTTPException(
            status_code=502,
            detail=f"Consumer '{provider['consumer_id']}' returned non-JSON",
        ) from None

    observe_consumer_invocation(capability, "success")
    return {
        "capability": capability,
        "consumer_id": provider["consumer_id"],
        "result": result,
    }
