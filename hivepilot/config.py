from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Fields populated from an env var that pydantic-settings would otherwise try
# to strict-JSON-decode (see `_parse_env_list` below for the HIGH-severity
# bug this fixes). Kept as a module-level tuple so it can be reused by the
# shared `field_validator` decorator without repeating every name inline.
_ENV_LIST_FIELDS = (
    "plugins_disabled",
    "plugins_capability_policy",
    "discovery_roots",
    "api_allowed_origins",
    "secrets_allowed_dirs",
    "telegram_allowed_chat_ids",
    "ssh_options",
    "sandbox_env_allowlist",
    "dev_fallback_runners",
    "slack_allowed_channel_ids",
    "discord_allowed_guild_ids",
    "discord_allowed_channel_ids",
    "signal_allowed_numbers",
    "governance_files",
)


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
    # Phase 24b.2a — opt-in usage capture (tokens/cost/actual-model) from the
    # claude runner. Default False = BYTE-IDENTICAL current behaviour: capture()
    # invokes claude without --output-format json and returns raw stdout. When
    # True, capture() adds --output-format json, parses the JSON envelope, still
    # returns only the agent's `.result` text as the step output (unchanged),
    # and additionally records input/output tokens + self-reported cost + the
    # actual model used. Any parsing/shape/CLI failure gracefully falls back to
    # raw-stdout behaviour with null usage — this flag must never break a run.
    # env: HIVEPILOT_CLAUDE_CAPTURE_USAGE
    claude_capture_usage: bool = False
    gh_command: str = "gh"
    git_command: str = "git"
    # Auto-clone of a missing project repo (PR B): when a project's `path`
    # doesn't exist and it has an `owner_repo`, orchestrator.run_task clones
    # it from that repo before the run instead of failing deep inside a
    # runner's `subprocess.run(cwd=...)` with a raw `[Errno 2]`. "ssh"
    # matches github_service.ensure_repository's existing ssh default (and
    # the deploy-key/SSH pattern most self-hosts use); "https" requires
    # pre-authed git or a credential helper on the host -- HivePilot's
    # config-token machinery (HIVEPILOT_CONFIG_TOKEN) only authenticates the
    # config repo, not arbitrary project repos.
    # env: HIVEPILOT_PROJECT_CLONE_PROTOCOL
    project_clone_protocol: str = "ssh"
    concurrency_limit: int = 4
    interactive_default_all: bool = False
    enable_textual_ui: bool = False
    # Mirador web UI (hivepilot/webui/) — serves the pre-built React/Vite
    # bundle committed under hivepilot/webui/static/ at GET /ui. Off by
    # default; mirrors enable_textual_ui's opt-in pattern above. Also
    # requires a real build to be present (hivepilot.webui.static_available())
    # — the route returns 404 if either condition isn't met.
    # env: HIVEPILOT_ENABLE_WEBUI
    enable_webui: bool = False
    # Phase 18 — OpenTelemetry distributed tracing for pipeline/task/step
    # execution (hivepilot/observability/tracing.py). Off by default; mirrors
    # enable_webui/headroom_enabled's opt-in-only gating above. Also requires
    # the `tracing` extra (`pip install hivepilot[tracing]`) to be installed —
    # when the OTel SDK isn't importable, init_tracing() no-ops regardless of
    # this flag and get_tracer() always returns the local no-op tracer, so
    # core install (no extra) is completely unaffected either way.
    # env: HIVEPILOT_ENABLE_TRACING
    enable_tracing: bool = False
    # OTLP span exporter endpoint (e.g. http://localhost:4317 for a local
    # Jaeger/Zipkin/collector). Unset (None) lets the OTel SDK fall back to
    # reading the STANDARD `OTEL_EXPORTER_OTLP_ENDPOINT` env var natively
    # (OTel's own SDK config resolution, not pydantic-settings) — set this
    # only if you want it sourced from HivePilot's own .env instead.
    # env: HIVEPILOT_OTEL_EXPORTER_OTLP_ENDPOINT
    otel_exporter_otlp_endpoint: str | None = None
    # `service.name` resource attribute attached to every exported span.
    # env: HIVEPILOT_OTEL_SERVICE_NAME
    otel_service_name: str = "hivepilot"
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
    plugins_disabled: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Multi-directory plugin search: additional directories `_scan_local_plugins`
    # (hivepilot/plugins.py) scans for local-file plugins AFTER `base_dir/plugins`
    # (scanned first, always). Lets a deployment that overrides `base_dir` (e.g. a
    # config repo, to load its OWN `plugins/*.py`) ALSO load the engine's shipped
    # `plugins/*.py`, instead of one shadowing the other. Scanned in list order;
    # a module stem already loaded from an earlier directory (base_dir first, then
    # each entry here in order) is skipped rather than raising a collision — see
    # `_scan_local_plugins`'s dedup-by-stem, first-wins rule. A directory that
    # doesn't exist is silently skipped, not an error. Additive/opt-in: empty
    # (default) is byte-identical to pre-existing behaviour.
    # env: HIVEPILOT_PLUGINS_EXTRA_DIRS — os.pathsep-separated list of directory
    # paths (":" on POSIX, ";" on Windows), e.g.
    # HIVEPILOT_PLUGINS_EXTRA_DIRS=/opt/hivepilot/plugins:/srv/config/plugins
    # Deliberately NOT the JSON-array convention `plugins_disabled` above uses —
    # a directory list reads more naturally PATH/PYTHONPATH-style. `NoDecode`
    # stops pydantic-settings' default complex-type JSON decoding from ever
    # seeing this env value, so the "before" validator below always receives
    # the raw string.
    plugins_extra_dirs: Annotated[list[Path], NoDecode] = Field(default_factory=list)

    @field_validator("plugins_extra_dirs", mode="before")
    @classmethod
    def _parse_plugins_extra_dirs(cls, v: object) -> object:
        # Unset/empty -> [] (never a single Path("")). A list already (e.g.
        # constructed directly in Python/tests, or reload of an already-parsed
        # value) passes through unchanged for pydantic to coerce element-wise.
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [part for part in v.split(os.pathsep) if part]
        return v

    # HIGH-severity fix: pydantic-settings strict-JSON-decodes every
    # `list[...]` field's env value BEFORE any validator runs, unless the
    # field is `Annotated[..., NoDecode]` (see `plugins_extra_dirs` above).
    # Every field in `_ENV_LIST_FIELDS` is one of these `list[...]` fields —
    # without `NoDecode` + this validator, a plain value
    # (HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS=123456), a CSV value
    # ("123,456"), or an EMPTY value ("") is not valid JSON and raises
    # `SettingsError` at `Settings()` construction — which happens at
    # IMPORT of `hivepilot.config` (see `settings = Settings()` at module
    # bottom), bricking the entire CLI/app before a single command runs.
    @field_validator(*_ENV_LIST_FIELDS, mode="before")
    @classmethod
    def _parse_env_list(cls, v: object) -> object:
        # A list already (constructed directly in Python/tests, or a value
        # some other source already parsed) passes through unchanged for
        # pydantic to coerce element-wise.
        if not isinstance(v, str):
            return v
        s = v.strip()
        if not s:
            return []
        if s.startswith("["):
            # JSON-array convention (backward-compat with every existing
            # deployment/test using it, e.g. plugins_disabled today).
            try:
                return json.loads(s)
            except (json.JSONDecodeError, ValueError):
                pass  # fall through to CSV parsing below
        # Plain single value or comma-separated list: split, strip each
        # element, drop empties. Pydantic then coerces each string element
        # to the field's declared type (e.g. "123" -> 123 for list[int]),
        # raising a clean ValidationError on a genuinely non-coercible
        # element rather than crashing the whole Settings() construction.
        return [part.strip() for part in s.split(",") if part.strip()]

    # Phase 26b Approach A — URL of a JSON "plugin index" document (name/
    # description/author/homepage/install-hint/version/checksum) that
    # `plugins search`/`info` fetch and display (hivepilot/services/
    # plugin_index.py). METADATA ONLY: the index is never used to download
    # or execute plugin code — installation stays on the operator's own
    # pip/git path (see docs/PLUGINS.md "Trust model"). Empty (default)
    # means no index is configured; `plugins search`/`info` then fail fast
    # with a friendly message instead of making any network call.
    # env: HIVEPILOT_PLUGINS_INDEX_URL
    plugins_index_url: str = ""
    # Phase 26b — opt-in hot-reload of local-file plugins without a process
    # restart. When True, SchedulerDaemon polls `plugins/*.py` mtimes each
    # tick (`PluginManager.plugins_changed_on_disk()`) and, on a change,
    # atomically re-scans+re-registers (`PluginManager.reload()`) via its
    # OWN dedicated, long-lived PluginManager -- NOT the ad-hoc one each
    # per-schedule/per-deferred-row `Orchestrator()` construction builds
    # fresh (see `hivepilot/services/scheduler_daemon.py`). SIGHUP always
    # forces an immediate reload attempt when this is enabled (a no-op,
    # logged, when it is not). Default OFF: hot-reload mutates
    # process-global RUNNER_MAP/NOTIFIER_MAP/SECRETS_MAP state at runtime,
    # a behavior change an operator should opt into explicitly.
    # env: HIVEPILOT_PLUGINS_HOT_RELOAD
    plugins_hot_reload: bool = False
    # Phase 14c (#249) — opt-in hot-reload of roles.yaml on EVERY scheduler
    # tick, mirroring `plugins_hot_reload`'s shape/rationale above but for
    # roles instead of plugins. When True, SchedulerDaemon calls
    # `roles.refresh_roles()` once per tick (cheap: a YAML read + Pydantic
    # validation, fail-closed to the previous roles on any error -- see
    # `hivepilot/roles.py`'s `refresh_roles()` docstring). Independent of
    # this flag, roles ARE always reloadable on demand via `hivepilot
    # reload` / `POST /v1/admin/reload` / sending the daemon `SIGHUP` -- this
    # setting only gates the AUTOMATIC per-tick reload. Default OFF:
    # byte-identical scheduler behavior until an operator opts in.
    # env: HIVEPILOT_CONFIG_HOT_RELOAD
    config_hot_reload: bool = False
    # Phase 26b — operator allow-list for the plugin `capabilities` manifest
    # (hivepilot/plugin_capabilities.py). A plugin MAY declare
    # `register()["capabilities"] = [...]` from the closed
    # `PLUGIN_CAPABILITIES` vocabulary (network/filesystem/subprocess/
    # secrets_access/env); this setting is the set of tokens the operator
    # ALLOWS a plugin to declare. Default `[]` = fail-closed deny-every-
    # declared-capability: ANY plugin declaring ANY capability is denied at
    # load until the operator explicitly opts that token in here. A plugin
    # that declares NO capabilities at all is completely unaffected by this
    # setting, regardless of its value — purely additive/backward-compatible.
    # This is an ADVISORY admission gate, not runtime sandboxing — see
    # docs/PLUGINS.md "Capability manifest & policy gate".
    # env: HIVEPILOT_PLUGINS_CAPABILITY_POLICY — JSON array, same convention
    # as plugins_disabled above.
    plugins_capability_policy: Annotated[list[str], NoDecode] = Field(default_factory=list)
    discovery_roots: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["~/dev"])
    api_host: str = "127.0.0.1"
    api_port: int = 8045
    api_root_path: str = ""  # set to "/hivepilot" when behind a path-prefix proxy
    api_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    chatops_token: str | None = None
    secrets_allowed_dirs: Annotated[list[str], NoDecode] = Field(default_factory=list)
    token_ttl_days: int | None = None
    log_to_file: bool = False
    api_max_body_size: int = 1_048_576  # 1 MB
    http_proxy: str | None = None
    https_proxy: str | None = None
    no_proxy: str | None = None
    config_repo: str | None = None
    config_branch: str = "main"
    # Auth token for a PRIVATE https config_repo (fine-grained/classic PAT with
    # Contents:read [+ write, only if `hivepilot config push` is used] on the
    # config repo). env: HIVEPILOT_CONFIG_TOKEN. Injected by config_service as
    # a TRANSIENT per-invocation `http.extraheader` (via GIT_CONFIG_* env
    # vars) — never written to `.git/config`, never embedded in the repo URL,
    # never logged. https-only: ssh:// / git@ config_repo URLs ignore this
    # setting entirely and authenticate via the host's own SSH key/agent.
    config_token: str | None = None
    # Auto-load `plugins/*.py` shipped INSIDE the config repo clone (e.g. a
    # vendored `vendored_skills.py` under `<config_repo>/plugins/`) without
    # requiring a manual `HIVEPILOT_PLUGINS_EXTRA_DIRS` override -- see
    # `hivepilot.plugins._config_repo_plugins_dir`. Default True: an
    # operator who points HivePilot at a config repo generally expects its
    # bundled plugins/skills to just work. env:
    # HIVEPILOT_CONFIG_REPO_LOAD_PLUGINS -- a cautious operator who does NOT
    # want the config repo to also be a code-execution surface (a plugin is
    # arbitrary Python run in-process) can set this to False; plugins remain
    # subject to the normal `plugins_enabled`/`plugins_disabled`/capability-
    # policy gates either way. No-op when `config_repo` itself is unset.
    config_repo_load_plugins: bool = True
    domain: str | None = None  # public domain used by caddy + webhook auto-registration
    telegram_bot_token: str | None = None
    telegram_allowed_chat_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
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
    ssh_options: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )  # extra ssh -o options for remote agents
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
    # 1Password direct service-account (non-Connect) mode (plugins/onepassword.py):
    # when `op_connect_host` is UNSET but `op_service_account_token` IS set, the
    # plugin resolves `op://vault/item/field` directly against api.1password.com
    # via the official async `onepassword-sdk` package (imported lazily; never
    # installed by the plugin). These two identify the integration to the SDK's
    # `Client.authenticate(...)` — cosmetic labels, never secrets.
    # env: HIVEPILOT_OP_INTEGRATION_NAME / HIVEPILOT_OP_INTEGRATION_VERSION
    op_integration_name: str = "HivePilot"
    op_integration_version: str = "1.0.0"
    # ---- KMS envelope-encryption secrets provider (plugins/kms.py) ----
    # Config for the first-party `kms` secrets-backend plugin: decrypts an
    # operator-provided ciphertext at runtime via their OWN cloud KMS (AWS KMS /
    # GCP Cloud KMS / Azure Key Vault). Two spec modes (auto-detected by the
    # ref.spec keys present): DIRECT (`ciphertext` — a KMS-encrypted blob ≤4KB
    # decrypted straight by the provider) and ENVELOPE (`encrypted_data_key` +
    # `ciphertext` + `iv` (+`tag`) — KMS-decrypt a small data key, then
    # AES-256-GCM-decrypt a local ciphertext with it). All provider SDKs and
    # `cryptography` are imported lazily; a missing lib/provider raises a clear
    # RuntimeError naming ONLY the lib/provider — never a plaintext or data key.
    # Provider — "aws" | "gcp" | "azure"; a ref.spec `provider` overrides this
    # per-secret. env: HIVEPILOT_KMS_PROVIDER
    kms_provider: str | None = None
    # Fully-qualified KMS key id/resource name used to decrypt the data key
    # (required for gcp/azure; AWS direct-mode embeds it in the ciphertext). A
    # ref.spec `key_id` overrides this per-secret. env: HIVEPILOT_KMS_KEY_ID
    kms_key_id: str | None = None
    # ---- Secret TTL cache / rotation (hivepilot/services/secrets_service.py) ----
    # Opt-in in-memory, process-local TTL cache for resolved secret values so a
    # run doesn't re-hit the provider for every step, and rotated secrets are
    # picked up after expiry. 0 (default) = DISABLED = today's always-live
    # behaviour (backend called every resolution). Cached plaintext is held only
    # in memory, NEVER persisted to disk, TTL-bounded, and flushed by
    # `hivepilot secrets cache-clear`. env: HIVEPILOT_SECRETS_CACHE_TTL_SECONDS
    secrets_cache_ttl_seconds: int = 0
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
    # Per-plugin enable flags for the six always-on bundled plugins. UNLIKE
    # headroom_enabled/mem0_enabled above (which default False — opt-IN,
    # dormant), these six default True — opt-OUT: current behavior is
    # byte-identical by default, but each plugin can now be toggled off
    # individually via its own flag. Each register() early-returns `{}`
    # (contributing nothing) when its flag is False.
    # env: HIVEPILOT_HERDR_ENABLED / _INFISICAL_ / _OBSIDIAN_ /
    #      _ONEPASSWORD_ / _RTK_ / _SAMPLE_ENABLED
    herdr_enabled: bool = True
    infisical_enabled: bool = True
    # KMS envelope-encryption secrets backend (plugins/kms.py) — opt-OUT, like
    # infisical_enabled/onepassword_enabled: register() returns {} when False.
    # env: HIVEPILOT_KMS_ENABLED
    kms_enabled: bool = True
    obsidian_enabled: bool = True
    onepassword_enabled: bool = True
    rtk_enabled: bool = True
    sample_enabled: bool = False
    # Demo skill plugin (plugins/sample_skill.py) — opt-IN, dormant by
    # default. env: HIVEPILOT_SAMPLE_SKILL_ENABLED
    sample_skill_enabled: bool = False
    # Example `graph_sources` plugin capability (plugins/example_graph_source.py,
    # Mirador Graph View PRD Sprint 4) — opt-IN, dormant by default, same
    # pattern as sample/sample_skill above. Required by
    # tests/test_gating_conformance.py::TestAllPluginStemsHaveEnabledFlag
    # (every plugins/*.py stem must have a matching `<stem>_enabled` flag).
    # env: HIVEPILOT_EXAMPLE_GRAPH_SOURCE_ENABLED
    example_graph_source_enabled: bool = False
    # Two more plugin-contributed `graph_sources` (Mirador GraphSources
    # plugins sprint) -- opt-IN, dormant by default, same pattern as
    # example_graph_source_enabled above. `drift` renders the IaC drift-scan
    # history (plugins/drift_graph_source.py); `secrets-trust` renders which
    # secret NAMES resolve via which provider/backend, never a value
    # (plugins/secrets_trust_graph_source.py). Both required by
    # tests/test_gating_conformance.py::TestAllPluginStemsHaveEnabledFlag.
    # env: HIVEPILOT_DRIFT_GRAPH_SOURCE_ENABLED / _SECRETS_TRUST_GRAPH_SOURCE_ENABLED
    drift_graph_source_enabled: bool = False
    secrets_trust_graph_source_enabled: bool = False
    # Two `panel`-type plugins (Mirador Panels sprint) -- opt-IN, dormant by
    # default, same pattern as drift_graph_source_enabled above. `drift_panel`
    # (plugins/drift_panel.py) is a counts-only IaC drift-scan history panel;
    # `autopilot_panel` (plugins/autopilot_panel.py) is a queue-status +
    # budget burn-down + awaiting-human panel. Both required by
    # tests/test_gating_conformance.py::TestAllPluginStemsHaveEnabledFlag.
    # env: HIVEPILOT_DRIFT_PANEL_ENABLED / _AUTOPILOT_PANEL_ENABLED
    drift_panel_enabled: bool = False
    autopilot_panel_enabled: bool = False
    # Two more opt-in `skill`-type plugins -- dormant by default, same
    # pattern as sample_skill_enabled above. `shadcn` (plugins/shadcn.py)
    # is a Mirador web accelerator (shadcn/ui + Tailwind conventions for
    # web/); `improve` (plugins/improve.py) is a read-only auditor whose
    # findings feed the review/lessons loop. Both required by
    # tests/test_gating_conformance.py::TestAllPluginStemsHaveEnabledFlag.
    # env: HIVEPILOT_SHADCN_ENABLED / _IMPROVE_ENABLED
    shadcn_enabled: bool = False
    improve_enabled: bool = False
    # Built-in agent runners are individually disable-able (plugin-arch-overhaul
    # Sprint 01). Default True — turning one off removes it from RUNNER_MAP via
    # the _BUILTIN_RUNNERS gate in hivepilot/registry.py. Infra runners
    # (shell/terraform/kubectl/…) stay unconditional and get no flag.
    # env: HIVEPILOT_CLAUDE_ENABLED / _VIBE_ENABLED / _OPENROUTER_ENABLED
    claude_enabled: bool = True
    vibe_enabled: bool = True
    openrouter_enabled: bool = True
    # Sprint 2 (runner-defaults-plugins-mode PRD): gemini/opencode/ollama
    # moved OUT of hivepilot.registry._BUILTIN_RUNNERS and into default-on,
    # PATH-gated plugins (plugins/gemini.py / plugins/opencode.py /
    # plugins/ollama.py) — same opt-OUT pattern as the six flags above. Each
    # plugin's register() ALSO checks `shutil.which(<binary>)`, so a config
    # still referencing `kind: gemini`/`opencode`/`ollama` keeps resolving
    # exactly as before whenever both the flag is True (default) and the
    # binary is on PATH.
    # env: HIVEPILOT_GEMINI_ENABLED / _OPENCODE_ENABLED / _OLLAMA_ENABLED
    gemini_enabled: bool = True
    opencode_enabled: bool = True
    ollama_enabled: bool = True
    # codex-cursor-plugins migration: codex/cursor moved OUT of
    # hivepilot.registry._BUILTIN_RUNNERS and into default-on, PATH-gated
    # plugins (plugins/codex.py / plugins/cursor.py) — same opt-OUT +
    # shutil.which gating pattern as gemini/opencode/ollama above. `codex`
    # stays in hivepilot.services.agent_checks.MANDATORY_AGENTS regardless
    # (that check scans PATH directly, unaffected by builtin-vs-plugin).
    # env: HIVEPILOT_CODEX_ENABLED / _CURSOR_ENABLED
    codex_enabled: bool = True
    cursor_enabled: bool = True
    # Sprint 3 (runner-defaults-plugins-mode PRD): three brand-new agent
    # kinds (never previously built-in) added directly as default-on,
    # PATH-gated plugins (plugins/pi.py / plugins/qwen_code.py /
    # plugins/kimi_cli.py) — same opt-OUT + shutil.which gating pattern as
    # the three flags above.
    # env: HIVEPILOT_PI_ENABLED / _QWEN_CODE_ENABLED / _KIMI_CLI_ENABLED
    pi_enabled: bool = True
    qwen_code_enabled: bool = True
    kimi_cli_enabled: bool = True
    # S3 (follow-on to runner-defaults-plugins-mode PRD): brand-new
    # `kind: "antigravity"` agent runner (Google Antigravity CLI, binary
    # `agy`) added directly as a default-on, PATH-gated plugin
    # (plugins/antigravity.py) — same opt-OUT + shutil.which gating pattern
    # as pi_enabled/qwen_code_enabled/kimi_cli_enabled above.
    # env: HIVEPILOT_ANTIGRAVITY_ENABLED
    antigravity_enabled: bool = True
    # Phase 25 — brand-new `kind: "hugo"` static-site-generator runner added
    # directly as a default-on, PATH-gated plugin (plugins/hugo.py) — same
    # opt-OUT + shutil.which gating pattern as rtk_enabled/pi_enabled/etc.
    # env: HIVEPILOT_HUGO_ENABLED
    hugo_enabled: bool = True
    # S4 — brand-new `kind: "gh"` COMMAND runner (GitHub CLI, binary
    # `gh`) added as a self-contained, default-on, PATH-gated plugin
    # (plugins/gh.py) — same opt-OUT + shutil.which gating pattern as
    # rtk_enabled/hugo_enabled/tmux_enabled. Unlike the agent-kind plugins
    # above (antigravity/kimi-cli/qwen-code/…), `gh` is a plain command
    # runner, never registered in AGENT_RUNNER_KINDS /
    # _OPTIONAL_AGENT_PLUGIN_KINDS.
    # env: HIVEPILOT_GH_ENABLED
    gh_enabled: bool = True
    # Sprint 02 (plugin-arch-overhaul PRD) — obsidian "brain" recall sub-flags.
    # `obsidian_recall_enabled` gates the NEW `before_step` (`recall`) /
    # `after_step` (`store`) context-provider behavior independently of
    # `obsidian_enabled` (which still gates the whole plugin, including the
    # pre-existing notifier/journal hooks). Both default True (opt-out),
    # matching the six-flag pattern above; recall additionally requires a
    # configured+present `obsidian_vault` regardless of this flag.
    # env: HIVEPILOT_OBSIDIAN_RECALL_ENABLED
    obsidian_recall_enabled: bool = True
    # Hard byte cap on the vault-note excerpt block `recall` injects into
    # `RunnerPayload.metadata["extra_prompt"]` per step — keeps a large vault
    # from ballooning the rendered prompt. Enforced strictly on the injected
    # content only (pre-existing `extra_prompt` content, e.g. from mem0, is
    # never truncated). env: HIVEPILOT_OBSIDIAN_RECALL_MAX_BYTES
    obsidian_recall_max_bytes: int = 4000
    # Sprint 03 (plugin-arch-overhaul PRD) — brand-new `kind: "tmux"`
    # execution-wrapper runner (runs each step inside a dedicated tmux
    # session for live attach/observe) added directly as a default-on,
    # PATH-gated plugin (plugins/tmux.py) — same opt-OUT + shutil.which
    # gating pattern as rtk_enabled/herdr_enabled/hugo_enabled.
    # env: HIVEPILOT_TMUX_ENABLED
    tmux_enabled: bool = True
    # Sprint 04 (plugin-arch-overhaul) — bitwarden/vaultwarden secrets backends.
    # Two first-party `secrets` provider plugins (plugins/bitwarden.py /
    # plugins/vaultwarden.py) that shell out to the official Bitwarden `bw` CLI
    # (an optional EXTERNAL tool, never a Python dependency). Same opt-OUT +
    # fail-closed pattern as infisical/onepassword: resolve() raises naming ONLY
    # the item + provider (never the secret value or the BW_SESSION token).
    # `bitwarden` targets the Bitwarden cloud endpoint; `vaultwarden` targets a
    # self-hosted Bitwarden-compatible server via `vaultwarden_server_url`
    # (`bw config server <url>`). Session is read from the BW_SESSION env var.
    # env: HIVEPILOT_BITWARDEN_ENABLED / _VAULTWARDEN_ENABLED /
    #      _VAULTWARDEN_SERVER_URL
    bitwarden_enabled: bool = True
    vaultwarden_enabled: bool = True
    vaultwarden_server_url: str | None = None
    # Phase 24b.2b — operator-supplied price-map override, merged OVER
    # `hivepilot.services.pricing.DEFAULT_PRICE_MAP` (per-model merge, not a
    # wholesale replacement — see `pricing._effective_price_map`). Shape:
    # {"<model>": {"input": <usd/Mtok>, "output": <usd/Mtok>}}. Unset (None)
    # means the built-in defaults apply unmodified. Used as a fallback cost
    # estimate only when a step has no self-reported `cost_usd`.
    # env: HIVEPILOT_LLM_PRICE_MAP (JSON string)
    llm_price_map: dict[str, Any] | None = None
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
    sandbox_env_allowlist: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )  # empty = use DEFAULT_ALLOWLIST
    claude_max_concurrency: int = (
        1  # max concurrent claude steps (env: HIVEPILOT_CLAUDE_MAX_CONCURRENCY)
    )
    dev_fallback_runners: Annotated[list[str], NoDecode] = Field(
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

    # ---- Debate synthesis judge (Debate Judge & Consensus PRD, Sprint 1) ----
    # Opt-in LLM arbiter that synthesizes a debate's model positions into a
    # real decision + confidence, replacing the templated `decision=` string
    # `Orchestrator._run_debate_body` builds today. Defaults False — the
    # flags-off path is byte-identical to pre-Sprint-1 behaviour (templated
    # decision, majority-stance fallback in DebateService untouched).
    # env: HIVEPILOT_ENABLE_DEBATE_JUDGE
    enable_debate_judge: bool = False
    # Runner kind used for the ONE judge `capture_definition` call (see
    # `Orchestrator._adjudicate`). env: HIVEPILOT_JUDGE_RUNNER
    judge_runner: str = "claude"
    # Model passed to the judge RunnerDefinition; None lets the runner use its
    # own default. env: HIVEPILOT_JUDGE_MODEL
    judge_model: str | None = None

    # ---- Independent challenge arbiter (Debate Judge & Consensus PRD, Sprint 2) ----
    # Opt-in THIRD-party judge that adjudicates a challenge/rebuttal pair
    # instead of letting the challenger self-grade the resolution. Defaults
    # False — the flags-off path is byte-identical to pre-Sprint-2 behaviour
    # (challenger's own runner is re-invoked for the ACCEPT/MAINTAIN check,
    # see `Orchestrator._run_rebuttal_round`).
    # env: HIVEPILOT_ENABLE_CHALLENGE_ARBITER
    enable_challenge_arbiter: bool = False
    # Minimum verdict confidence, in [0.0, 1.0], required to accept an
    # arbiter ACCEPT verdict without escalating to a human. Any verdict with
    # `decision is None`, `confidence is None`, `confidence` below this
    # threshold, or `decision != "ACCEPT"` escalates via
    # `notification_service.stream_needs_human` (fail TOWARD human review,
    # never fail open). env: HIVEPILOT_JUDGE_CONFIDENCE_THRESHOLD
    #
    # Also consumed by `git_service.is_blocking`/`perform_git_actions`
    # (Debate Judge & Consensus PRD, Sprint 3) as the SAME fail-closed
    # threshold for the promote_pr/merge_pr PR gate: only active when
    # `enable_debate_judge` or `enable_challenge_arbiter` is True (see
    # `Orchestrator._governing_verdict`/`_register_verdict`).
    judge_confidence_threshold: float = 0.5

    @field_validator("judge_confidence_threshold")
    @classmethod
    def _validate_judge_confidence_threshold(cls, v: float) -> float:
        # Fail closed on a misconfigured floor threshold. This mirrors the
        # per-pipeline `DebateConfig.confidence_threshold` guard
        # (models.py / pipeline_service.py): a floor value of 0 (or negative,
        # >1, NaN, inf) supplied via HIVEPILOT_JUDGE_CONFIDENCE_THRESHOLD would
        # otherwise reach `git_service.is_blocking(verdict, 0)` and approve any
        # finite-confidence ACCEPT -- a fail-OPEN gate. Reject at startup so a
        # bad env value stops the process instead of silently disabling the
        # verdict->PR gate. Absent -> the 0.5 default (validated as in-range).
        if not math.isfinite(v) or not (0 < v <= 1):
            raise ValueError(
                "judge_confidence_threshold (env HIVEPILOT_JUDGE_CONFIDENCE_THRESHOLD) "
                f"must be a finite number in (0, 1], got {v!r}"
            )
        return v

    # ---- Auto-Learning Lessons Loop PRD, Sprint 2 (opt-in distillation) ----
    # Opt-in, ONE-LLM-call-per-run distillation of the run's verdicts +
    # interactions + outcomes into structured, scored CANDIDATE lessons
    # (see `lessons_service.distill_lessons`, wired at pipeline end in
    # `Orchestrator._run_task_body`, near where per-project
    # `knowledge_service.append_feedback` already fires). Defaults False --
    # the flags-off path is byte-identical to pre-Sprint-2 behaviour (no
    # extra LLM call, no `lessons` rows written).
    # env: HIVEPILOT_ENABLE_LESSON_DISTILLATION
    enable_lesson_distillation: bool = False
    # Runner kind used for the ONE distiller `capture_definition` call.
    # env: HIVEPILOT_LESSON_DISTILL_RUNNER
    lesson_distill_runner: str = "claude"
    # Model passed to the distiller RunnerDefinition; None lets the runner
    # use its own default. env: HIVEPILOT_LESSON_DISTILL_MODEL
    lesson_distill_model: str | None = None
    # Minimum score, in (0.0, 1.0], a lesson must reach before it is
    # eligible for retrieval/injection into a future run (Sprint 3 computes
    # the real score from outcome signal -- Sprint 2 never reads this at
    # distillation time, only persists candidates at `validated=False`).
    # env: HIVEPILOT_LESSON_MIN_SCORE
    lesson_min_score: float = 0.5
    # Max number of validated lessons injected into a future run's context
    # (Sprint 3/4's retrieval + injection path).
    # env: HIVEPILOT_LESSON_INJECT_LIMIT
    lesson_inject_limit: int = 5

    # ---- Auto-Learning Lessons Loop PRD, Sprint 4 (opt-in semantic rank) --
    # Opt-in semantic re-ranking of ALREADY-VALIDATED lessons at retrieval
    # time (`lessons_service.retrieve_lessons(..., semantic=True)`) using
    # the SAME optional `hivepilot[langchain]` embedding extra
    # `knowledge_service._embedding_context` already uses -- lazy-imported,
    # never a hard dependency. Defaults False -- the core lessons loop
    # (distill/validate/inject) stays fully dependency-free with this flag
    # off, byte-identical to Sprint 3. Even when True, a missing extra or
    # any embedding-time error falls back to the plain SQLite score+recency
    # ranking (`state_service.list_ranked_lessons`) -- this flag can never
    # turn a working retrieval into a crash.
    # env: HIVEPILOT_ENABLE_SEMANTIC_LESSON_RETRIEVAL
    enable_semantic_lesson_retrieval: bool = False

    @field_validator("lesson_min_score")
    @classmethod
    def _validate_lesson_min_score(cls, v: float) -> float:
        # Fail closed, same rationale/shape as
        # `_validate_judge_confidence_threshold` above: a `lesson_min_score`
        # of 0 (or negative, >1, NaN, inf) would let ANY distilled candidate
        # (however weak) pass the future validation gate -- a fail-OPEN
        # lesson-quality floor. Reject at startup instead of silently
        # admitting garbage lessons.
        if not math.isfinite(v) or not (0 < v <= 1):
            raise ValueError(
                "lesson_min_score (env HIVEPILOT_LESSON_MIN_SCORE) must be a "
                f"finite number in (0, 1], got {v!r}"
            )
        return v

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
    slack_allowed_channel_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    slack_notification_channel_id: str | None = None  # proactive notifications
    discord_bot_token: str | None = None
    discord_public_key: str | None = None  # Ed25519 public key for HTTP interactions
    discord_allowed_guild_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    discord_allowed_channel_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    discord_notification_channel_id: int | None = None  # proactive notifications
    # Phase 23e — Signal bot (dual-mode). Signal has no cloud bot API / inbound
    # webhook (E2E P2P); the bot is a dedicated phone number driven either by
    # the `signal-cli` binary (PATH-gated, optional external dependency) or a
    # `signal-cli-rest-api` HTTP wrapper. See hivepilot/services/signal_bot.py.
    signal_number: str | None = None  # bot's own E.164 number, e.g. +15551234567
    signal_allowed_numbers: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )  # E.164 whitelist
    signal_cli_path: str = "signal-cli"  # binary name/path on PATH
    signal_rest_url: str | None = None  # base URL of a signal-cli-rest-api instance
    signal_notification_number: str | None = None  # proactive notifications
    signal_receive_mode: str = "cli"  # "cli" or "rest"
    linear_api_key: str | None = None
    linear_team_id: str | None = None  # default team for issue creation
    linear_default_project_id: str | None = None  # default project
    linear_webhook_secret: str | None = None  # HMAC secret for webhook verification
    notion_token: str | None = None
    notion_runs_database_id: str | None = None  # database where run logs are written
    obsidian_vault: Path = Path("obsidian-vault")

    # Mirador Graph View PRD, Sprint 2: local host filesystem root the
    # built-in `skills` graph source (`hivepilot/graph_sources/
    # skills_source.py`) scans for `SKILL.md`/hooks/commands/agents. `None`
    # (the default) means the source degrades to an empty graph -- never a
    # crash, never an implicit path. Unlike `obsidian_vault` above (a
    # relative default that always resolves to something), this has NO
    # non-None default: an admin must opt in to which local directory this
    # host-only scan is allowed to read.
    graph_skills_scan_path: Path | None = None

    # Governance repository root (e.g. /path/to/shared-governance-repo or https URL)
    # Deployment-specific; leave None to disable governance file injection.
    governance_repo: str | None = Field(
        default=None,
        validation_alias="HIVEPILOT_GOVERNANCE_REPO",
    )

    # Governance file names (relative to governance_repo root) to inject into prompts.
    governance_files: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "CLAUDE.md",
            "AGENTS.md",
            "AGENT-GOVERNANCE.md",
            ".cursorrules",
            ".windsurfrules",
            "GEMINI.md",
        ],
    )

    # Optional path to a Unicode-capable TTF font (e.g. DejaVu Sans) used by
    # the PDF analytics export (`api_service._pdf_response`) so non-latin
    # project/task/provider names render correctly instead of degrading to
    # `?`. `None` (the default) means the exporter probes a small list of
    # common system font paths instead; if none of those exist either, PDF
    # export gracefully falls back to the latin-1-only core font it already
    # used before this option existed. env: HIVEPILOT_PDF_FONT_PATH
    pdf_font_path: str | None = None

    # ---- `hivepilot self-update` defaults ----
    # HivePilot is NOT published on PyPI -- it installs from git. These are
    # the defaults `hivepilot self-update` builds its pip spec from
    # (`hivepilot[<update_extras>] @ git+<update_repo>@<update_ref>`); every
    # one is overridable per-invocation via the matching CLI flag
    # (--repo/--ref/--extras). An operator can pin a tag/sha here via env
    # (HIVEPILOT_UPDATE_REPO/_REF/_EXTRAS) for reproducible prod updates.
    update_repo: str = "https://github.com/jsoyer/HivePilot.git"
    update_ref: str = "main"
    # Base prod extras. Must include everything this deployment actually
    # runs -- e.g. pass --extras "api,notifications,webui" (or set
    # HIVEPILOT_UPDATE_EXTRAS) to keep the web UI, containers, etc.
    update_extras: str = "api,notifications"

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
