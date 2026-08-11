"""Textual-free persistence helper for the `plugins_disabled` toggle.

Extracted out of `hivepilot.ui.plugin_manager` (which unconditionally imports
`textual` at module scope — an optional, TUI-only dependency) so that
non-TUI callers, notably `hivepilot.services.api_service`'s
`POST /v1/plugins/{name}/toggle` endpoint, can import `persist_plugins_disabled`
without transitively requiring `textual` to be installed. `plugin_manager`
re-exports both names from here for backwards compatibility (existing
`monkeypatch.setattr(plugin_manager, "persist_plugins_disabled", ...)`
call sites keep working — see `tests/test_plugin_manager_tui.py`).
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from hivepilot.config import settings

logger = structlog.get_logger(__name__)

_ENV_KEY = "HIVEPILOT_PLUGINS_DISABLED"


def persist_plugins_disabled(disabled: list[str], *, env_path: Path | None = None) -> Path:
    """Upsert `HIVEPILOT_PLUGINS_DISABLED=<json list>` into the `.env` file
    `Settings` reads its overrides from.

    There is no dedicated writer for scalar/list `Settings` fields today
    (unlike `hivepilot.services.config_writer`'s ruamel round-trip writer,
    which only covers the 6 declarative YAML domain files — projects/roles/
    policies/groups/pipelines/tasks — none of which back `plugins_disabled`;
    every `Settings` field, including `plugins_enabled`/`plugins_disabled`,
    is sourced purely from env vars / the resolved `.env` file). This upserts
    the SAME dotenv file/format `Settings` already reads (see
    `hivepilot.config._resolve_env_file`) rather than inventing a new one —
    it preserves every other line verbatim and only replaces (or appends)
    the `HIVEPILOT_PLUGINS_DISABLED=` line.

    Effective on next start only: `PluginManager` scans/registers once, at
    construction — see `hivepilot.ui.plugin_manager` module docstring.
    """
    if env_path is None:
        from hivepilot.config import Settings

        # Settings.model_config["env_file"] is resolved once, at class
        # definition/import time (see hivepilot.config._resolve_env_file) —
        # it will NOT reflect a HIVEPILOT_ENV_FILE change made after startup.
        env_path = Path(str(Settings.model_config.get("env_file") or ".env"))

    # Anchor a relative path to base_dir, never the CWD. `plugins install
    # shadcn` typed from an SSH home wrote /home/<user>/.env -- a file no
    # service reads -- and printed "persisted to .env" as though it had taken
    # effect. Where a write lands must not depend on where the command was
    # typed.
    if not env_path.is_absolute():
        env_path = settings.resolve_path(env_path)

    # A file that did not exist is a file nothing was reading. On a deployment
    # configured through systemd's EnvironmentFile= this write is inert
    # wherever it lands, and saying so is the difference between a no-op and a
    # no-op the operator knows about.
    existed = env_path.exists()

    line = f"{_ENV_KEY}={json.dumps(sorted(disabled))}"

    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    for i, existing in enumerate(lines):
        if existing.startswith(f"{_ENV_KEY}="):
            lines[i] = line
            break
    else:
        lines.append(line)

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not existed:
        logger.warning(
            "plugin_persist.created_new_env_file",
            path=str(env_path),
            detail="this file did not exist, so nothing was reading it; the flag "
            "takes effect only if Settings actually loads this path",
        )

    return env_path
