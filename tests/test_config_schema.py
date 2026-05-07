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
