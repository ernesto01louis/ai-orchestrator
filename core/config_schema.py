"""Pydantic v2 schema for config.json.

Validates the orchestrator's configuration file at import time so that
missing required fields and type errors surface with a clear message
rather than as AttributeError deep in a request path.

Design choices:
- ``extra="allow"`` on every model: ``_comment`` / ``_*_note`` keys in the
  JSON are documentation-only and must be tolerated without warnings.
- Required vs. optional mirrors today's ``CONFIG[...]`` vs.
  ``CONFIG.get(..., default)`` pattern in ``core/config.py``.
- No ``HttpUrl`` validators: orchestrator URLs are LAN/Tailscale strings
  that the codebase string-concatenates (``+ "/api/chat"``); ``HttpUrl``
  would return an object that breaks those concatenations.
- ``pydantic-settings`` is already vendored but we use plain
  ``BaseModel`` here because the source is a JSON file we load ourselves,
  not environment variables.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Shared base that allows unknown keys (tolerates _comment / _*_note)
# ---------------------------------------------------------------------------

class _FlexModel(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Nested group models
# ---------------------------------------------------------------------------

class OllamaConfig(_FlexModel):
    # Required (raw CONFIG["ollama"]["main_url"] / ["judge_url"])
    main_url: str
    judge_url: str
    # Optional — defaults mirror config.py
    planner_url: str = ""        # falls back to judge_url in config.py
    judge_fallback_model: str = ""


class HindsightConfig(_FlexModel):
    url: str = "http://192.168.2.203:8888"
    bank_id: str = "Orchestrator"
    enabled: bool = True
    timeout: int = 120


class NotificationsConfig(_FlexModel):
    enabled: bool = False
    service: str = "ntfy"
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = "ai-orchestrator"
    ntfy_priority: str = "default"
    gotify_url: str = ""
    gotify_token: str = ""
    gotify_priority: int = 5
    on_success: bool = True
    on_failure: bool = True
    ntfy_urls: list[str] = []
    gotify_urls: list[str] = []
    orchestrator_url: str = "http://192.168.2.216:8000"
    strategy: str = "failover"


class VaultConfig(_FlexModel):
    enabled: bool = False
    local_dir: str = "/opt/ai-orchestrator/vault"
    remote_host: str = ""
    remote_user: str = "root"
    remote_key: str = "/root/.ssh/id_rsa"
    remote_dir: str = "/opt/notediscovery/data"
    sync_enabled: bool = True
    nas_enabled: bool = False
    nas_path: str = "/mnt/nas-vault/ai-orchestrator-vault"


class GenerationConfig(_FlexModel):
    parallel: bool = True


class AutonomyConfig(_FlexModel):
    # Required (raw CONFIG["autonomy"]["target_score"] / ["max_iterations"])
    target_score: float
    max_iterations: int
    # Optional
    enabled: bool = True
    max_troubleshoot_attempts: int = 3


class SshConfig(_FlexModel):
    timeout: int = 120


class TimeoutsConfig(_FlexModel):
    embedding: int = 1800
    llm_generate: int = 2400
    llm_structured: int = 2400
    hindsight_retain: int = 600
    hindsight_recall: int = 120
    hindsight_reflect: int = 600
    vault_sync: int = 180
    vault_nas_sync: int = 120


class SudoConfig(_FlexModel):
    enabled: bool = False
    allowed_commands: list[str] = []


class DeployConfig(_FlexModel):
    base_path: str = "~/ai-projects"


class DebugConfig(_FlexModel):
    verbose: bool = False


class SshTargetConfig(_FlexModel):
    """One entry in the ``ssh_targets`` list."""
    name: str
    host: str
    username: str
    key_path: str


class CloudImageGenFallback(_FlexModel):
    provider: str = "gemini"
    model: str = "gemini-2.5-flash-image"
    api_key: str = ""


class CloudImageGenConfig(_FlexModel):
    enabled: bool = False
    provider: str = "replicate"
    api_key: str = ""
    default_model: str = "black-forest-labs/flux-dev"
    fallback: CloudImageGenFallback = CloudImageGenFallback()


class PrefectConfig(_FlexModel):
    api_url: str = "http://prefect.tailnet:4200/api"
    execution_mode: str = "in_process"
    work_pool: str = "orchestrator-pool"


class DreamConfig(_FlexModel):
    auto_interval: int = 10


# ---------------------------------------------------------------------------
# Top-level settings model
# ---------------------------------------------------------------------------

class OrchestratorSettings(_FlexModel):
    """Validated view of config.json.

    Required fields (no default) mirror the ``CONFIG["x"]`` accesses that
    would raise ``KeyError`` today.  Optional fields keep their existing
    defaults from ``core/config.py``.
    """

    # Required groups
    ollama: OllamaConfig
    autonomy: AutonomyConfig
    # Required list (may be empty, but the key must exist)
    ssh_targets: list[SshTargetConfig]

    # Optional groups with sane defaults
    hindsight: HindsightConfig = HindsightConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    vault: VaultConfig = VaultConfig()
    generation: GenerationConfig = GenerationConfig()
    ssh: SshConfig = SshConfig()
    timeouts: TimeoutsConfig = TimeoutsConfig()
    sudo: SudoConfig = SudoConfig()
    deploy: DeployConfig = DeployConfig()
    debug: DebugConfig = DebugConfig()
    cloud_image_gen: CloudImageGenConfig = CloudImageGenConfig()
    mcp_servers: dict[str, object] = {}
    prefect: PrefectConfig = PrefectConfig()
    dream: DreamConfig = DreamConfig()
