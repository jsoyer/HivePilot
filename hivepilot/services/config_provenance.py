"""Read-only introspection of resolved Settings values with provenance.

Powers `hivepilot config get <key>` and `hivepilot config list` (Sprint 2 of
the config-edit-commands PRD): for each `Settings` field, resolve where its
value actually comes from — replicating the same XDG -> config_repo ->
base_dir tier walk `Settings.resolve_config_path` uses, but reporting *which*
tier produced it (rank + source file) instead of only the final path — and
mask secret-typed fields before they are ever rendered.

This module never mutates `hivepilot.config` state; it only reads from the
`Settings` instance it is given (or the process-wide `settings` singleton).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hivepilot.config import Settings
from hivepilot.config import settings as _default_settings

# Substrings that mark a Settings field name as secret-typed, matched
# case-insensitively (e.g. "chatops_token", "slack_signing_secret",
# "linear_api_key").
_SECRET_NAME_SUBSTRINGS: tuple[str, ...] = ("token", "secret", "password", "api_key", "key")

# Settings whose *value* can embed credentials even though the field name
# doesn't match _SECRET_NAME_SUBSTRINGS — e.g. a Postgres/Redis DSN carries a
# password inside the URL. Treated as secret-typed unconditionally.
_KNOWN_CREDENTIAL_FIELDS: frozenset[str] = frozenset({"database_url", "redis_url"})

# Settings fields resolved through the XDG -> config_repo -> base_dir chain.
# Mirrors every real `settings.resolve_config_path(settings.<field>)` call
# site in the codebase (project_service, schedule_service, token_service,
# profile_service, policy_service, roles.py): each one is a `*_file` field.
# Directories (prompts_dir, runs_dir, logs_dir, obsidian_vault, ...) are not
# resolved per-file through this chain, so they stay rank 0.
_FILE_BACKED_SUFFIX = "_file"

REDACTED = "REDACTED"


@dataclass(frozen=True)
class Provenance:
    """A resolved Settings value plus where it came from.

    xdg_rank: 1=XDG override, 2=config_repo, 3=base_dir fallback,
              0=default/env (not a file-backed setting).
    """

    value: Any
    source_path: Path | None
    xdg_rank: int
    redacted: bool


def is_secret_field(name: str) -> bool:
    """True if *name* names a secret-typed Settings field that must never be
    echoed raw."""
    if name in _KNOWN_CREDENTIAL_FIELDS:
        return True
    lowered = name.lower()
    return any(needle in lowered for needle in _SECRET_NAME_SUBSTRINGS)


def _is_file_backed(name: str) -> bool:
    return name.endswith(_FILE_BACKED_SUFFIX)


def _walk_provenance(cfg: Settings, filename: Path) -> tuple[Path | None, int]:
    """Replicate `Settings.resolve_config_path`'s tier walk, reporting which
    tier produced the file instead of only the final path."""
    xdg_candidate = cfg.xdg_config_home / filename
    if xdg_candidate.exists():
        return xdg_candidate, 1

    local_repo = cfg._config_repo_local_path()
    if local_repo is not None:
        repo_candidate = local_repo / filename
        if repo_candidate.exists():
            return repo_candidate, 2

    return cfg.resolve_path(filename), 3


def resolve_with_provenance(key: str, cfg: Settings | None = None) -> Provenance:
    """Resolve *key* to its value plus provenance (source file + XDG rank),
    redacting secrets.

    Raises `KeyError` if *key* is not a valid `Settings` field.
    """
    settings_obj = cfg if cfg is not None else _default_settings
    if key not in type(settings_obj).model_fields:
        raise KeyError(key)

    raw_value = getattr(settings_obj, key)
    secret = is_secret_field(key)

    source_path: Path | None = None
    xdg_rank = 0
    if _is_file_backed(key) and isinstance(raw_value, Path):
        source_path, xdg_rank = _walk_provenance(settings_obj, raw_value)

    value: Any = REDACTED if secret else raw_value
    return Provenance(value=value, source_path=source_path, xdg_rank=xdg_rank, redacted=secret)


def all_keys(cfg: Settings | None = None) -> list[str]:
    """All valid `Settings` field names, in declaration order."""
    settings_obj = cfg if cfg is not None else _default_settings
    return list(type(settings_obj).model_fields.keys())


# ---------------------------------------------------------------------------
# Value-based masking registry.
#
# `resolve_with_provenance` masks secret-typed *config fields* by NAME (→
# REDACTED). Resolved `${secret:NAME}` reference values, by contrast, are
# dynamic strings whose names are not known ahead of time — so they are masked
# by VALUE: every value handed to `register_secret_value` is replaced with the
# SAME `REDACTED` sentinel wherever `redact_text`/`redact_value` is applied.
# Keeping this in `config_provenance` means the whole codebase shares one
# masking vocabulary and one redaction entry point.
#
# What is actually protected, concretely:
#   * The structlog processor (hivepilot.utils.logging) redacts every log
#     event field, recursively (see `redact_value`).
#   * The chosen persistent/outbound-sink choke points redact before writing:
#       - state_service.record_step / complete_run (the `detail` string) and
#         record_interaction (the `summary` string) — SQLite `steps.detail`,
#         `runs.detail`, `interactions.summary`.
#       - utils.io.write_summary (the whole `summary` dict, recursively) and
#         services.artifact_service.ArtifactManager.write_file/write_json
#         (covers the run's summary.json/results.json artifacts).
#       - services.obsidian_service.ObsidianService._emit (the fully-rendered
#         note content, for EVERY vault write — write_note/append_daily/
#         write_adr and any direct caller, e.g. the commits_vault
#         documentation changelog note built straight from `stage_output`).
#         hivepilot.pipelines.write_stage_artifact also redacts its `output`
#         before calling write_note — harmless double coverage (idempotent).
#       - services.notification_service.send_notification (outbound Slack/
#         Discord/Telegram messages), stream_agent_turn (the live Telegram
#         agent-turn stream — covers stream_challenge/stream_rebuttal/
#         stream_resolved/stream_needs_human/stream_agent_request/
#         stream_agent_answer, which all funnel through it), and
#         send_approval_keyboard (the `details` checkpoint-approval DM, often
#         built from `prior_chunks` agent output via
#         Orchestrator._build_checkpoint_details).
#       - services.knowledge_service.append_feedback (the vault feedback log).
#       - Orchestrator._run_task_body / Orchestrator.run_approved (the
#         `RunResult.detail` returned to CALLERS — Phase 10c): unlike the
#         sinks above, `RunResult.detail` is consumed directly by cli.py
#         (`typer.echo(result.detail)`), services.api_service's `/v1/run`
#         response body, and the discord/slack/telegram `_format_results`
#         chat replies — none of which redact themselves. Redacting at the
#         `RunResult(...)` construction choke point (both the success-path
#         `detail` and the failure-path `str(exc)`, which can itself echo
#         captured runner output) means every one of those sinks gets
#         already-masked text for free.
#   * `payload.secrets` itself (the runner-env mapping holding resolved
#     plaintext) is protected structurally, not by redaction: it is never
#     serialized into run state or artifacts (state_service only persists
#     `metadata`/`detail`/`summary` strings; RunnerPayload is not JSON-dumped
#     anywhere) — so there is nothing to redact there in the first place.
#
# Registry lifecycle (bounding, not indefinite growth): `_SECRET_VALUES` is
# process-global, so Orchestrator clears it via `clear_secret_values()` once a
# top-level `run_task`/`run_pipeline`/`run_debate` call fully completes
# (`finally`, after every sink for that run has already redacted against it)
# — see `Orchestrator._enter_run_scope`/`_exit_run_scope`. All three public
# entry points share the same reentrancy-safe depth counter, so a `run_debate`
# nested inside a role-driven `run_task` (or a standalone debate triggered
# repeatedly via ChatOps in the daemon) never clears prematurely nor leaks
# across separate invocations. This keeps the registry scoped to in-flight
# runs rather than accumulating for the process lifetime.
#
# Known limitations (by design — see also `_MIN_MASKABLE_LEN` below):
#   * Substring replacement: if a secret value happens to equal a common
#     word/substring, redaction will blank that substring everywhere else in
#     the same text too. This is a COSMETIC over-redaction risk, not a
#     security one — prefer long, high-entropy secret values in practice.
#   * Values shorter than `_MIN_MASKABLE_LEN` are never registered/redacted.
#   * Ephemeral inter-agent payloads (the debate request/rebuttal/resolution/
#     challenge turns in Orchestrator — see the `secrets={}` RunnerPayload
#     constructions) do not resolve `${secret:NAME}` references at all: the
#     literal reference string passes through unresolved. This is NOT a
#     leak — no secret value is ever produced or registered on that path —
#     it is simply an unresolved reference in a payload that was never wired
#     to a secrets catalog.
# ---------------------------------------------------------------------------

# Values shorter than this are never registered: a 1-3 char "secret" would mask
# far too much unrelated text (e.g. masking the string "on" out of every log
# line). Real credentials comfortably exceed this; the marker strings used by
# the masking tests are long and unique.
_MIN_MASKABLE_LEN = 4

_secret_values_lock = threading.Lock()
_SECRET_VALUES: set[str] = set()


def register_secret_value(value: str) -> None:
    """Register a resolved secret *value* so it is redacted from later output.

    No-op for empty/whitespace-only or very short values (see
    `_MIN_MASKABLE_LEN`). Idempotent and thread-safe.
    """
    if not isinstance(value, str):
        return
    stripped = value.strip()
    if len(stripped) < _MIN_MASKABLE_LEN:
        return
    with _secret_values_lock:
        _SECRET_VALUES.add(value)


def redact_text(text: str) -> str:
    """Replace every registered secret value in *text* with `REDACTED`.

    Fast-paths to the input when nothing is registered. Longer values are
    substituted first so an overlapping shorter value can't partially unmask a
    longer one.
    """
    if not isinstance(text, str) or not text:
        return text
    with _secret_values_lock:
        if not _SECRET_VALUES:
            return text
        values = sorted(_SECRET_VALUES, key=len, reverse=True)
    for secret in values:
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    return text


def redact_value(value: Any) -> Any:
    """Recursively redact registered secret values found anywhere inside
    *value*.

    Strings are passed through `redact_text`. `dict`/`list`/`tuple` are
    walked recursively and a NEW container is returned (the input is never
    mutated). Every other type (int, bool, None, ...) is returned unchanged.

    Used by writers that persist/emit a whole structure (log event kwargs,
    JSON artifacts) rather than a single string, so a secret nested inside a
    dict or list value can't slip past `redact_text` alone.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_value(val) for key, val in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


def registered_secret_values() -> frozenset[str]:
    """Snapshot of currently-registered secret values (for tests/introspection)."""
    with _secret_values_lock:
        return frozenset(_SECRET_VALUES)


def clear_secret_values() -> None:
    """Drop all registered secret values (test hygiene / process reset)."""
    with _secret_values_lock:
        _SECRET_VALUES.clear()
