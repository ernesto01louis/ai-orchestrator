"""In-process tests using FastAPI's TestClient.

These are the targeted tests that anchor specific bug fixes in Phase 0.e.
They import app.py directly — slower than the HTTP smoke tests, but they
let us drive the WebSocket plumbing and inspect internal helpers.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

pytestmark = pytest.mark.inprocess


def test_repair_json_round_trip():
    """`repair_json` strips markdown fences and trailing commas, returns parseable JSON."""
    import app  # noqa: WPS433  (test-time import)

    raw = """```json
    {
        "name": "kazuki",
        "items": [1, 2, 3,],
        "meta": {"trailing": "comma",},
    }
    ```"""
    cleaned = app.repair_json(raw)
    parsed = json.loads(cleaned)
    assert parsed["name"] == "kazuki"
    assert parsed["items"] == [1, 2, 3]
    assert parsed["meta"]["trailing"] == "comma"


def test_ws_broadcast_from_background_thread():
    """Calling _ws_broadcast from a non-loop thread must deliver to clients.

    Fixed in Phase 0.e: _lifespan now captures the running loop, and
    _ws_broadcast uses asyncio.run_coroutine_threadsafe to post the
    coroutine onto that loop from any thread.
    """
    from fastapi.testclient import TestClient

    import app  # noqa: WPS433

    received: list[str] = []
    error_box: list[BaseException] = []

    with TestClient(app.app) as client:
        with client.websocket_connect("/ws") as ws:
            # Give the server a beat to register the client in _ws_clients.
            time.sleep(0.1)

            def broadcaster() -> None:
                try:
                    app._ws_broadcast({"event": "test", "payload": "hello"})
                except BaseException as exc:  # noqa: BLE001
                    error_box.append(exc)

            t = threading.Thread(target=broadcaster, daemon=True)
            t.start()
            t.join(timeout=3.0)

            # Starlette's WebSocketTestSession.receive_text() blocks until a
            # message is buffered; broadcaster has already completed (joined
            # above), so the message must be ready or the broadcast was lost.
            try:
                msg = ws.receive_text()
                received.append(msg)
            except Exception:  # noqa: BLE001
                pass

    assert not error_box, f"broadcaster raised: {error_box[0]!r}"
    assert received, "no message received from background-thread broadcast"
    body = json.loads(received[0])
    assert body.get("payload") == "hello"
