from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

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
    roles_file: Path = Path("roles.yaml")
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
    default_target: str = "acme"
    # Default pipeline name used by @mention and /run commands. Deployment-specific;
    # override via env HIVEPILOT_DEFAULT_PIPELINE.
    default_pipeline: str = "default"
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
    plugins_enabled: bool = True  # master on/off switch for local-file + entry-point plugin loading
    # Names of plugins to skip loading even when discovered (local-file stem
    # or entry-point name) — complements plugins_enabled above, which is an
    # all-or-nothing master switch: this is a per-plugin skip list. Toggled
    # by the TUI plugin manager's `space` binding (hivepilot/ui/plugin_manager.py),
    # which persists changes to .env; effective on next start only (plugins
    # load once at PluginManager construction, no live reload).
    # env: HIVEPILOT_PLUGINS_DISABLED
    plugins_disabled: list[str] = Field(default_factory=list)
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
    vault_addr: str | None = None  # HashiCorp Vault address (env: HIVEPILOT_VAULT_ADDR)
    vault_token: str | None = None  # HashiCorp Vault token (env: HIVEPILOT_VAULT_TOKEN)
    # ---- Infisical secrets provider (plugins/infisical.py) ----
    # Config for the first-party `infisical` secrets-backend plugin (Infisical
    # is an open-source, self-hostable config/value store — https://infisical.com).
    # All optional; when a required value is missing the plugin's resolve()
    # raises a clear error naming ONLY the secret + provider (never the fetched
    # value) so the `closed` fail-mode aborts. A ref.spec may override
    # environment / path / workspace per-secret.
    # Self-host base URL (e.g. https://infisical.example.com). Unset -> the
    # Infisical SDK default (hosted app.infisical.com). env: HIVEPILOT_INFISICAL_URL
    infisical_url: str | None = None
    # Access token / machine-identity token used to authenticate the SDK client.
    # env: HIVEPILOT_INFISICAL_TOKEN
    infisical_token: str | None = None
    # Project / workspace id the secret lives in (a ref.spec `workspace_id` /
    # `project_id` overrides this per-secret). env: HIVEPILOT_INFISICAL_WORKSPACE_ID
    infisical_workspace_id: str | None = None
    # Environment slug (e.g. "dev" / "prod"); a ref.spec `environment` overrides
    # this per-secret. env: HIVEPILOT_INFISICAL_ENVIRONMENT
    infisical_environment: str | None = None
    # ---- 1Password secrets provider (plugins/onepassword.py) ----
    # Config for the first-party `onepassword` secrets-backend plugin. It talks
    # to a 1Password Connect endpoint (self-hostable) via the
    # `onepasswordconnectsdk` package (imported lazily; never installed by the
    # plugin). All optional; when the required token/host is missing — or the
    # SDK / client / value is unavailable — the plugin's resolve() raises a
    # clear error naming ONLY the reference identity (op://vault/item/field) +
    # provider (never the token or fetched value) so the `closed` fail-mode
    # aborts. A ref.spec supplies vault/item/field (or a full `op://` ref).
    # 1Password Connect API base URL (e.g. https://op-connect.example.com).
    # Required for both credential modes below. env: HIVEPILOT_OP_CONNECT_HOST
    op_connect_host: str | None = None
    # 1Password Connect token used to authenticate the SDK client (Connect
    # credential mode). env: HIVEPILOT_OP_CONNECT_TOKEN
    op_connect_token: str | None = None
    # 1Password service-account token — an alternative credential presented to
    # the same Connect endpoint (service-account credential mode). Used only
    # when no Connect token is set. env: HIVEPILOT_OP_SERVICE_ACCOUNT_TOKEN
    op_service_account_token: str | None = None
    worker_retries: int = 2  # retry attempts on transient worker dispatch failures (W3)
    worker_fallback_local: bool = False  # on worker failure, run the step locally (W3)
    worker_max_concurrency: int = 4  # max concurrent dispatches to a single worker (W4)

    # ---- herdr runner plugin (plugins/herdr.py) ----
    # Config for the first-party `herdr` runner plugin: executes each step
    # inside a dedicated herdr (terminal multiplexer for coding agents) pane
    # via `herdr pane split` -> `pane run` -> `wait agent-status` -> `pane
    # read`, giving live parallel-pane visibility. All optional; the plugin
    # degrades gracefully to raw command execution when `herdr` isn't on
    # PATH regardless of these values.
    # Timeout (ms) for `herdr wait agent-status --status idle`; a pane that
    # doesn't reach idle within this window is treated as a step failure
    # (fail-closed — blocked/unknown/timeout are never silently success).
    # env: HIVEPILOT_HERDR_WAIT_TIMEOUT_MS
    herdr_wait_timeout_ms: int = 300000
    # Lines of scrollback to capture via `herdr pane read --lines`.
    # env: HIVEPILOT_HERDR_READ_LINES
    herdr_read_lines: int = 200
    # Direction passed to `herdr pane split --direction`.
    # env: HIVEPILOT_HERDR_SPLIT_DIRECTION
    herdr_split_direction: str = "right"

    # Database backend — None keeps SQLite at state_db (default); set to
    # "postgresql://..." to switch to Postgres (requires psycopg[binary]).
    # env: HIVEPILOT_DATABASE_URL
    database_url: str | None = None

    # Token-saving caching (L1-L3)
    anthropic_prompt_cache: bool = True  # add cache_control to Anthropic system block (L1)
    prior_context_mode: str = "cap"  # full | synthesis | cap (L2)
    max_prior_context_chars: int = 8000  # max chars for cap mode (L2)
    # PRD A2 Sprint 2: prior-context routing mode.
    # "full"  (default) — today's behaviour: build_prior_context() over ALL
    #          prior_chunks, for EVERY role, regardless of whether that role
    #          declares `inputs` in roles.yaml. Byte-identical to pre-Sprint-2.
    # "keyed" (opt-in)  — a stage whose role declares non-empty `inputs` gets
    #          its prior context assembled from ONLY those input keys via the
    #          Sprint-1 `outputs_by_key` run-scoped store, with a conservative
    #          fallback to full context when none of the keys are present.
    # Gating is on THIS flag ONLY, never on input-presence: roles.yaml already
    # declares `inputs` cosmetically on every role, so gating on presence
    # would silently regress every existing pipeline to a keyed subset.
    # env: HIVEPILOT_CONTEXT_ROUTING_MODE
    context_routing_mode: Literal["full", "keyed"] = "full"
    # Opt-in gate for the `headroom` before_step plugin (plugins/headroom.py):
    # lossy compression of shared pipeline context (prior_context/extra_prompt)
    # via the optional `headroom-ai` library. Defaults False — ships dormant
    # even when the plugin file is present and the library is installed;
    # mirrors context_routing_mode's opt-in-only gating above.
    # env: HIVEPILOT_HEADROOM_ENABLED
    headroom_enabled: bool = False
    # Opt-in gate for the `mem0` before_step/after_step plugin (plugins/mem0.py):
    # persistent cross-run agent memory (recall before a step, store after)
    # via the optional `mem0ai` library. Defaults False — ships dormant even
    # when the plugin file is present and the library is installed; mirrors
    # headroom_enabled's opt-in-only gating above.
    # env: HIVEPILOT_MEM0_ENABLED
    mem0_enabled: bool = False
    # Hosted mem0 API key (https://mem0.ai). When set, plugins/mem0.py uses
    # `mem0.MemoryClient(api_key=...)`. WARNING: hosted mode sends
    # extra_prompt, prior_context, the step's output (the agent's actual
    # generated result — more likely than extra_prompt/prior_context to
    # contain secrets), AND the structured PROVENANCE metadata `store()`
    # attaches to every memory (source/project/task/role/step/category/ts —
    # see the "PROVENANCE metadata" note in plugins/mem0.py) off-machine to
    # mem0.ai — do NOT use it for sensitive projects; leave unset to keep
    # everything local via `mem0.Memory()`.
    # env: HIVEPILOT_MEM0_API_KEY
    mem0_api_key: str | None = None
    # Optional self-host mem0 config dict, passed to `Memory.from_config()`
    # (vector store / embedder / llm overrides). Only used when mem0_api_key
    # is unset. env: HIVEPILOT_MEM0_CONFIG (JSON string)
    mem0_config: dict[str, Any] | None = None
    stage_cache_enabled: bool = False  # opt-in SQLite stage memoization (L3)
    cache_backend: str = "sqlite"  # sqlite | redis (L3)
    redis_url: str | None = None  # required when cache_backend=redis (L3)
    worktree_isolation: bool = True  # run dev/role tasks inside a throwaway git worktree (env: HIVEPILOT_WORKTREE_ISOLATION)
    # Sandbox mode for autonomous developer steps with elevated permission_mode.
    # "bwrap"  — wrap the subprocess with bubblewrap FS confinement + env scrub
    #             (best-effort: falls back to unsandboxed on any error).
    # "none"   — no sandboxing (default; safe for CI environments where bwrap
    #             is unavailable or unnecessary).
    # Override via env HIVEPILOT_DEV_SANDBOX.
    dev_sandbox: str = "none"
    # Env var allowlist used when dev_sandbox="bwrap".  Mirrors the default
    # from hivepilot.utils.sandbox.DEFAULT_ALLOWLIST; override to add extra keys.
    sandbox_env_allowlist: list[str] = []  # empty = use DEFAULT_ALLOWLIST
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
