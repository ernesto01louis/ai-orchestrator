"""Tests for core.config_schema — Pydantic validation of config.json.

Covers:
- Valid config (config.example.json) parses cleanly.
- Missing required fields raise ValidationError.
- Wrong types raise ValidationError.
- Unknown / _comment keys are tolerated (extra="allow").
- Missing optional groups get sane defaults.
- Error messages identify the bad field's path.
- SSH_TARGETS dict shape is preserved after import.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config_schema import OrchestratorSettings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "config.example.json"


def _load_example() -> dict:
    with open(EXAMPLE_CONFIG) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_example_config_parses_cleanly() -> None:
    """config.example.json must pass validation without any errors."""
    raw = _load_example()
    settings = OrchestratorSettings.model_validate(raw)
    assert settings.ollama.main_url == "http://OLLAMA_MAIN_HOST:11434"
    assert settings.ollama.judge_url == "http://OLLAMA_JUDGE_HOST:11434"
    assert settings.autonomy.target_score == 9.0
    assert settings.autonomy.max_iterations == 3
    assert len(settings.ssh_targets) == 1
    assert settings.ssh_targets[0].name == "example-target"


def test_missing_required_ollama_main_url_raises() -> None:
    """Dropping ollama.main_url (required) must raise ValidationError."""
    raw = _load_example()
    del raw["ollama"]["main_url"]
    with pytest.raises(ValidationError) as exc_info:
        OrchestratorSettings.model_validate(raw)
    # Error message must reference the field path
    err_str = str(exc_info.value)
    assert "main_url" in err_str


def test_missing_required_ollama_judge_url_raises() -> None:
    """Dropping ollama.judge_url (required) must raise ValidationError."""
    raw = _load_example()
    del raw["ollama"]["judge_url"]
    with pytest.raises(ValidationError) as exc_info:
        OrchestratorSettings.model_validate(raw)
    assert "judge_url" in str(exc_info.value)


def test_missing_required_autonomy_target_score_raises() -> None:
    """Dropping autonomy.target_score (required) must raise ValidationError."""
    raw = _load_example()
    del raw["autonomy"]["target_score"]
    with pytest.raises(ValidationError) as exc_info:
        OrchestratorSettings.model_validate(raw)
    assert "target_score" in str(exc_info.value)


def test_wrong_type_max_iterations_raises() -> None:
    """autonomy.max_iterations must be an int; passing a string raises ValidationError."""
    raw = _load_example()
    raw["autonomy"]["max_iterations"] = "three"
    with pytest.raises(ValidationError) as exc_info:
        OrchestratorSettings.model_validate(raw)
    err_str = str(exc_info.value)
    assert "max_iterations" in err_str


def test_wrong_type_target_score_raises() -> None:
    """autonomy.target_score must be a float/int; passing a dict raises ValidationError."""
    raw = _load_example()
    raw["autonomy"]["target_score"] = {"not": "a number"}
    with pytest.raises(ValidationError) as exc_info:
        OrchestratorSettings.model_validate(raw)
    assert "target_score" in str(exc_info.value)


def test_unknown_top_level_keys_tolerated() -> None:
    """_comment and other unknown top-level keys must not cause ValidationError."""
    raw = _load_example()
    raw["_comment"] = "This is a documentation key"
    raw["_future_feature"] = {"some": "value"}
    # Should not raise
    settings = OrchestratorSettings.model_validate(raw)
    assert settings.ollama.main_url  # basic sanity


def test_comment_note_keys_in_nested_models_tolerated() -> None:
    """_*_note keys inside nested models (e.g. notifications) must be tolerated."""
    raw = _load_example()
    raw["notifications"]["_gotify_token_note"] = "Set GOTIFY_TOKEN in .env"
    raw["cloud_image_gen"]["_api_key_note"] = "Set REPLICATE_API_TOKEN in .env"
    settings = OrchestratorSettings.model_validate(raw)
    assert settings.notifications.enabled is False


def test_missing_optional_timeouts_block_uses_defaults() -> None:
    """Omitting the entire 'timeouts' block must yield the schema defaults."""
    raw = _load_example()
    del raw["timeouts"]
    settings = OrchestratorSettings.model_validate(raw)
    assert settings.timeouts.embedding == 1800
    assert settings.timeouts.llm_generate == 2400
    assert settings.timeouts.hindsight_recall == 120


def test_missing_optional_hindsight_block_uses_defaults() -> None:
    """Omitting 'hindsight' must yield the schema defaults."""
    raw = _load_example()
    del raw["hindsight"]
    settings = OrchestratorSettings.model_validate(raw)
    assert settings.hindsight.url == "http://192.168.2.203:8888"
    assert settings.hindsight.bank_id == "Orchestrator"
    assert settings.hindsight.enabled is True
    assert settings.hindsight.timeout == 120


def test_missing_optional_dream_block_uses_defaults() -> None:
    """Omitting 'dream' must yield auto_interval default of 10."""
    raw = _load_example()
    raw.pop("dream", None)  # may not be in example; that's fine
    settings = OrchestratorSettings.model_validate(raw)
    assert settings.dream.auto_interval == 10


def test_missing_optional_postgres_block_uses_defaults() -> None:
    """Phase 2.1: omitting 'postgres' must yield enabled=False (dormant)."""
    raw = _load_example()
    raw.pop("postgres", None)
    settings = OrchestratorSettings.model_validate(raw)
    assert settings.postgres.enabled is False
    assert settings.postgres.dsn == ""
    assert settings.postgres.pool_size == 5
    assert settings.postgres.pool_max_overflow == 5
    assert settings.postgres.statement_timeout_ms == 5000
    assert settings.postgres.reconcile_on_startup is True


def test_postgres_block_in_example_parses_to_disabled() -> None:
    """The 'postgres' block in config.example.json must default to disabled."""
    raw = _load_example()
    settings = OrchestratorSettings.model_validate(raw)
    # Whether or not the example ships the block, defaults must keep it dormant
    assert settings.postgres.enabled is False
    assert settings.postgres.dsn == ""


def test_postgres_enabled_with_dsn_parses() -> None:
    """A configured postgres block with enabled=true and a real DSN parses."""
    raw = _load_example()
    raw["postgres"] = {
        "enabled": True,
        "dsn": "postgresql://orchestrator:secret@192.168.2.183:5432/orchestrator",
        "pool_size": 8,
        "statement_timeout_ms": 3000,
        "reconcile_on_startup": False,
    }
    settings = OrchestratorSettings.model_validate(raw)
    assert settings.postgres.enabled is True
    assert settings.postgres.dsn.startswith("postgresql://")
    assert "orchestrator" in settings.postgres.dsn
    assert settings.postgres.pool_size == 8
    assert settings.postgres.statement_timeout_ms == 3000
    assert settings.postgres.reconcile_on_startup is False


def test_postgres_wrong_type_pool_size_raises() -> None:
    """postgres.pool_size must be an int; passing a string raises ValidationError."""
    raw = _load_example()
    raw["postgres"] = {"enabled": True, "pool_size": "many"}
    with pytest.raises(ValidationError) as exc_info:
        OrchestratorSettings.model_validate(raw)
    assert "pool_size" in str(exc_info.value)


def test_postgres_unknown_keys_tolerated() -> None:
    """_*_note keys inside the postgres block must be tolerated (extra='allow')."""
    raw = _load_example()
    raw["postgres"] = {
        "_dsn_note": "Set POSTGRES_DSN in .env",
        "enabled": False,
    }
    settings = OrchestratorSettings.model_validate(raw)
    assert settings.postgres.enabled is False


def test_core_config_exposes_postgres_constants() -> None:
    """core.config must export the POSTGRES_* derived constants."""
    import core.config as cfg  # noqa: PLC0415

    assert hasattr(cfg, "POSTGRES_ENABLED")
    assert hasattr(cfg, "POSTGRES_DSN")
    assert hasattr(cfg, "POSTGRES_POOL_SIZE")
    assert hasattr(cfg, "POSTGRES_POOL_MAX_OVERFLOW")
    assert hasattr(cfg, "POSTGRES_STATEMENT_TIMEOUT_MS")
    assert hasattr(cfg, "POSTGRES_RECONCILE_ON_STARTUP")
    # Defaults from config.example.json keep the feature dormant
    assert cfg.POSTGRES_ENABLED is False
    assert isinstance(cfg.POSTGRES_POOL_SIZE, int)


def test_missing_optional_redis_block_uses_defaults() -> None:
    """Phase 2.2: omitting 'redis' must yield enabled=False (dormant)."""
    raw = _load_example()
    raw.pop("redis", None)
    settings = OrchestratorSettings.model_validate(raw)
    assert settings.redis.enabled is False
    assert settings.redis.url == ""
    assert settings.redis.socket_connect_timeout == 2.0
    assert settings.redis.socket_timeout == 5.0
    assert settings.redis.run_status_ttl == 86400
    assert settings.redis.url_cache_ttl == 60
    assert settings.redis.embed_cache_ttl == 604800


def test_redis_block_in_example_parses_to_disabled() -> None:
    """The 'redis' block in config.example.json must default to disabled."""
    raw = _load_example()
    settings = OrchestratorSettings.model_validate(raw)
    assert settings.redis.enabled is False
    assert settings.redis.url == ""


def test_redis_enabled_with_url_parses() -> None:
    """A configured redis block with enabled=true and a real URL parses."""
    raw = _load_example()
    raw["redis"] = {
        "enabled": True,
        "url": "redis://:secret@192.168.2.185:6379/0",
        "socket_connect_timeout": 1.0,
        "socket_timeout": 3.0,
        "run_status_ttl": 3600,
        "url_cache_ttl": 30,
        "embed_cache_ttl": 86400,
    }
    settings = OrchestratorSettings.model_validate(raw)
    assert settings.redis.enabled is True
    assert settings.redis.url.startswith("redis://")
    assert settings.redis.socket_connect_timeout == 1.0
    assert settings.redis.socket_timeout == 3.0
    assert settings.redis.run_status_ttl == 3600


def test_redis_wrong_type_run_status_ttl_raises() -> None:
    """redis.run_status_ttl must be an int; passing a string raises ValidationError."""
    raw = _load_example()
    raw["redis"] = {"enabled": True, "run_status_ttl": "forever"}
    with pytest.raises(ValidationError) as exc_info:
        OrchestratorSettings.model_validate(raw)
    assert "run_status_ttl" in str(exc_info.value)


def test_redis_unknown_keys_tolerated() -> None:
    """_*_note keys inside the redis block must be tolerated (extra='allow')."""
    raw = _load_example()
    raw["redis"] = {
        "_url_note": "Set REDIS_URL in .env",
        "enabled": False,
    }
    settings = OrchestratorSettings.model_validate(raw)
    assert settings.redis.enabled is False


def test_core_config_exposes_redis_constants() -> None:
    """core.config must export the REDIS_* derived constants."""
    import core.config as cfg  # noqa: PLC0415

    assert hasattr(cfg, "REDIS_ENABLED")
    assert hasattr(cfg, "REDIS_URL")
    assert hasattr(cfg, "REDIS_SOCKET_CONNECT_TIMEOUT")
    assert hasattr(cfg, "REDIS_SOCKET_TIMEOUT")
    assert hasattr(cfg, "REDIS_RUN_STATUS_TTL")
    assert hasattr(cfg, "REDIS_URL_CACHE_TTL")
    assert hasattr(cfg, "REDIS_EMBED_CACHE_TTL")
    assert isinstance(cfg.REDIS_RUN_STATUS_TTL, int)


def test_core_config_redis_url_env_overrides_config() -> None:
    """REDIS_URL env var must win over the config.json value (mirrors POSTGRES_DSN).

    Runs in a subprocess to avoid contaminating the current core.config import.
    """
    import subprocess
    import sys

    script = (
        "import os, sys; sys.path.insert(0, '.'); "
        "os.environ['REDIS_URL'] = 'redis://envwin:1@override-host:6379/9'; "
        "import core.config as cfg; "
        "print(cfg.REDIS_URL)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        cwd=REPO_ROOT,
        check=True,
    )
    assert b"redis://envwin:1@override-host:6379/9" in result.stdout


def test_core_config_postgres_dsn_env_overrides_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSTGRES_DSN env var must win over the config.json value (mirrors GOTIFY_TOKEN).

    Runs in a subprocess to avoid contaminating the current core.config import.
    """
    import subprocess
    import sys

    script = (
        "import os, sys; sys.path.insert(0, '.'); "
        "os.environ['POSTGRES_DSN'] = 'postgresql://env-wins/db'; "
        "import core.config as cfg; "
        "print(cfg.POSTGRES_DSN)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        cwd=REPO_ROOT,
        check=True,
    )
    assert b"postgresql://env-wins/db" in result.stdout


def test_error_message_identifies_field_path() -> None:
    """ValidationError on a nested required field must name the field path clearly."""
    bad_cfg = {
        "ollama": {"main_url": "http://host:11434"},  # judge_url missing
        "autonomy": {"target_score": 9.0, "max_iterations": 3},
        "ssh_targets": [],
    }
    with pytest.raises(ValidationError) as exc_info:
        OrchestratorSettings.model_validate(bad_cfg)
    err_str = str(exc_info.value)
    # Pydantic reports the path like "ollama.judge_url" or "judge_url"
    assert "judge_url" in err_str


def test_ssh_targets_items_require_all_four_fields() -> None:
    """An ssh_targets item missing 'key_path' must raise ValidationError."""
    raw = _load_example()
    raw["ssh_targets"] = [{"name": "t", "host": "h", "username": "u"}]  # no key_path
    with pytest.raises(ValidationError) as exc_info:
        OrchestratorSettings.model_validate(raw)
    assert "key_path" in str(exc_info.value)


def test_ssh_targets_empty_list_is_valid() -> None:
    """ssh_targets may be an empty list (no targets configured)."""
    raw = _load_example()
    raw["ssh_targets"] = []
    settings = OrchestratorSettings.model_validate(raw)
    assert settings.ssh_targets == []


def test_core_config_imports_without_error() -> None:
    """Smoke-test: importing core.config succeeds (uses config.example.json in CI)."""
    import core.config as cfg  # noqa: PLC0415

    assert cfg.OLLAMA_MAIN_URL
    assert cfg.OLLAMA_JUDGE_URL
    assert isinstance(cfg.MAX_ITERATIONS, int)
    assert isinstance(cfg.TARGET_SCORE, float)
    assert isinstance(cfg.SSH_TARGETS, dict)
    assert isinstance(cfg.CONFIG, dict)


def test_core_config_systemexit_on_invalid_config(tmp_path: Path) -> None:
    """Import-time fail-fast: broken config.json must cause SystemExit with a useful message.

    Runs in a subprocess to avoid corrupting the current process's core.config module.
    """
    import subprocess
    import sys

    bad_config = tmp_path / "config.json"
    # ollama block present but missing the required judge_url field
    bad_config.write_text(
        json.dumps(
            {
                "ollama": {"main_url": "http://host:11434"},
                "autonomy": {"target_score": 9.0, "max_iterations": 3},
                "ssh_targets": [],
            }
        )
    )

    script = (
        "import sys; sys.path.insert(0, '.'); "
        "import core.paths; "
        f"core.paths.CONFIG_PATH = '{bad_config}'; "
        "import importlib, core.config; importlib.reload(core.config)"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode != 0, "Expected non-zero exit for invalid config"
    output = result.stderr + result.stdout
    assert b"[core.config]" in output, f"Expected '[core.config]' in output; got: {output!r}"
    assert str(bad_config).encode() in output, (
        f"Expected config path in output; got: {output!r}"
    )
    # Pydantic includes the field path in the error — judge_url is the missing required field
    assert b"judge_url" in output, f"Expected 'judge_url' in output; got: {output!r}"
