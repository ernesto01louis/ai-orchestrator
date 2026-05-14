"""WebSocket /ws live log + status stream (carved from api/routes/__init__.py).

Audit Stage 5 §D.1 split. The package-level ``router`` aggregator in
``api/routes/__init__.py`` mounts this sub-module via
``include_router``. Public URL paths and response shapes are
byte-identical to pre-split.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.runtime import _ws_clients, _ws_lock

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    Live streaming endpoint. Clients receive JSON messages:
      {"type": "log",    "run_id": "...", "line": "...", "phase": "..."}
      {"type": "status", "run_id": "...", "phase": "...", "score": N, ...}
    """
    await ws.accept()
    with _ws_lock:
        _ws_clients.append(ws)
    try:
        while True:
            # keep connection alive; client can send pings or filter commands
            await ws.receive_text()
            # echo back as heartbeat acknowledgement
            await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    finally:
        with _ws_lock:
            try:
                _ws_clients.remove(ws)
            except ValueError:
                pass
