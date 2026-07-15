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
# SAME `REDACTED` sentinel wherever it later appears in rendered output (logs,
# provenance, serialized state). Keeping this in `config_provenance` means the
# whole codebase shares one masking vocabulary and one redaction entry point.
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


def registered_secret_values() -> frozenset[str]:
    """Snapshot of currently-registered secret values (for tests/introspection)."""
    with _secret_values_lock:
        return frozenset(_SECRET_VALUES)


def clear_secret_values() -> None:
    """Drop all registered secret values (test hygiene / process reset)."""
    with _secret_values_lock:
        _SECRET_VALUES.clear()
