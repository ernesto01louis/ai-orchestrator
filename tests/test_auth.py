"""Tests for core.auth.BearerTokenAuthMiddleware (Phase 1.7)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket

from core.auth import (
    DEFAULT_PUBLIC_PATHS,
    ENV_VAR,
    BearerTokenAuthMiddleware,
    load_token_from_env,
)


def _build_app(*, token: str | None) -> Starlette:
    async def root(_request):
        return JSONResponse({"ok": True})

    async def health(_request):
        return PlainTextResponse("ok")

    async def echo_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    routes = [
        Route("/", root, methods=["GET", "OPTIONS"]),
        Route("/health", health, methods=["GET"]),
        WebSocketRoute("/ws", echo_ws),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(BearerTokenAuthMiddleware, token=token)
    return app


# ── load_token_from_env ────────────────────────────────────────────────


def test_load_token_from_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert load_token_from_env() is None


def test_load_token_from_env_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "   ")
    assert load_token_from_env() is None


def test_load_token_from_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "  s3kret  ")
    assert load_token_from_env() == "s3kret"


# ── disabled middleware (token=None) ───────────────────────────────────


def test_disabled_no_authorization_required() -> None:
    client = TestClient(_build_app(token=None))
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200


def test_disabled_websocket_open() -> None:
    client = TestClient(_build_app(token=None))
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_text() == "hello"


# ── enabled middleware (token=set) ─────────────────────────────────────


@pytest.fixture()
def auth_client() -> Iterator[TestClient]:
    app = _build_app(token="s3kret")
    with TestClient(app) as client:
        yield client


def test_enabled_rejects_missing_header(auth_client: TestClient) -> None:
    resp = auth_client.get("/")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith("Bearer ")
    assert "Unauthorized" in resp.json()["detail"]


def test_enabled_rejects_wrong_token(auth_client: TestClient) -> None:
    resp = auth_client.get("/", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_enabled_rejects_wrong_scheme(auth_client: TestClient) -> None:
    resp = auth_client.get("/", headers={"Authorization": "Basic s3kret"})
    assert resp.status_code == 401


def test_enabled_rejects_empty_token(auth_client: TestClient) -> None:
    resp = auth_client.get("/", headers={"Authorization": "Bearer "})
    assert resp.status_code == 401


def test_enabled_accepts_valid_token(auth_client: TestClient) -> None:
    resp = auth_client.get("/", headers={"Authorization": "Bearer s3kret"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_enabled_accepts_case_insensitive_scheme(auth_client: TestClient) -> None:
    resp = auth_client.get("/", headers={"Authorization": "bearer s3kret"})
    assert resp.status_code == 200


def test_enabled_health_bypasses_auth(auth_client: TestClient) -> None:
    resp = auth_client.get("/health")
    assert resp.status_code == 200


def test_enabled_options_bypasses_auth(auth_client: TestClient) -> None:
    # OPTIONS preflight must succeed without Authorization for browser CORS.
    resp = auth_client.options("/")
    # Starlette returns 200 for OPTIONS on a route that lists OPTIONS,
    # 405 otherwise — either is fine; what we assert is "not 401".
    assert resp.status_code != 401


def test_enabled_websocket_rejects_without_token() -> None:
    client = TestClient(_build_app(token="s3kret"))
    with pytest.raises(Exception):  # WebSocketDisconnect or similar
        with client.websocket_connect("/ws"):
            pass


def test_enabled_websocket_accepts_with_token() -> None:
    client = TestClient(_build_app(token="s3kret"))
    with client.websocket_connect(
        "/ws", headers={"Authorization": "Bearer s3kret"}
    ) as ws:
        assert ws.receive_text() == "hello"


def test_default_public_paths_contains_expected() -> None:
    assert "/health" in DEFAULT_PUBLIC_PATHS
    assert "/openapi.json" in DEFAULT_PUBLIC_PATHS


# ── real app integration: auth disabled by default in tests ───────────


def test_real_app_health_reachable_without_auth(
    inprocess_client: TestClient,
) -> None:
    """Wire-up smoke: with no ORCHESTRATOR_API_TOKEN set at app startup,
    /health remains reachable on the real orchestrator app.

    The middleware is configured once at app.py import time via
    load_token_from_env() — this test asserts the default behavior, not
    runtime env reactivity. To test the token-set path, instantiate the
    middleware directly (see other tests in this file).

    Uses the session-scoped ``inprocess_client`` fixture from conftest so we
    don't create a second TestClient over the same MCP singleton (which raises
    RuntimeError on the second lifespan start).
    """
    resp = inprocess_client.get("/health")
    assert resp.status_code == 200
