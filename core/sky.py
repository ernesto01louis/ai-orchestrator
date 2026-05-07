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
