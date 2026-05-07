"""Tests for core.sky — SkyPilot cloud-burst wrapper (Phase 2.5).

The SDK is stubbed so tests run without ever contacting a real cloud.
``is_enabled()`` is gated by config + the importable SDK + the
yaml_dir existing on disk; we verify all three branches.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from core import config, sky


@pytest.fixture(autouse=True)
def _reset_sky() -> Iterator[None]:
    sky.reset_for_tests()
    sky.stop_idle_stop_daemon()
    sky._idle_daemon_thread = None
    yield
    sky.stop_idle_stop_daemon()
    sky._idle_daemon_thread = None
    sky.reset_for_tests()


@pytest.fixture
def disabled_sky(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SKY_ENABLED", False, raising=False)


@pytest.fixture
def enabled_sky(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[MagicMock]:
    """Sky reports enabled with a valid yaml_dir + a fake SDK module."""
    yaml_dir = tmp_path / "sky"
    yaml_dir.mkdir()
    (yaml_dir / "llm-burst.yaml").write_text("name: llm\nresources:\n  cpus: 2\n")

    monkeypatch.setattr(config, "SKY_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "SKY_YAML_DIR", str(yaml_dir), raising=False)
    monkeypatch.setattr(config, "SKY_DEFAULT_CLOUD", "runpod", raising=False)
    monkeypatch.setattr(config, "SKY_DEFAULT_ACCELERATOR", "A10:1", raising=False)
    monkeypatch.setattr(config, "SKY_MAX_BURST_COST_USD", 5.0, raising=False)

    fake_sdk = MagicMock(name="FakeSkyModule")
    fake_sdk.launch.return_value = "request-abc"
    fake_sdk.stop.return_value = None
    fake_sdk.status.return_value = []
    monkeypatch.setattr(sky, "_sky_module", fake_sdk, raising=False)
    yield fake_sdk


# ---------------------------------------------------------------------------
# is_enabled()
# ---------------------------------------------------------------------------


def test_is_enabled_false_by_default(disabled_sky: None) -> None:
    assert sky.is_enabled() is False


def test_is_enabled_false_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """If skypilot isn't installed (operator skipped the optional
    install), is_enabled() must say False even when the config flag
    is on."""
    yaml_dir = tmp_path / "sky"
    yaml_dir.mkdir()
    monkeypatch.setattr(config, "SKY_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "SKY_YAML_DIR", str(yaml_dir), raising=False)
    monkeypatch.setattr(sky, "_try_import_sky", lambda: None)
    assert sky.is_enabled() is False


def test_is_enabled_false_when_yaml_dir_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "SKY_ENABLED", True, raising=False)
    monkeypatch.setattr(
        config, "SKY_YAML_DIR", "/nonexistent/path/skydir", raising=False,
    )
    monkeypatch.setattr(sky, "_sky_module", MagicMock(), raising=False)
    assert sky.is_enabled() is False


def test_is_enabled_true_when_all_conditions_met(enabled_sky: MagicMock) -> None:
    assert sky.is_enabled() is True


# ---------------------------------------------------------------------------
# launch_burst
# ---------------------------------------------------------------------------


def test_launch_burst_raises_when_disabled(disabled_sky: None) -> None:
    req = sky.BurstRequest(spec_name="llm-burst", cluster_name="r-test")
    with pytest.raises(sky.SkyDisabledError):
        sky.launch_burst(req)


def test_launch_burst_raises_when_spec_missing(enabled_sky: MagicMock) -> None:
    req = sky.BurstRequest(spec_name="does-not-exist", cluster_name="r-test")
    with pytest.raises(FileNotFoundError):
        sky.launch_burst(req)


def test_launch_burst_rejects_over_budget(enabled_sky: MagicMock) -> None:
    """A burst whose estimated cost exceeds the per-burst ceiling is
    rejected at launch time — never contacts the SDK."""
    req = sky.BurstRequest(
        spec_name="llm-burst",
        cluster_name="r-test",
        estimated_cost_usd=99.0,
    )
    with pytest.raises(ValueError, match="exceeds"):
        sky.launch_burst(req)
    enabled_sky.launch.assert_not_called()


def test_launch_burst_calls_sdk_with_overrides(
    enabled_sky: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal launch builds Task + Resources + submits."""
    captured: dict = {}

    def _capture_build(_sdk, yaml_path, req):
        captured["yaml_path"] = yaml_path
        captured["req"] = req
        return MagicMock(name="FakeTask")

    def _capture_submit(_sdk, _task, cluster_name, detach_run):
        captured["cluster_name"] = cluster_name
        captured["detach_run"] = detach_run
        return "request-xyz"

    monkeypatch.setattr(sky, "_build_task", _capture_build)
    monkeypatch.setattr(sky, "_submit_task", _capture_submit)

    req = sky.BurstRequest(
        spec_name="llm-burst",
        cluster_name="run-42-burst",
        accelerator="A100:1",
        cloud="runpod",
        estimated_cost_usd=1.25,
    )
    handle = sky.launch_burst(req)

    assert isinstance(handle, sky.BurstHandle)
    assert handle.cluster_name == "run-42-burst"
    assert handle.spec_name == "llm-burst"
    assert handle.estimated_cost_usd == 1.25
    assert captured["yaml_path"].endswith("llm-burst.yaml")
    assert captured["cluster_name"] == "run-42-burst"


def test_launch_burst_strips_yaml_suffix(
    enabled_sky: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``spec_name='llm-burst.yaml'`` is accepted as a courtesy."""
    monkeypatch.setattr(sky, "_build_task", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(sky, "_submit_task", lambda *_a, **_k: "req")
    req = sky.BurstRequest(spec_name="llm-burst.yaml", cluster_name="x")
    handle = sky.launch_burst(req)
    assert handle.spec_name == "llm-burst.yaml"


# ---------------------------------------------------------------------------
# stop_burst / status_burst / list_active_bursts
# ---------------------------------------------------------------------------


def test_stop_burst_raises_when_disabled(disabled_sky: None) -> None:
    with pytest.raises(sky.SkyDisabledError):
        sky.stop_burst("any-cluster")


def test_stop_burst_calls_sdk_stop(enabled_sky: MagicMock) -> None:
    sky.stop_burst("run-42-burst")
    enabled_sky.stop.assert_called_once_with("run-42-burst")


def test_status_burst_returns_not_found_for_unknown(
    enabled_sky: MagicMock,
) -> None:
    enabled_sky.status.return_value = []
    out = sky.status_burst("unknown-cluster")
    assert out["status"] == "not_found"


def test_status_burst_returns_dict_for_known(enabled_sky: MagicMock) -> None:
    enabled_sky.status.return_value = [
        {"name": "run-42-burst", "status": "RUNNING",
         "cloud": "RunPod", "last_use": "2026-05-07T19:00:00"},
    ]
    out = sky.status_burst("run-42-burst")
    assert out["status"] == "RUNNING"
    assert out["cloud"] == "RunPod"


def test_list_active_bursts_empty_when_disabled(disabled_sky: None) -> None:
    """list_active_bursts is the only public function that must not
    raise when disabled — the idle-stop daemon polls it on a timer."""
    assert sky.list_active_bursts() == []


def test_list_active_bursts_returns_normalised(enabled_sky: MagicMock) -> None:
    enabled_sky.status.return_value = [
        {"name": "c1", "status": "INIT", "cloud": "RunPod", "last_use": ""},
        {"name": "c2", "status": "UP", "cloud": "Vast", "last_use": "2026-05-07T19:00"},
    ]
    rows = sky.list_active_bursts()
    assert len(rows) == 2
    assert rows[0]["cluster_name"] == "c1"
    assert rows[1]["status"] == "UP"


def test_list_active_bursts_swallows_errors(
    enabled_sky: MagicMock,
) -> None:
    """Failing status calls don't kill the daemon."""
    enabled_sky.status.side_effect = RuntimeError("provider down")
    assert sky.list_active_bursts() == []


# ---------------------------------------------------------------------------
# _resolve_yaml_path
# ---------------------------------------------------------------------------


def test_resolve_yaml_path_strips_suffix(
    enabled_sky: MagicMock, tmp_path,
) -> None:
    # yaml_dir is set by enabled_sky fixture; same llm-burst.yaml exists
    p = sky._resolve_yaml_path("llm-burst")
    assert os.path.basename(p) == "llm-burst.yaml"


def test_resolve_yaml_path_raises_for_missing(
    enabled_sky: MagicMock,
) -> None:
    with pytest.raises(FileNotFoundError):
        sky._resolve_yaml_path("nope")


# ---------------------------------------------------------------------------
# BURSTS registry helpers
# ---------------------------------------------------------------------------


def _handle(cluster_name: str = "c-1", cost: float = 1.0) -> sky.BurstHandle:
    return sky.BurstHandle(
        cluster_name=cluster_name,
        spec_name="llm-burst",
        started_at="2026-05-07T19:00:00",
        estimated_cost_usd=cost,
        request=sky.BurstRequest(spec_name="llm-burst", cluster_name=cluster_name),
    )


def test_register_burst_records_entry() -> None:
    sky.register_burst("r-1", _handle("c-1"))
    rows = sky.list_registered_bursts()
    assert len(rows) == 1
    assert rows[0]["cluster_name"] == "c-1"
    assert rows[0]["run_id"] == "r-1"
    assert rows[0]["status"] == "running"


def test_unregister_burst_removes_entry() -> None:
    sky.register_burst("r-1", _handle("c-1"))
    popped = sky.unregister_burst("c-1")
    assert popped is not None
    assert sky.list_registered_bursts() == []


def test_unregister_unknown_returns_none() -> None:
    assert sky.unregister_burst("does-not-exist") is None


def test_cost_report_falls_back_to_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the SDK exposes no ``cost_report`` (or returns nothing),
    we report the registered estimate so callers always get a number."""
    sky.register_burst("r-1", _handle("c-1", cost=2.5))
    monkeypatch.setattr(sky, "_try_import_sky", lambda: None)
    assert sky.cost_report_for_cluster("c-1") == 2.5


def test_cost_report_uses_sdk_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sky.register_burst("r-1", _handle("c-1", cost=2.5))
    fake_sdk = MagicMock()
    fake_sdk.cost_report.return_value = [{"total_cost": 4.20}]
    monkeypatch.setattr(sky, "_try_import_sky", lambda: fake_sdk)
    assert sky.cost_report_for_cluster("c-1") == 4.20


def test_cost_report_unknown_cluster_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sky, "_try_import_sky", lambda: None)
    assert sky.cost_report_for_cluster("never-launched") == 0.0


def test_cost_report_swallows_sdk_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flaky cost_report call must not propagate — the route uses
    this on the stop path and the cluster is already going down."""
    sky.register_burst("r-1", _handle("c-1", cost=1.0))
    fake_sdk = MagicMock()
    fake_sdk.cost_report.side_effect = RuntimeError("boom")
    monkeypatch.setattr(sky, "_try_import_sky", lambda: fake_sdk)
    assert sky.cost_report_for_cluster("c-1") == 1.0


# ---------------------------------------------------------------------------
# idle_stop_pass — Phase 2.5.4 failsafe
# ---------------------------------------------------------------------------


def test_idle_stop_pass_noop_when_disabled(disabled_sky: None) -> None:
    sky.register_burst("r-1", _handle("c-1"))
    assert sky.idle_stop_pass() == []
    # Registry untouched
    assert len(sky.list_registered_bursts()) == 1


def test_idle_stop_pass_noop_when_timeout_zero(
    enabled_sky: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """timeout=0 means operators want manual control."""
    from core import config  # noqa: PLC0415
    monkeypatch.setattr(config, "SKY_IDLE_TIMEOUT_MINUTES", 0, raising=False)
    sky.register_burst("r-1", _handle("c-1"))
    assert sky.idle_stop_pass() == []


def test_idle_stop_pass_skips_active_burst(
    enabled_sky: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A burst whose ``last_use`` is recent is left alone."""
    from datetime import datetime, timezone  # noqa: PLC0415

    from core import config  # noqa: PLC0415
    monkeypatch.setattr(config, "SKY_IDLE_TIMEOUT_MINUTES", 30, raising=False)

    sky.register_burst("r-1", _handle("c-1"))
    recent = datetime.now(timezone.utc).isoformat()
    enabled_sky.status.return_value = [
        {"name": "c-1", "status": "UP", "cloud": "RunPod", "last_use": recent},
    ]
    assert sky.idle_stop_pass() == []
    enabled_sky.stop.assert_not_called()


def test_idle_stop_pass_stops_idle_burst(
    enabled_sky: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A burst idle past ``idle_timeout_minutes`` gets stopped + deregistered."""
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    from core import config  # noqa: PLC0415
    monkeypatch.setattr(config, "SKY_IDLE_TIMEOUT_MINUTES", 30, raising=False)

    sky.register_burst("r-1", _handle("c-1", cost=2.5))
    long_ago = (
        datetime.now(timezone.utc) - timedelta(minutes=60)
    ).isoformat()
    enabled_sky.status.return_value = [
        {"name": "c-1", "status": "UP", "cloud": "RunPod", "last_use": long_ago},
    ]
    enabled_sky.cost_report.return_value = []  # falls back to estimate

    stopped = sky.idle_stop_pass()
    assert stopped == ["c-1"]
    enabled_sky.stop.assert_called_once_with("c-1")
    # Deregistered.
    assert sky.list_registered_bursts() == []


def test_idle_stop_pass_no_last_use_skips(
    enabled_sky: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conservative: missing ``last_use`` is treated as not-yet-idle."""
    from core import config  # noqa: PLC0415
    monkeypatch.setattr(config, "SKY_IDLE_TIMEOUT_MINUTES", 30, raising=False)

    sky.register_burst("r-1", _handle("c-1"))
    enabled_sky.status.return_value = [
        {"name": "c-1", "status": "INIT", "cloud": "RunPod", "last_use": ""},
    ]
    assert sky.idle_stop_pass() == []


def test_idle_stop_pass_isolates_per_cluster_failures(
    enabled_sky: MagicMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One cluster's status / stop failure must not stop the loop."""
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    from core import config  # noqa: PLC0415
    monkeypatch.setattr(config, "SKY_IDLE_TIMEOUT_MINUTES", 30, raising=False)

    sky.register_burst("r-1", _handle("c-broken"))
    sky.register_burst("r-2", _handle("c-good"))
    long_ago = (
        datetime.now(timezone.utc) - timedelta(minutes=60)
    ).isoformat()

    def _status(cluster_names=None, refresh=False):
        if cluster_names == ["c-broken"]:
            raise RuntimeError("status failed")
        if cluster_names == ["c-good"]:
            return [
                {"name": "c-good", "status": "UP",
                 "cloud": "RunPod", "last_use": long_ago},
            ]
        return []

    enabled_sky.status.side_effect = _status
    enabled_sky.cost_report.return_value = []

    stopped = sky.idle_stop_pass()
    assert stopped == ["c-good"]
    # broken cluster is still registered (we didn't try to stop what we couldn't query).
    names = [b["cluster_name"] for b in sky.list_registered_bursts()]
    assert names == ["c-broken"]


# ---------------------------------------------------------------------------
# start_idle_stop_daemon — idempotency + dormant no-op
# ---------------------------------------------------------------------------


def test_start_idle_stop_daemon_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two calls back-to-back must produce one thread."""
    started: list[object] = []
    real_thread = __import__("threading").Thread

    def capturing_ctor(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        started.append(t)
        return t

    monkeypatch.setattr("core.sky.threading.Thread", capturing_ctor)
    sky.start_idle_stop_daemon(poll_interval_seconds=600)
    sky.start_idle_stop_daemon(poll_interval_seconds=600)
    sky.stop_idle_stop_daemon()
    assert len(started) == 1
