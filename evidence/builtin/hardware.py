"""Hardware-fingerprint calculator.

Augments the bundle's typed ``HardwareFingerprint`` (cpu, ram, gpus,
os, kernel) with finer-grained detail useful for honest re-running
attempts: CPU flag set (avx2/avx512 etc), nvidia-smi summary if
present, network-interface list. Reads /proc/cpuinfo and a few
shell utilities; fails open on any error.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from core.evidence import CalculatorResult
from evidence.hookspecs import hookimpl

if TYPE_CHECKING:
    from core.campaign import Campaign
    from core.evidence import RunRecord


_CALCULATOR_ID = "ai_orchestrator.builtin.hardware:v1"
_OUTPUT_SCHEMA_VERSION = "1.0.0"


def _read_cpu_flags() -> list[str]:
    """Return the union of ``flags:`` lines from /proc/cpuinfo."""
    cpuinfo = Path("/proc/cpuinfo")
    if not cpuinfo.exists():
        return []
    flags: set[str] = set()
    try:
        for line in cpuinfo.read_text().splitlines():
            if line.startswith("flags") and ":" in line:
                _, _, raw = line.partition(":")
                flags.update(raw.strip().split())
    except OSError:
        return []
    return sorted(flags)


def _nvidia_smi_summary() -> str | None:
    """One-line summary per GPU; None if nvidia-smi not installed."""
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _network_interfaces() -> list[dict[str, str]]:
    """Best-effort interface list; returns [] if `ip` is unavailable."""
    if shutil.which("ip") is None:
        return []
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    interfaces: list[dict[str, str]] = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            interfaces.append({"name": parts[1], "addr": parts[3]})
    return interfaces


@hookimpl
def compute_evidence(
    campaign: "Campaign", runs: "list[RunRecord]"
) -> list[CalculatorResult]:
    started = time.monotonic()

    output = {
        "hostname": socket.gethostname(),
        "cpu_flags": _read_cpu_flags(),
        "nvidia_smi": _nvidia_smi_summary(),
        "network_interfaces": _network_interfaces(),
    }

    duration_ms = int((time.monotonic() - started) * 1000)
    return [
        CalculatorResult(
            kind="hardware_fingerprint",
            calculator_id=_CALCULATOR_ID,
            schema_version=_OUTPUT_SCHEMA_VERSION,
            inputs={"campaign_id": campaign.id},
            output=output,
            duration_ms=duration_ms,
            deterministic=False,  # depends on host
        )
    ]
