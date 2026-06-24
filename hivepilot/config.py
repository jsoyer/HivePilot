from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _xdg_config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "hivepilot"


def _resolve_env_file() -> str:
    """Return the .env path to load, following XDG then cwd precedence."""
    explicit = os.environ.get("HIVEPILOT_ENV_FILE")
    if explicit:
        return explicit
    xdg_env = _xdg_config_dir() / ".env"
    if xdg_env.exists():
        return str(xdg_env)
    return ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HIVEPILOT_",
        env_file=_resolve_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_dir: Path = Field(default_factory=lambda: Path.cwd())
    projects_file: Path = Path("projects.yaml")
    tasks_file: Path = Path("tasks.yaml")
    pipelines_file: Path = Path("pipelines.yaml")
    policies_file: Path = Path("policies.yaml")
    groups_file: Path = Path("groups.yaml")
    schedules_file: Path = Path("schedules.yaml")
    prompts_dir: Path = Path("prompts")
    runs_dir: Path = Path("runs")
    logs_dir: Path = Path("runs/logs")
    claude_profiles_file: Path = Path("model_profiles.yaml")
    state_db: Path = Path("state.db")
    tokens_file: Path = Path("api_tokens.yaml")
    default_runner: str = "claude"
    default_model: str | None = None
    # Default target (project or group) for direct agent orders when no @target is
    # given (Telegram /ask, /dev, … and @mention routing). Required by telegram_bot.
    default_target: str = "noxys"
    claude_command: str = "claude"
    # Permission mode passed to `claude --print` so the developer agent can edit
    # files autonomously in headless mode. Without it, claude blocks waiting for
    # an interactive permission prompt it can never show (the run hangs to timeout
    # and writes nothing). Values: acceptEdits (edits autonomously, bash still
    # gated) | bypassPermissions (full autonomy) | plan | default. None = no flag
    # (the safe default; suitable for read-only planning agents).
    claude_permission_mode: str | None = None
    gh_command: str = "gh"
    git_command: str = "git"
    concurrency_limit: int = 4
    interactive_default_all: bool = False
    enable_textual_ui: bool = False
    output_format: str = "json"
    plugins_entry: str | None = None
    discovery_roots: list[str] = Field(default_factory=lambda: ["~/dev"])
    api_host: str = "127.0.0.1"
    api_port: int = 8045
    api_root_path: str = ""  # set to "/hivepilot" when behind a path-prefix proxy
    api_allowed_origins: list[str] = Field(default_factory=list)
    chatops_token: str | None = None
    secrets_allowed_dirs: list[str] = Field(default_factory=list)
    token_ttl_days: int | None = None
    log_to_file: bool = False
    api_max_body_size: int = 1_048_576  # 1 MB
    http_proxy: str | None = None
    https_proxy: str | None = None
    no_proxy: str | None = None
    config_repo: str | None = None
    config_branch: str = "main"
    domain: str | None = None  # public domain used by caddy + webhook auto-registration
    telegram_bot_token: str | None = None
    telegram_allowed_chat_ids: list[int] = Field(default_factory=list)
    telegram_notification_chat_id: int | None = (
        None  # proactive notifications (approvals, run results)
    )
    telegram_stream_chat_id: int | None = (
        None  # dedicated channel for the live agent stream (falls back to notification chat)
    )
    telegram_webhook_url: str | None = None
    telegram_webhook_secret: str | None = None
    telegram_webhook_port: int = 8443
    telegram_stream_live: bool = True  # live-stream agent turns to Telegram during runs
    telegram_stream_topics: bool = False  # env: HIVEPILOT_TELEGRAM_STREAM_TOPICS — route each agent's turns to its own forum topic
    telegram_stream_rich: bool = True  # env: HIVEPILOT_TELEGRAM_STREAM_RICH — render HTML cards with status badge, bullets, links
    auditor_auto: bool = (
        True  # run Henri (external auditor) automatically after each pipeline cycle
    )
    container_runtime: str = "docker"  # container runtime for the container runner: docker | podman
    auto_commit_vault: bool = False  # git add/commit/push the Obsidian vault after a pipeline run
    event_webhook_url: str | None = None  # POST pipeline lifecycle events here (n8n, etc.)
    event_webhook_token: str | None = None  # optional bearer token for the event webhook
    ssh_options: list[str] = Field(default_factory=list)  # extra ssh -o options for remote agents
    worker_token: str | None = None  # shared bearer token between hub and remote workers
    worker_port: int = 8900  # default port for `hivepilot worker`
    worker_retries: int = 2  # retry attempts on transient worker dispatch failures (W3)
    worker_fallback_local: bool = False  # on worker failure, run the step locally (W3)
    worker_max_concurrency: int = 4  # max concurrent dispatches to a single worker (W4)

    # Token-saving caching (L1-L3)
    anthropic_prompt_cache: bool = True  # add cache_control to Anthropic system block (L1)
    prior_context_mode: str = "cap"  # full | synthesis | cap (L2)
    max_prior_context_chars: int = 8000  # max chars for cap mode (L2)
    stage_cache_enabled: bool = False  # opt-in SQLite stage memoization (L3)
    cache_backend: str = "sqlite"  # sqlite | redis (L3)
    redis_url: str | None = None  # required when cache_backend=redis (L3)
    worktree_isolation: bool = True  # run dev/role tasks inside a throwaway git worktree (env: HIVEPILOT_WORKTREE_ISOLATION)
    claude_max_concurrency: int = (
        1  # max concurrent claude steps (env: HIVEPILOT_CLAUDE_MAX_CONCURRENCY)
    )
    dev_fallback_runners: list[str] = Field(
        default_factory=lambda: ["codex", "cursor"]
    )  # fallback runner order for developer role on claude quota (env: HIVEPILOT_DEV_FALLBACK_RUNNERS)
    dev_batch_size: int = Field(
        default=0,
        description="Max components per fan-out pass (0 = unlimited). env: HIVEPILOT_DEV_BATCH_SIZE",
    )
    # Challenge rebuttal rounds (Part B)
    enable_challenge_rounds: bool = True  # run bounded rebuttal when a stage issues a challenge
    max_challenge_rounds: int = 1  # 1 = one rebuttal + one resolution check
    # Tier-2: on-demand orchestrator-mediated agent requests
    enable_agent_requests: bool = True
    max_agent_requests: int = 3  # per stage turn (max REQUEST: lines honoured)
    max_request_depth: int = 2  # recursion depth cap (requests from answers)
    max_requests_per_run: int = 20  # global budget per pipeline run

    @field_validator("telegram_notification_chat_id", "telegram_stream_chat_id", mode="before")
    @classmethod
    def _coerce_notification_chat_id(cls, v: object) -> object:
        # Lenient: empty -> None; a pasted JSON array / list -> its first id.
        if v in ("", None):
            return None
        if isinstance(v, str) and v.strip().startswith("["):
            import json

            try:
                items = json.loads(v)
            except Exception:
                return None
            return items[0] if items else None
        if isinstance(v, (list, tuple)):
            return v[0] if v else None
        return v

    slack_bot_token: str | None = None
    slack_signing_secret: str | None = None
    slack_app_token: str | None = None  # for Socket Mode (xapp-...)
    slack_allowed_channel_ids: list[str] = Field(default_factory=list)
    slack_notification_channel_id: str | None = None  # proactive notifications
    discord_bot_token: str | None = None
    discord_public_key: str | None = None  # Ed25519 public key for HTTP interactions
    discord_allowed_guild_ids: list[int] = Field(default_factory=list)
    discord_allowed_channel_ids: list[int] = Field(default_factory=list)
    discord_notification_channel_id: int | None = None  # proactive notifications
    linear_api_key: str | None = None
    linear_team_id: str | None = None  # default team for issue creation
    linear_default_project_id: str | None = None  # default project
    linear_webhook_secret: str | None = None  # HMAC secret for webhook verification
    notion_token: str | None = None
    notion_runs_database_id: str | None = None  # database where run logs are written
    obsidian_vault: Path = Path("obsidian-vault")

    # Governance repository root (e.g. /path/to/shared-governance-repo or https URL)
    # Deployment-specific; leave None to disable governance file injection.
    governance_repo: str | None = Field(
        default=None,
        validation_alias="HIVEPILOT_GOVERNANCE_REPO",
    )

    # Governance file names (relative to governance_repo root) to inject into prompts.
    governance_files: list[str] = Field(
        default_factory=lambda: [
            "CLAUDE.md",
            "AGENTS.md",
            "AGENT-GOVERNANCE.md",
            ".cursorrules",
            ".windsurfrules",
            "GEMINI.md",
        ],
    )

    @property
    def xdg_config_home(self) -> Path:
        """~/.config/hivepilot (or $XDG_CONFIG_HOME/hivepilot)"""
        return _xdg_config_dir()

    @property
    def xdg_data_home(self) -> Path:
        """~/.local/share/hivepilot (or $XDG_DATA_HOME/hivepilot)"""
        return Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() / "hivepilot"

    def resolve_path(self, path: Path) -> Path:
        return (self.base_dir / path).expanduser().resolve()

    def _config_repo_local_path(self) -> Path | None:
        """Return config_repo as a Path if it is an existing local directory, else None."""
        if not self.config_repo:
            return None
        p = Path(self.config_repo).expanduser()
        if p.is_dir():
            return p
        return None

    def resolve_config_path(self, filename: str | Path) -> Path:
        """
        Resolve a config file using XDG priority chain:

        1. $XDG_CONFIG_HOME/hivepilot/<filename>   — local machine override
        2. config_repo/<filename>                  — shared config (local path)
        3. base_dir/<filename>                     — cwd fallback
        """
        name = Path(filename)

        xdg = self.xdg_config_home / name
        if xdg.exists():
            return xdg

        local_repo = self._config_repo_local_path()
        if local_repo:
            candidate = local_repo / name
            if candidate.exists():
                return candidate

        return self.resolve_path(name)


settings = Settings()
