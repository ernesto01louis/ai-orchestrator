"""Phase 2.5 — SkyPilot cloud-burst wrapper.

Dormant by default. ``is_enabled()`` returns ``True`` only when
``sky.enabled=true`` in config.json AND the SkyPilot SDK is importable
AND the operator has at least one cloud provider configured (verified
lazily by ``sky.check`` at first call). Every public function is a
no-op or raises a structured ``SkyDisabledError`` when those
prerequisites aren't met — the orchestrator never attempts to talk
to a cloud provider until an operator explicitly opts in.

Failure modes mirror the Phase 2.1 / 2.2 / 2.3 dual-write semantics:
unexpected runtime errors are caught, logged with a structured WARN,
and surfaced to the caller as a typed exception. The state hooks and
HTTP routes that wrap us must turn that into a 503 / status update,
never propagate it through Prefect ``@task`` bodies.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger(__name__)

# Module-level cache. ``_sky_module`` is the lazily-imported ``sky``
# package; ``_check_passed`` records whether we've successfully run
# the one-time cloud-credential probe.
_sky_module: Any | None = None
_check_passed: bool = False
_init_lock = threading.RLock()


class SkyDisabledError(RuntimeError):
    """Raised when a SkyPilot operation is requested but the feature
    is dormant. Callers should translate this to a 503 in HTTP routes
    or a no-op in background daemons."""


@dataclass(frozen=True)
class BurstRequest:
    """Operator-facing burst description. Translated into a SkyPilot
    Resources / Task spec by ``launch_burst``."""

    spec_name: str  # YAML basename under SKY_YAML_DIR (without .yaml)
    cluster_name: str  # human-readable cluster id; usually run_id-prefixed
    accelerator: str | None = None  # override SKY_DEFAULT_ACCELERATOR
    cloud: str | None = None  # override SKY_DEFAULT_CLOUD
    env_overrides: dict[str, str] | None = None
    detach_run: bool = True  # default: launch + return; failsafe stops idle
    estimated_cost_usd: float = 0.0  # caller's pre-launch estimate


@dataclass(frozen=True)
class BurstHandle:
    """Returned by ``launch_burst``. Carries enough state for the
    idle-stop daemon and the routes layer to query / cancel."""

    cluster_name: str
    spec_name: str
    started_at: str  # ISO-8601 UTC
    estimated_cost_usd: float
    request: BurstRequest


def is_enabled() -> bool:
    """Return whether SkyPilot bursts are wired up.

    Three conditions must all be true:
    1. ``sky.enabled=true`` in config.json.
    2. The ``skypilot`` package is importable (operators who skipped
       the optional install get a clean disabled state, not a crash).
    3. The configured ``yaml_dir`` exists on disk so the burst route
       can resolve named specs.
    """
    from core import config  # noqa: PLC0415
    if not config.SKY_ENABLED:
        return False
    if _try_import_sky() is None:
        return False
    if not os.path.isdir(config.SKY_YAML_DIR):
        return False
    return True


def _try_import_sky() -> Any | None:
    """Lazy-import the ``sky`` package once.

    Cached so that ``is_enabled()`` is cheap to call from hot paths.
    Returns ``None`` (not raising) if the package is missing — that
    way operators who skip the optional cloud install still get a
    clean disabled state.
    """
    global _sky_module
    if _sky_module is not None:
        return _sky_module
    with _init_lock:
        if _sky_module is not None:
            return _sky_module
        try:
            import sky  # noqa: PLC0415
        except ImportError:
            return None
        except Exception as exc:  # pragma: no cover — defensive
            _logger.warning("sky_import_failed error=%s", exc)
            return None
        _sky_module = sky
        return sky


def _resolve_yaml_path(spec_name: str) -> str:
    """Map ``spec_name`` (basename like ``llm-burst``) to an on-disk
    YAML path under ``SKY_YAML_DIR``. Strips any ``.yaml`` suffix the
    caller may have left in. Raises ``FileNotFoundError`` if the spec
    doesn't exist — the route layer turns that into a 404."""
    from core import config  # noqa: PLC0415
    base = spec_name.removesuffix(".yaml")
    candidate = os.path.join(config.SKY_YAML_DIR, f"{base}.yaml")
    if not os.path.isfile(candidate):
        raise FileNotFoundError(
            f"SkyPilot spec not found: {candidate}. "
            f"Available specs live under {config.SKY_YAML_DIR}/."
        )
    return candidate


def launch_burst(req: BurstRequest) -> BurstHandle:
    """Provision a cloud GPU per ``req`` and return a handle.

    Raises ``SkyDisabledError`` when ``is_enabled()`` is ``False``.
    Raises ``FileNotFoundError`` when the named spec is missing.
    Raises ``ValueError`` when ``estimated_cost_usd`` exceeds
    ``SKY_MAX_BURST_COST_USD`` (the per-burst safety ceiling).

    Implementation note: this function intentionally does NOT call
    ``sky.launch`` directly — it constructs the ``Task`` and uses
    ``sky.stream_and_get(sky.launch(...))`` so the route can return
    quickly while SkyPilot provisions in the background. Cost
    accounting happens later via the SkyPilot CLI's ``sky cost-report``
    integrated through ``cost_report_for_cluster``.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    from core import config  # noqa: PLC0415

    if not is_enabled():
        raise SkyDisabledError(
            "SkyPilot is not enabled — set sky.enabled=true in config.json "
            "and configure provider credentials before requesting bursts."
        )
    if req.estimated_cost_usd > config.SKY_MAX_BURST_COST_USD:
        raise ValueError(
            f"Burst estimated cost ${req.estimated_cost_usd:.2f} exceeds "
            f"the configured ceiling of ${config.SKY_MAX_BURST_COST_USD:.2f}."
        )

    yaml_path = _resolve_yaml_path(req.spec_name)
    sky = _try_import_sky()
    assert sky is not None  # is_enabled() vouched

    # The SDK is loaded; build the Task + Resources from the YAML +
    # operator overrides. Done inside a helper so the test suite can
    # patch the construction without contacting any cloud.
    task = _build_task(sky, yaml_path, req)
    request_id = _submit_task(sky, task, req.cluster_name, req.detach_run)
    started_at = datetime.now(timezone.utc).isoformat()
    _logger.info(
        "sky_burst_launched cluster=%s spec=%s request_id=%s",
        req.cluster_name, req.spec_name, request_id,
    )
    return BurstHandle(
        cluster_name=req.cluster_name,
        spec_name=req.spec_name,
        started_at=started_at,
        estimated_cost_usd=req.estimated_cost_usd,
        request=req,
    )


def _build_task(sky_mod: Any, yaml_path: str, req: BurstRequest) -> Any:
    """Construct ``sky.Task`` from a YAML spec + operator overrides.

    Split out so tests can patch this point and assert on the
    resulting Task without ever calling sky.launch.
    """
    from core import config  # noqa: PLC0415

    task = sky_mod.Task.from_yaml(yaml_path)
    cloud = req.cloud or config.SKY_DEFAULT_CLOUD
    accelerator = req.accelerator or config.SKY_DEFAULT_ACCELERATOR
    resources = sky_mod.Resources(
        cloud=sky_mod.clouds.CLOUD_REGISTRY.from_str(cloud)
        if cloud else None,
        accelerators=accelerator,
    )
    task.set_resources({resources})
    if req.env_overrides:
        task.update_envs(req.env_overrides)
    return task


def _submit_task(
    sky_mod: Any, task: Any, cluster_name: str, detach_run: bool
) -> str:
    """Submit a ``sky.Task`` for provisioning.

    Wrapper exists so the test suite can mock the actual API call
    without faking the entire SDK surface.
    """
    request_id = sky_mod.launch(
        task,
        cluster_name=cluster_name,
        detach_run=detach_run,
        idle_minutes_to_autostop=None,  # set explicitly by failsafe
    )
    return str(request_id)


def stop_burst(cluster_name: str) -> None:
    """Stop a running burst (idempotent).

    Used by the manual stop route AND the idle-timeout daemon.
    Raises ``SkyDisabledError`` when the feature is dormant.
    """
    if not is_enabled():
        raise SkyDisabledError(
            "SkyPilot is not enabled — cannot stop a burst that "
            "doesn't exist on this orchestrator instance."
        )
    sky = _try_import_sky()
    assert sky is not None
    try:
        sky.stop(cluster_name)
        _logger.info("sky_burst_stopped cluster=%s", cluster_name)
    except Exception as exc:
        _logger.warning(
            "sky_stop_failed cluster=%s error=%s", cluster_name, exc,
        )
        raise


def status_burst(cluster_name: str) -> dict[str, Any]:
    """Return the SkyPilot cluster status for ``cluster_name``.

    Returns a dict with at minimum ``status``, ``cloud``, and
    ``last_use`` keys. Raises ``SkyDisabledError`` when dormant.
    """
    if not is_enabled():
        raise SkyDisabledError("SkyPilot is not enabled")
    sky = _try_import_sky()
    assert sky is not None
    try:
        rows = sky.status(cluster_names=[cluster_name], refresh=False)
    except Exception as exc:
        _logger.warning(
            "sky_status_failed cluster=%s error=%s", cluster_name, exc,
        )
        raise
    if not rows:
        return {"cluster_name": cluster_name, "status": "not_found"}
    row = rows[0] if isinstance(rows, list) else rows
    return {
        "cluster_name": cluster_name,
        "status": str(row.get("status", "unknown")),
        "cloud": str(row.get("cloud", "")),
        "last_use": str(row.get("last_use", "")),
    }


def list_active_bursts() -> list[dict[str, Any]]:
    """Return all clusters currently registered with SkyPilot.

    Empty list when dormant — callers (the idle-stop daemon, the UI)
    don't need to gate on ``is_enabled()`` themselves.
    """
    if not is_enabled():
        return []
    sky = _try_import_sky()
    assert sky is not None
    try:
        rows = sky.status(refresh=False)
    except Exception as exc:
        _logger.warning("sky_list_failed error=%s", exc)
        return []
    if not rows:
        return []
    return [
        {
            "cluster_name": str(r.get("name", "")),
            "status": str(r.get("status", "unknown")),
            "cloud": str(r.get("cloud", "")),
            "last_use": str(r.get("last_use", "")),
        }
        for r in (rows if isinstance(rows, list) else [rows])
    ]


def reset_for_tests() -> None:
    """Drop the cached SDK reference + check flag.

    Tests that monkeypatch ``config.SKY_ENABLED`` must call this so
    a previous test's state doesn't leak.
    """
    global _sky_module, _check_passed
    with _init_lock:
        _sky_module = None
        _check_passed = False
    BURSTS.clear()


# ---------------------------------------------------------------------------
# Active-burst registry — populated by the route, drained by the
# idle-stop daemon
# ---------------------------------------------------------------------------

# Keyed by ``cluster_name``. Values: dict with ``handle`` (BurstHandle),
# ``run_id`` (the orchestrator run that requested the burst), and
# ``status`` (``running`` / ``stopping`` / ``stopped``). Mutated under
# ``_burst_lock``.
BURSTS: dict[str, dict[str, Any]] = {}
_burst_lock = threading.Lock()


def register_burst(run_id: str, handle: BurstHandle) -> None:
    """Record a successful launch in the in-process registry.

    Called by the burst route after ``launch_burst`` returns. The
    idle-stop daemon (Phase 2.5.4) reads this registry to know which
    clusters to monitor.
    """
    with _burst_lock:
        BURSTS[handle.cluster_name] = {
            "handle": handle,
            "run_id": run_id,
            "status": "running",
        }
    _logger.info(
        "sky_burst_registered cluster=%s run_id=%s",
        handle.cluster_name, run_id,
    )


def unregister_burst(cluster_name: str) -> dict[str, Any] | None:
    """Remove a burst from the registry. Returns the entry or None.

    Called after a successful ``stop_burst`` so subsequent calls to
    ``list_registered_bursts`` don't include the dead cluster.
    """
    with _burst_lock:
        return BURSTS.pop(cluster_name, None)


def list_registered_bursts() -> list[dict[str, Any]]:
    """Snapshot of the active-burst registry. Used by the routes to
    return a list view; daemon iterates ``BURSTS`` directly."""
    with _burst_lock:
        return [
            {
                "cluster_name": name,
                "run_id": entry["run_id"],
                "status": entry["status"],
                "spec_name": entry["handle"].spec_name,
                "started_at": entry["handle"].started_at,
                "estimated_cost_usd": entry["handle"].estimated_cost_usd,
            }
            for name, entry in BURSTS.items()
        ]


def idle_stop_pass() -> list[str]:
    """One pass of the idle-stop failsafe.

    For every cluster in ``BURSTS``, query its status; if the cluster
    has been idle for ``SKY_IDLE_TIMEOUT_MINUTES``, stop it,
    deregister, and accrue actual cost to the parent campaign. Returns
    the list of cluster_names that were stopped this pass — caller
    (the daemon loop, tests) can use this for assertions / logging.

    Always safe to call: no-op when SkyPilot is dormant or the
    registry is empty. Per-cluster errors are swallowed so one
    misbehaving cluster doesn't stall the loop.
    """
    if not is_enabled():
        return []
    from datetime import datetime, timezone  # noqa: PLC0415

    from core import config  # noqa: PLC0415

    timeout_minutes = max(0, int(config.SKY_IDLE_TIMEOUT_MINUTES))
    if timeout_minutes == 0:
        # Operators who set 0 disable idle-stop entirely.
        return []

    stopped: list[str] = []
    with _burst_lock:
        candidates = list(BURSTS.keys())
    now = datetime.now(timezone.utc)

    for cluster_name in candidates:
        try:
            status = status_burst(cluster_name)
        except Exception as exc:
            _logger.warning(
                "sky_idle_stop_status_failed cluster=%s error=%s",
                cluster_name, exc,
            )
            continue
        if not _is_idle(status, now, timeout_minutes):
            continue
        try:
            actual_cost = cost_report_for_cluster(cluster_name)
            stop_burst(cluster_name)
        except Exception as exc:
            _logger.warning(
                "sky_idle_stop_failed cluster=%s error=%s",
                cluster_name, exc,
            )
            continue
        entry = unregister_burst(cluster_name)
        run_id = entry["run_id"] if entry else None
        if run_id:
            try:
                from core.budget import accrue_to_campaign  # noqa: PLC0415
                accrue_to_campaign(run_id, float(actual_cost))
            except Exception:  # pragma: no cover — defensive
                pass
        stopped.append(cluster_name)
        _logger.info(
            "sky_idle_stop_executed cluster=%s actual_cost_usd=%.4f",
            cluster_name, actual_cost,
        )

    return stopped


def _is_idle(
    status: dict[str, Any], now: Any, timeout_minutes: int
) -> bool:
    """Decide whether a cluster has been idle long enough to stop.

    Conservative: any parse failure on ``last_use`` returns False so
    a flaky timestamp doesn't trigger an unintended stop.
    """
    from datetime import datetime, timedelta  # noqa: PLC0415

    last_use_raw = status.get("last_use")
    if not last_use_raw:
        return False
    try:
        last_use = datetime.fromisoformat(str(last_use_raw))
    except (TypeError, ValueError):
        return False
    if last_use.tzinfo is None:
        last_use = last_use.replace(tzinfo=now.tzinfo)
    return (now - last_use) >= timedelta(minutes=timeout_minutes)


_idle_daemon_thread: threading.Thread | None = None
_idle_daemon_lock = threading.Lock()
_idle_daemon_stop = threading.Event()


def start_idle_stop_daemon(poll_interval_seconds: int = 60) -> None:
    """Start the daemon thread that runs ``idle_stop_pass`` periodically.

    Idempotent — calling twice is a no-op. No-op when SkyPilot is
    dormant; the daemon will simply re-evaluate ``is_enabled()`` on
    each pass and exit cleanly when the operator flips the flag back
    to false.
    """
    global _idle_daemon_thread
    with _idle_daemon_lock:
        if _idle_daemon_thread is not None and _idle_daemon_thread.is_alive():
            return
        _idle_daemon_stop.clear()

        def _loop() -> None:
            _logger.info(
                "sky_idle_daemon_started poll_interval=%ss",
                poll_interval_seconds,
            )
            while not _idle_daemon_stop.wait(poll_interval_seconds):
                try:
                    idle_stop_pass()
                except Exception as exc:  # pragma: no cover — defensive
                    _logger.warning("sky_idle_daemon_pass_failed error=%s", exc)

        thread = threading.Thread(
            target=_loop,
            name="sky-idle-stop-daemon",
            daemon=True,
        )
        _idle_daemon_thread = thread
        thread.start()


def stop_idle_stop_daemon() -> None:
    """Signal the daemon to exit at the next poll boundary.

    Used by tests + clean shutdown paths. Safe to call when the
    daemon was never started.
    """
    _idle_daemon_stop.set()


def cost_report_for_cluster(cluster_name: str) -> float:
    """Best-effort actual-cost lookup for a cluster.

    Returns the estimated cost when the SDK doesn't surface a
    real number (and when SkyPilot is dormant). Phase 2.5.4 calls
    this on cluster termination to accrue the real charge to
    Phase 2.4 ``budget_used_usd``. Never raises.
    """
    fallback = 0.0
    with _burst_lock:
        entry = BURSTS.get(cluster_name)
    if entry is not None:
        fallback = float(entry["handle"].estimated_cost_usd)

    sky = _try_import_sky()
    if sky is None:
        return fallback
    try:
        # SkyPilot exposes per-cluster cost via ``sky cost-report --cluster
        # <name>`` in the CLI. The Python API surface for this
        # changes between releases; gate on attribute existence so
        # we degrade gracefully on older SDK versions.
        report_fn = getattr(sky, "cost_report", None)
        if report_fn is None:
            return fallback
        rows = report_fn([cluster_name])
        if not rows:
            return fallback
        row = rows[0] if isinstance(rows, list) else rows
        return float(row.get("total_cost", fallback) or fallback)
    except Exception as exc:
        _logger.warning(
            "sky_cost_report_failed cluster=%s error=%s",
            cluster_name, exc,
        )
        return fallback
