"""Runtime state — RUN_STATUS dict, WebSocket broadcast, log writer.

These three concerns are tightly coupled (log writes status, status writes
broadcast, broadcast pushes to WS clients) so they live in one file. Any
thread can call into here safely:

  * `_ws_broadcast` posts coroutines onto the captured main loop via
    `asyncio.run_coroutine_threadsafe`.
  * `_update_run_status` and `_init_run_status` use a threading.Lock to
    serialize mutations.
  * `log` writes via flock on the per-run log file.

The main loop is captured by `_lifespan` at startup via `set_main_loop`.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import threading
from datetime import datetime
from pathlib import Path

from .paths import LOG_DIR, RUN_INDEX_FILE

# ── live process state ─────────────────────────────
RUN_STATUS: dict = {}
_run_status_lock = threading.Lock()
ORCHESTRATOR_PAUSED = False

# Per-campaign live flags (Phase 1.1). Keyed by campaign_id; entries:
#   {"phase": "queued"|"running"|"paused"|"completed"|"aborted"|"failed",
#    "paused": bool, "aborted": bool, "current_run_id": str | None,
#    "manifest_status": "ok"|"corrupted"|"missing"|"skipped"|None}
#      - "ok" / "skipped" set by run_campaign hook at end of campaign  (Phase C)
#      - "corrupted" / "missing" set by /campaigns/{id}/verify-merkle  (Phase D)
CAMPAIGN_STATUS: dict = {}
_campaign_status_lock = threading.Lock()

_ws_clients: list = []
_ws_lock = threading.Lock()
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Captured by `_lifespan` at startup so background threads can post
    coroutines back onto the FastAPI event loop via run_coroutine_threadsafe.
    """
    global _MAIN_LOOP
    _MAIN_LOOP = loop


# ── WebSocket broadcast ────────────────────────────
def _ws_broadcast(msg: dict) -> None:
    """Send a JSON message to all connected WebSocket clients.

    Safe to call from any thread: the actual `ws.send_text` coroutine is
    scheduled on the captured main loop via asyncio.run_coroutine_threadsafe.
    Each send is bounded by a 2 s timeout; clients that error are evicted.
    """
    if _MAIN_LOOP is None:
        # startup hasn't completed; no clients are subscribed yet anyway
        return
    with _ws_lock:
        clients = list(_ws_clients)
    if not clients:
        return
    payload = json.dumps(msg)
    dead: list = []
    for ws in clients:
        try:
            fut = asyncio.run_coroutine_threadsafe(ws.send_text(payload), _MAIN_LOOP)
            fut.result(timeout=2.0)
        except Exception:
            dead.append(ws)
    if dead:
        with _ws_lock:
            for ws in dead:
                try:
                    _ws_clients.remove(ws)
                except ValueError:
                    pass


# ── run status helpers ─────────────────────────────
def _load_run_index() -> dict:
    """Load the persistent run index from disk."""
    try:
        with open(RUN_INDEX_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _persist_run_index(run_id: str, snapshot: dict) -> None:
    """Save a completed run's metadata to the persistent run index.

    Phase 2.1: after the JSON write succeeds, mirror the row into
    Postgres via core.db_writethrough. JSON stays canonical — a Postgres
    write failure is logged + swallowed.
    """
    try:
        index = _load_run_index()
        index[run_id] = {
            "phase": snapshot.get("phase", "completed"),
            "score": snapshot.get("score", 0),
            "completed": True,
            "project": snapshot.get("project", ""),
            "target": snapshot.get("target", ""),
            "has_error": snapshot.get("error") is not None,
            "error_msg": str(snapshot["error"])[:200] if snapshot.get("error") else None,
            "timestamp": datetime.utcnow().isoformat(),
        }
        with open(RUN_INDEX_FILE, "w") as f:
            json.dump(index, f, indent=2)
    except OSError:
        return
    # Lazy import — core.runtime is loaded early in app boot; we don't
    # want to drag SQLAlchemy in unless someone actually persists a run.
    try:
        from core import db_writethrough
        db_writethrough.mirror_run_completion(run_id, snapshot)
    except Exception:
        # mirror_run_completion already swallows; this catch is just
        # belt-and-braces for an import-time error from db_writethrough.
        pass


def _update_run_status(run_id: str, **kwargs) -> None:
    """Thread-safe update of RUN_STATUS fields, broadcasts on change, persists on completion."""
    with _run_status_lock:
        if run_id not in RUN_STATUS:
            RUN_STATUS[run_id] = {
                "phase": "", "score": 0, "completed": False,
                "result": None, "error": None,
            }
        RUN_STATUS[run_id].update(kwargs)
        snapshot = dict(RUN_STATUS[run_id])
    if any(k in kwargs for k in ("phase", "score", "completed", "error")):
        _ws_broadcast({
            "type": "status", "run_id": run_id,
            "phase": snapshot.get("phase", ""),
            "score": snapshot.get("score", 0),
            "completed": snapshot.get("completed", False),
            "error": snapshot.get("error"),
            "project": snapshot.get("project", ""),
            "target": snapshot.get("target", ""),
        })
    if kwargs.get("completed"):
        _persist_run_index(run_id, snapshot)


def _init_run_status(run_id: str, **kwargs) -> None:
    """Thread-safe initialization of a new run entry.

    Fields:
      phase                  str   — current lifecycle phase / last log message
      score                  float — best score achieved (0–10)
      completed              bool  — True once the run reaches a terminal state
      result                 dict | None — final result payload on success
      error                  str | None  — error message on failure
      _judge_primary_down    bool  — True if the primary judge LLM was unreachable
      manifest_status        Literal["ok","corrupted","missing","skipped"] | None
                             — "ok": manifest.json written and hashes verified       [Phase B: orchestration hook]
                             — "skipped": write attempted but failed (non-fatal)     [Phase B: orchestration hook]
                             — "corrupted": manifest written but hashes don't match  [Phase D: /runs/{id}/verify endpoint]
                             — "missing": manifest.json absent after run             [Phase D: /runs/{id}/verify endpoint]
                             — None: not yet attempted (run still in progress)
    """
    with _run_status_lock:
        RUN_STATUS[run_id] = {
            "phase": "queued", "score": 0, "completed": False,
            "result": None, "error": None, "_judge_primary_down": False,
            "manifest_status": None,
            **kwargs,
        }
    try:
        from core.metrics import observe_run_started
        observe_run_started()
    except Exception:
        pass


# ── log writer ─────────────────────────────────────
def log(run_id: str, message: str) -> None:
    """Per-run log writer with file lock + WS broadcast + status update."""
    ts = datetime.utcnow().strftime("%H:%M:%S")
    line = f"[{ts}] [{run_id}] {message}"
    print(line)

    log_path = Path(LOG_DIR) / f"{run_id}.log"
    try:
        with open(log_path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(line + "\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as e:
        print(f"WARNING: could not write to log file: {e}")

    _update_run_status(run_id, phase=message)
    _ws_broadcast({"type": "log", "run_id": run_id, "line": line, "phase": message})
