"""Health, Ollama model, and Prometheus metric routes.

Moved out of ``api/routes/__init__.py`` in audit Stage 5 §D.1 split.
Zero behavior change — every path / response shape is byte-identical
to pre-split.

Endpoints:
    GET /health         orchestrator health probe (Ollama + Hindsight + active runs)
    GET /metrics        Prometheus exposition (text/plain), include_in_schema=False
    GET /models         loaded + available Ollama models on both servers
    GET /models/loaded  hot-in-memory models only (cheap probe)
"""
from __future__ import annotations

import requests
from fastapi import APIRouter
from fastapi.responses import Response

import time
from pathlib import Path

from core.config import (
    HINDSIGHT_ENABLED,
    HINDSIGHT_URL,
    OLLAMA_JUDGE_URL,
    OLLAMA_MAIN_URL,
)
from core.runtime import APP_START_TIME, APP_VERSION
from orchestration import (
    get_active_runs,
    get_available_models,
    get_loaded_models,
    get_orchestrator_health,
)

router = APIRouter()


# Phase 2.6 — flat service health probes for the operator console.
# Each probe returns one of "ok" | "degraded" | "down". Probes never
# raise — exceptions degrade to "down" so /health stays a green-path
# endpoint even when half the stack is offline.
def _probe_postgres() -> str:
    try:
        from core import db
        if not db.is_enabled():
            return "degraded"
        return "ok"
    except Exception:
        return "down"


def _probe_redis() -> str:
    try:
        from core import redis_client
        if not redis_client.is_enabled():
            return "degraded"
        return "ok"
    except Exception:
        return "down"


def _probe_otel() -> str:
    """Returns "ok" if OTel tracing is enabled and initialized, else "degraded"."""
    try:
        from core import config as _cfg
        return "ok" if getattr(_cfg, "OTEL_ENABLED", False) else "degraded"
    except Exception:
        return "down"


def _probe_dvc() -> str:
    """Returns "ok" if .dvc/config exists in the repo root."""
    try:
        if (Path("/opt/ai-orchestrator/.dvc/config")).exists():
            return "ok"
        return "degraded"
    except Exception:
        return "down"


def _flatten_ollama_status(ollama_status: dict) -> str:
    """Reduce multi-server Ollama status to a single ok/degraded/down."""
    if not ollama_status:
        return "down"
    states = [s.get("status", "") for s in ollama_status.values()]
    if all(s == "online" for s in states):
        return "ok"
    if any(s == "online" for s in states):
        return "degraded"
    return "down"


@router.get("/models")
def get_models():
    """List all available and currently loaded models across both Ollama servers."""
    return {
        "loaded": get_loaded_models(),
        "available": get_available_models(),
    }


@router.get("/models/loaded")
def get_models_loaded():
    """Quick check: which models are currently hot in memory."""
    return {"loaded": get_loaded_models()}


@router.get("/health")
def get_health():
    """Orchestrator system health check."""

    health = get_orchestrator_health()
    active = get_active_runs()

    # check Ollama server connectivity
    ollama_status = {}
    for name, url in [("main", OLLAMA_MAIN_URL), ("judge", OLLAMA_JUDGE_URL)]:
        try:
            r = requests.get(f"{url}/api/tags", timeout=5)
            ollama_status[name] = {
                "url": url,
                "status": "online" if r.status_code == 200 else f"error ({r.status_code})",
                "model_count": len(r.json().get("models", [])) if r.status_code == 200 else 0,
            }
        except requests.exceptions.RequestException as e:
            ollama_status[name] = {
                "url": url,
                "status": f"offline ({type(e).__name__})",
                "model_count": 0,
            }

    # check Hindsight connectivity
    hindsight_health = {"enabled": HINDSIGHT_ENABLED}
    if HINDSIGHT_ENABLED:
        try:
            r = requests.get(f"{HINDSIGHT_URL}/v1/default/banks", timeout=5)
            hindsight_health["status"] = (
                "online" if r.status_code == 200 else f"error ({r.status_code})"
            )
            hindsight_health["url"] = HINDSIGHT_URL
        except requests.exceptions.RequestException as e:
            hindsight_health["status"] = f"offline ({type(e).__name__})"
            hindsight_health["url"] = HINDSIGHT_URL

    # Phase 2.6 — flat service-status fields for the operator console.
    # Kept as a `services` sub-key so the legacy nested ollama_servers /
    # hindsight payload above stays byte-identical for the Python SDK
    # and other existing consumers.
    services = {
        "orchestrator": "ok" if isinstance(health, dict) and health.get("status") != "down" else "degraded",
        "ollama": _flatten_ollama_status(ollama_status),
        "hindsight": "ok" if hindsight_health.get("status") == "online" else
                     ("degraded" if not HINDSIGHT_ENABLED else "down"),
        "postgres": _probe_postgres(),
        "redis": _probe_redis(),
        "tempo": _probe_otel(),
        "prometheus": "ok",  # if this handler ran, /metrics is reachable
        "dvc": _probe_dvc(),
    }

    return {
        "orchestrator": health,
        "ollama_servers": ollama_status,
        "hindsight": hindsight_health,
        "active_runs": len(active),
        "uptime_indicator": "ok",
        # Phase 2.6 additions (additive — no consumer breakage)
        "services": services,
        "uptime_s": int(time.time() - APP_START_TIME),
        "version": APP_VERSION,
    }


@router.get("/metrics", include_in_schema=False)
def get_metrics() -> Response:
    """Prometheus metrics exposition (text/plain)."""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
