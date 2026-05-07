"""Bearer token authentication middleware (RFC 6750).

Reads ``ORCHESTRATOR_API_TOKEN`` from the environment at startup. When unset
or empty, the middleware is a no-op so unauthenticated deployments and the
default test suite keep working. When set, every HTTP and WebSocket request
must carry ``Authorization: Bearer <token>`` except for paths in
``DEFAULT_PUBLIC_PATHS`` (liveness probes, OpenAPI metadata) and HTTP OPTIONS
preflight requests.

The middleware sits at the ASGI layer so it covers REST routes, the
``/mcp`` Starlette sub-app, and the ``/ws`` WebSocket uniformly without each
handler needing its own check.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

ENV_VAR = "ORCHESTRATOR_API_TOKEN"

DEFAULT_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/metrics",
        "/openapi.json",
        "/docs",
        "/redoc",
        "/docs/oauth2-redirect",
    }
)


def load_token_from_env() -> str | None:
    """Return the configured token, or ``None`` to disable auth."""
    raw = os.environ.get(ENV_VAR, "").strip()
    return raw or None


class BearerTokenAuthMiddleware:
    """ASGI middleware enforcing ``Authorization: Bearer <token>``."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str | None,
        public_paths: Iterable[str] = DEFAULT_PUBLIC_PATHS,
    ) -> None:
        self.app = app
        self._token = token
        self._public_paths: frozenset[str] = frozenset(public_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if self._token is None:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and scope.get("method", "").upper() == "OPTIONS":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in self._public_paths:
            await self.app(scope, receive, send)
            return

        presented = self._extract_token(scope)
        if presented is None or not secrets.compare_digest(presented, self._token):
            await self._reject(scope, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _extract_token(scope: Scope) -> str | None:
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                # latin-1 covers all 256 byte values — decode cannot raise.
                decoded = value.decode("latin-1")
                parts = decoded.split(None, 1)
                if len(parts) != 2:
                    return None
                if parts[0].lower() != "bearer":
                    return None
                token = parts[1].strip()
                return token or None
        return None

    async def _reject(self, scope: Scope, send: Send) -> None:
        if scope["type"] == "websocket":
            await send(
                {
                    "type": "websocket.close",
                    "code": 4401,
                    "reason": "Unauthorized",
                }
            )
            return
        response = JSONResponse(
            {"detail": "Unauthorized: missing or invalid bearer token"},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="ai-orchestrator"'},
        )
        await response(scope, _noop_receive, send)


async def _noop_receive() -> dict[str, object]:
    return {"type": "http.disconnect"}
