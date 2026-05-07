"""Phase 1.8.2 — subprocess.TimeoutExpired propagation tests.

Three focused tests, one per fixed callsite:
  1. execution.verify_local  → (False, "timed out…") instead of propagating
  2. execution.deploy_file   → RuntimeError("SCP timed out…") instead of propagating
  3. tools.execute_tool      → "[error] …" string (existing contract) via
                               tightened except (SubprocessError, OSError)
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Gap 1 — verify_local
# ---------------------------------------------------------------------------

def test_verify_local_timeout_returns_false(tmp_path):
    """TimeoutExpired must map to (False, "…timed out…") not propagate."""
    from execution import verify_local

    tmp_file = tmp_path / "code.py"

    with patch("execution.subprocess.run", side_effect=subprocess.TimeoutExpired(["fake-cmd"], 30)):
        ok, stderr = verify_local("print('hi')", ["fake-cmd", str(tmp_file)], str(tmp_file))

    assert ok is False
    assert "timed out" in stderr


# ---------------------------------------------------------------------------
# Gap 2 — deploy_file
# ---------------------------------------------------------------------------

def test_deploy_file_timeout_raises_runtimeerror():
    """TimeoutExpired must raise RuntimeError('SCP timed out…') to match
    the existing failure contract that callers (e.g. verify_remote) catch."""
    from core.config import SSH_TARGETS
    from execution import deploy_file

    # Use first configured target (config.example.json ships "example-target").
    target = next(iter(SSH_TARGETS.keys()))

    with patch("execution.subprocess.run", side_effect=subprocess.TimeoutExpired(["scp"], 150)):
        with pytest.raises(RuntimeError, match="timed out"):
            deploy_file("/tmp/local_src", "/tmp/remote_dst", target)


# ---------------------------------------------------------------------------
# Gap 3 — tools.execute_tool run_command handler
# ---------------------------------------------------------------------------

def test_run_command_handler_catches_timeout(monkeypatch):
    """TimeoutExpired is caught by the tightened except (SubprocessError, OSError)
    and returned as '[error] …' — same contract as before, but no longer
    swallowing unrelated exceptions."""
    import tools as tools_mod

    # Minimal builtin tool definition that exercises the local subprocess path.
    fake_registry = [
        {
            "name": "run_command",
            "description": "Run a shell command",
            "parameters": {"cmd": "command string"},
            "handler": "builtin",
            "command": "{cmd}",
        }
    ]

    monkeypatch.setattr(tools_mod, "load_tool_registry", lambda: fake_registry)

    # check_gate: always allow
    monkeypatch.setattr(tools_mod, "check_gate", lambda cmd, tool, args, run_id: (True, None, ""))
    # record_runtime_failure: no-op
    monkeypatch.setattr(tools_mod, "record_runtime_failure", lambda *a, **kw: None)

    with patch("tools.subprocess.run", side_effect=subprocess.TimeoutExpired(["echo"], 30)):
        result = tools_mod.execute_tool(
            "run_command",
            {"cmd": "echo hello"},
            target=None,   # local path (no SSH)
            run_id="test-run-timeout",
        )

    assert result.startswith("[error]")
