"""Phase 2.3 — OpenTelemetry tracing setup.

Dormant by default. ``init_tracing(app)`` is a no-op when
``otel.enabled=false`` or the OTLP endpoint is empty.

When enabled, ``init_tracing`` installs the global ``TracerProvider``
with a ``BatchSpanProcessor`` exporting via OTLP/gRPC to the configured
endpoint, and auto-instruments FastAPI (so every HTTP request gets a
span automatically) plus the ``requests`` library (so outbound HTTP
calls — Ollama, NoteDiscovery, ntfy, Gotify — get child spans).

When disabled, ``get_tracer()`` still returns a tracer — it just
delegates to OTel's no-op default ``TracerProvider``, so manual
``with tracer.start_as_current_span(...)`` blocks elsewhere in the
codebase are zero-cost when tracing isn't initialised.

Failure-tolerant: any error during init logs a structured WARN and
returns ``False``. The orchestrator boots even if Tempo is
unreachable. Mirrors the Phase 2.1 / 2.2 dual-write semantics.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

# Module-level singleton state. ``_initialised`` flips to ``True`` after
# a successful first init; subsequent calls are idempotent no-ops.
_initialised = False
_init_lock = threading.Lock()


def is_enabled() -> bool:
    """Return whether OTel tracing is configured.

    True only when both ``otel.enabled=true`` in config.json AND a
    non-empty endpoint is resolvable (env override ``OTEL_ENDPOINT``
    wins — mirrors POSTGRES_DSN / REDIS_URL precedence).
    """
    from core import config  # noqa: PLC0415
    return bool(config.OTEL_ENABLED and config.OTEL_ENDPOINT)


def init_tracing(app: Any | None = None) -> bool:
    """Initialise OTel TracerProvider + auto-instrument FastAPI / requests.

    Idempotent. Returns ``True`` iff tracing is now active. Returns
    ``False`` immediately when disabled or already initialised. Any
    exception during setup is logged and swallowed.

    Pass the FastAPI ``app`` to also wire request-level auto-spans.
    Calling without ``app`` still configures the provider for manual
    spans elsewhere in the codebase.
    """
    global _initialised
    if _initialised:
        return True
    if not is_enabled():
        return False

    with _init_lock:
        if _initialised:  # double-checked
            return True
        try:
            from opentelemetry import trace  # noqa: PLC0415
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
                OTLPSpanExporter,
            )
            from opentelemetry.instrumentation.fastapi import (  # noqa: PLC0415
                FastAPIInstrumentor,
            )
            from opentelemetry.instrumentation.requests import (  # noqa: PLC0415
                RequestsInstrumentor,
            )
            from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
            from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
            from opentelemetry.sdk.trace.export import (  # noqa: PLC0415
                BatchSpanProcessor,
            )
            from opentelemetry.sdk.trace.sampling import (  # noqa: PLC0415
                TraceIdRatioBased,
            )

            from core import config  # noqa: PLC0415

            resource = Resource.create({
                "service.name": config.OTEL_SERVICE_NAME,
                "service.version": "0.2.3",
                "deployment.environment": "homelab",
            })
            sampler = TraceIdRatioBased(config.OTEL_SAMPLE_RATIO)
            provider = TracerProvider(resource=resource, sampler=sampler)

            # OTLP/gRPC over plaintext — Tempo on the LAN doesn't need TLS.
            exporter = OTLPSpanExporter(
                endpoint=config.OTEL_ENDPOINT, insecure=True
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)

            # Auto-instrument the requests library globally so outbound
            # HTTP calls (Ollama, ntfy, Gotify, NoteDiscovery) get
            # child spans without per-callsite changes.
            RequestsInstrumentor().instrument()

            # Auto-instrument FastAPI on the app instance — adds a span
            # per HTTP request and propagates traceparent across the
            # ASGI middleware chain.
            if app is not None:
                FastAPIInstrumentor.instrument_app(app)

            _initialised = True
            log.info(
                "opentelemetry_initialised endpoint=%s service=%s sample_ratio=%s",
                config.OTEL_ENDPOINT,
                config.OTEL_SERVICE_NAME,
                config.OTEL_SAMPLE_RATIO,
            )
            return True
        except Exception as exc:
            log.warning("opentelemetry_init_failed error=%s", exc)
            return False


def get_tracer(name: str = "ai-orchestrator") -> Any:
    """Return a tracer for the given name.

    Safe to call before ``init_tracing()`` — OTel returns a no-op
    tracer when no provider is registered, so manual spans elsewhere
    in the codebase don't need to gate on ``is_enabled()``.
    """
    from opentelemetry import trace  # noqa: PLC0415
    return trace.get_tracer(name)


def reset_for_tests() -> None:
    """Drop the cached initialisation flag so tests can re-init.

    Does NOT reset OTel's global ``TracerProvider`` (the SDK doesn't
    expose a clean way), so tests that want a fresh provider should
    monkeypatch ``trace.get_tracer_provider`` themselves.
    """
    global _initialised
    with _init_lock:
        _initialised = False
