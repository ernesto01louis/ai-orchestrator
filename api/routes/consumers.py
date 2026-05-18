"""External consumer registry + capability dispatch (Phase 3.6).

A *consumer* is an external research project (rf-direction-finding,
aero-research-platform, …) that registers with the orchestrator and
declares a set of capabilities. Registration is the discovery surface:
``GET /consumers`` lets the planner (and operators) see which projects
offer which capabilities.

This module covers the registry (register / list / get / delete /
heartbeat), capability dispatch — ``POST /capabilities/{cap}/invoke``
proxies a call to whichever registered consumer offers the capability —
and the data-plane push endpoints (``POST /consumers/{id}/{memory,
vault,notify,evidence}``). Each push is gated on the consumer having
declared the matching ``memory.write`` / ``vault.write`` /
``notify.send`` / ``evidence.push`` capability at registration.

Every endpoint is bearer-auth gated by the existing
``BearerTokenAuthMiddleware`` — there is no public-path entry for
``/consumers``. ``consumers.json`` is JSON-canonical (file-locked); the
``callback_token`` a consumer supplies is an outbound credential the
orchestrator presents when invoking that consumer, so it is stored
usable and redacted out of every GET response.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import CONSUMERS_DISPATCH_TIMEOUT_SECONDS
from core.metrics import (
    observe_consumer_ingestion,
    observe_consumer_invocation,
    observe_consumer_registration,
)
from core.paths import REPO_ROOT
from memory_pkg import hindsight_retain, load_consumers, save_consumers, vault_write_consumer_note
from notifications import send_notification

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


# ── data-plane push ──────────────────────────────────────────────────
#
# A consumer pushes data INTO the orchestrator: Hindsight memory, L5
# vault notes, ntfy notifications, externally-produced evidence
# bundles. Each push is gated on the consumer having declared the
# matching generic capability at registration time — declaring
# "memory.write" / "vault.write" / "notify.send" / "evidence.push"
# alongside its domain capabilities is opt-in to the data plane.

_PUSH_CAPABILITY = {
    "memory": "memory.write",
    "vault": "vault.write",
    "notify": "notify.send",
    "evidence": "evidence.push",
}

# A consumer-supplied bundle_id is used as a directory name — keep it
# traversal-free.
_BUNDLE_ID = re.compile(r"^(?!.*\.\.)[a-zA-Z0-9_\-\.]{1,80}$")


def _require_capability(
    record: dict[str, Any], ingestion_type: str
) -> None:
    """Raise 403 unless the consumer declared the push capability."""
    needed = _PUSH_CAPABILITY[ingestion_type]
    if needed not in (record.get("capabilities") or []):
        observe_consumer_ingestion(ingestion_type, "forbidden")
        raise HTTPException(
            status_code=403,
            detail=(
                f"Consumer '{record['consumer_id']}' did not declare the "
                f"'{needed}' capability"
            ),
        )


class ConsumerMemoryRequest(BaseModel):
    """Body for ``POST /consumers/{id}/memory`` — a natural-language
    narrative Hindsight extracts facts from."""

    content: str = Field(..., min_length=1)


@router.post("/consumers/{consumer_id}/memory")
def consumer_write_memory(
    consumer_id: str, req: ConsumerMemoryRequest
) -> dict[str, Any]:
    """Push a Hindsight memory entry on behalf of a consumer."""
    _registry, record = _get_consumer_or_404(consumer_id)
    _require_capability(record, "memory")
    result = hindsight_retain(req.content, f"consumer-{consumer_id}")
    outcome = "success" if result is not None else "failure"
    observe_consumer_ingestion("memory", outcome)
    return {"status": outcome, "retained": result is not None}


class ConsumerVaultRequest(BaseModel):
    """Body for ``POST /consumers/{id}/vault`` — an L5 vault note."""

    title: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)
    tags: list[str] = Field(default_factory=list)


@router.post("/consumers/{consumer_id}/vault")
def consumer_write_vault(
    consumer_id: str, req: ConsumerVaultRequest
) -> dict[str, Any]:
    """Write an L5 vault note on behalf of a consumer."""
    _registry, record = _get_consumer_or_404(consumer_id)
    _require_capability(record, "vault")
    path = vault_write_consumer_note(consumer_id, req.title, req.body, req.tags)
    outcome = "success" if path else "failure"
    observe_consumer_ingestion("vault", outcome)
    return {"status": outcome, "path": path}


class ConsumerNotifyRequest(BaseModel):
    """Body for ``POST /consumers/{id}/notify`` — an ntfy/Gotify alert."""

    title: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    priority: int | None = None
    tags: list[str] = Field(default_factory=list)


@router.post("/consumers/{consumer_id}/notify")
def consumer_notify(
    consumer_id: str, req: ConsumerNotifyRequest
) -> dict[str, Any]:
    """Fire a notification on behalf of a consumer.

    ``send_notification`` early-returns when notifications are disabled
    in config and never raises — the endpoint reports the attempt.
    """
    _registry, record = _get_consumer_or_404(consumer_id)
    _require_capability(record, "notify")
    send_notification(
        f"[{consumer_id}] {req.title}",
        req.message,
        priority=req.priority,
        tags=req.tags or None,
    )
    observe_consumer_ingestion("notify", "success")
    return {"status": "sent", "consumer_id": consumer_id}


class ConsumerEvidenceRequest(BaseModel):
    """Body for ``POST /consumers/{id}/evidence``.

    The bundle stays in the consumer's own schema — the orchestrator
    persists it verbatim under ``campaigns/consumer-<id>/<bundle_id>/``
    and does not parse or validate the payload. ``bundle_id`` is
    optional; a ULID-shaped id is generated when omitted.
    """

    bundle_id: str | None = None
    bundle: dict[str, Any] = Field(default_factory=dict)


@router.post("/consumers/{consumer_id}/evidence")
def consumer_push_evidence(
    consumer_id: str, req: ConsumerEvidenceRequest
) -> dict[str, Any]:
    """Persist an externally-produced evidence bundle from a consumer."""
    _registry, record = _get_consumer_or_404(consumer_id)
    _require_capability(record, "evidence")

    bundle_id = req.bundle_id or uuid.uuid4().hex
    if not _BUNDLE_ID.match(bundle_id):
        observe_consumer_ingestion("evidence", "failure")
        raise HTTPException(
            status_code=400,
            detail="bundle_id must be 1-80 chars of [A-Za-z0-9_-.], no '..'",
        )

    dest_dir = REPO_ROOT / "campaigns" / f"consumer-{consumer_id}" / bundle_id
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = dest_dir / "bundle.json"
        bundle_path.write_text(json.dumps(req.bundle, indent=2))
    except OSError as exc:
        observe_consumer_ingestion("evidence", "failure")
        raise HTTPException(
            status_code=500, detail=f"Could not persist bundle: {exc}"
        ) from None

    observe_consumer_ingestion("evidence", "success")
    return {
        "status": "stored",
        "consumer_id": consumer_id,
        "bundle_id": bundle_id,
        "path": str(bundle_path),
    }
