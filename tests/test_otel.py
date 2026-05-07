"""Tests for core.otel — OpenTelemetry tracing init (Phase 2.3).

Two layers (mirrors tests/test_db.py / tests/test_redis_client.py):

* **Mocked / disabled** (default suite) — verifies that:
    - is_enabled() reflects config flags
    - init_tracing() is a no-op when disabled
    - init_tracing() builds the provider when enabled (mocked exporter)
    - init_tracing() is idempotent
    - init_tracing() swallows errors and returns False
    - get_tracer() always works (returns a real or no-op tracer)

OTel doesn't expose a clean way to *uninstall* a global TracerProvider,
so the "real" test layer in this file lives at the integration level
once Tempo is up (covered separately in Phase 2.3.3 smoke tests, not
gated by a pytest marker).
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from core import config, otel


@pytest.fixture(autouse=True)
def _reset_otel() -> Iterator[None]:
    """Each test starts with the init flag cleared."""
    otel.reset_for_tests()
    yield
    otel.reset_for_tests()


@pytest.fixture
def disabled_otel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "OTEL_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "OTEL_ENDPOINT", "", raising=False)


@pytest.fixture
def enabled_otel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "OTEL_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "OTEL_ENDPOINT", "tempo.fake:4317", raising=False)
    monkeypatch.setattr(config, "OTEL_SERVICE_NAME", "test-orchestrator", raising=False)
    monkeypatch.setattr(config, "OTEL_SAMPLE_RATIO", 1.0, raising=False)


# ---------------------------------------------------------------------------
# is_enabled()
# ---------------------------------------------------------------------------


def test_is_enabled_false_by_default(disabled_otel: None) -> None:
    assert otel.is_enabled() is False


def test_is_enabled_false_when_endpoint_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "OTEL_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "OTEL_ENDPOINT", "", raising=False)
    assert otel.is_enabled() is False


def test_is_enabled_true_when_both_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "OTEL_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "OTEL_ENDPOINT", "tempo:4317", raising=False)
    assert otel.is_enabled() is True


# ---------------------------------------------------------------------------
# init_tracing()
# ---------------------------------------------------------------------------


def test_init_tracing_returns_false_when_disabled(disabled_otel: None) -> None:
    assert otel.init_tracing() is False


def test_init_tracing_succeeds_with_mocked_exporter(
    enabled_otel: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch the OTLP exporter constructor + the FastAPI instrumentor
    + the requests instrumentor so init doesn't open a real socket."""
    fake_exporter = MagicMock(name="FakeOTLPExporter")
    fake_exporter_cls = MagicMock(return_value=fake_exporter)
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter",
        fake_exporter_cls,
    )
    fake_requests_inst = MagicMock(name="FakeRequestsInstrumentor")
    monkeypatch.setattr(
        "opentelemetry.instrumentation.requests.RequestsInstrumentor",
        MagicMock(return_value=fake_requests_inst),
    )
    fastapi_inst_cls = MagicMock(name="FakeFastAPIInstrumentor")
    monkeypatch.setattr(
        "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor",
        fastapi_inst_cls,
    )

    fake_app = MagicMock(name="FakeFastAPIApp")
    assert otel.init_tracing(fake_app) is True

    # Exporter was built with the configured endpoint + insecure=True
    fake_exporter_cls.assert_called_with(endpoint="tempo.fake:4317", insecure=True)
    # FastAPI was instrumented with the app
    fastapi_inst_cls.instrument_app.assert_called_with(fake_app)
    # Requests instrumentor was activated
    fake_requests_inst.instrument.assert_called_once()


def test_init_tracing_is_idempotent(
    enabled_otel: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second call must short-circuit and not rebuild the provider."""
    builds = 0

    def factory(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal builds
        builds += 1
        return MagicMock()

    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter",
        factory,
    )
    monkeypatch.setattr(
        "opentelemetry.instrumentation.requests.RequestsInstrumentor",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor",
        MagicMock(),
    )
    assert otel.init_tracing() is True
    assert otel.init_tracing() is True
    assert builds == 1


def test_init_tracing_swallows_exporter_error(
    enabled_otel: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed exporter init must log + return False, not raise."""

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("exporter exploded")

    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter",
        boom,
    )
    assert otel.init_tracing() is False


def test_init_tracing_works_without_app(
    enabled_otel: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual span flows should still work when init is called pre-FastAPI."""
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "opentelemetry.instrumentation.requests.RequestsInstrumentor",
        MagicMock(return_value=MagicMock()),
    )
    fastapi_inst_cls = MagicMock()
    monkeypatch.setattr(
        "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor",
        fastapi_inst_cls,
    )

    assert otel.init_tracing(None) is True
    fastapi_inst_cls.instrument_app.assert_not_called()


# ---------------------------------------------------------------------------
# get_tracer()
# ---------------------------------------------------------------------------


def test_get_tracer_returns_a_tracer_pre_init(disabled_otel: None) -> None:
    """Even without init, get_tracer must return a usable object — the
    SDK falls back to the no-op default. Manual spans elsewhere should
    not crash on cold-start."""
    tracer = otel.get_tracer("anything")
    assert tracer is not None
    # No-op tracer accepts the start_as_current_span context manager.
    with tracer.start_as_current_span("noop-span"):
        pass
