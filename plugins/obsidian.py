"""obsidian plugin — logs pipeline runs into the Obsidian vault.

Ships as BOTH a notifier and a pair of lifecycle hooks, both appending to the
SAME daily journal note: ``12 - HivePilot/Runs/YYYY-MM-DD.md``.

- Notifier ``obsidian``: each `send_notification(...)` string becomes a
  timestamped line in today's journal.
- Hooks ``on_pipeline_end`` / ``on_error``: append a structured run-report
  block (run_id, pipeline, status/stage, timestamp) to the same journal.

Reuses `hivepilot.services.obsidian_service.ObsidianService` for ALL I/O
(path guard + frontmatter) — never a raw `open().write()`. The vault comes
from `settings.obsidian_vault`, resolved lazily inside each function so a
config change picked up between calls is honored and importing this module
has no side effects.

Contract:
- Notifier: raises `NotConfigured` (the standard "skip silently" signal, see
  `hivepilot.services.notification_service`) when the vault isn't configured
  or doesn't exist on disk.
- Hooks: never raise. A broken/misconfigured vault is a silent no-op — a
  hook must never crash a pipeline run.

Deliberately NOT a `@dataclass`: local-file plugins are loaded via
`importlib.util.spec_from_file_location()` / `exec_module()`
(`hivepilot.plugins._scan_local_plugins`), which never registers the module
in `sys.modules`. Combined with `from __future__ import annotations`, that
trips a real CPython 3.14 `dataclasses` bug (`_is_type` does
`sys.modules[cls.__module__].__dict__`, which is `None` for an unregistered
module) — see `plugins/rtk.py` for the full write-up. This plugin sticks to
plain functions, sidestepping the issue entirely.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hivepilot.services.notification_service import NotConfigured
from hivepilot.services.obsidian_service import ObsidianService
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


def _resolve_vault() -> Path | None:
    """Return the configured vault path if set and present on disk, else None."""
    from hivepilot.config import settings

    vault = settings.obsidian_vault
    if not vault:
        return None
    path = Path(vault).expanduser()
    if not path.exists():
        return None
    return path.resolve()


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def notify(message: str) -> None:
    """Append a timestamped line for *message* to today's daily journal.

    Raises `NotConfigured` (the notifier "skip silently" contract) when the
    vault isn't configured or doesn't exist on disk.
    """
    vault = _resolve_vault()
    if vault is None:
        raise NotConfigured("obsidian_vault not configured or does not exist")

    entry = f"- {_timestamp()} — {message}"
    svc = ObsidianService(vault, dry_run=False)
    svc.append_daily(entry)


def on_pipeline_end(**kwargs: Any) -> None:
    """Append a structured run-report block for a finished pipeline run.

    Never raises — a broken hook must never crash a run. Silent no-op when
    the vault isn't configured or doesn't exist.
    """
    try:
        vault = _resolve_vault()
        if vault is None:
            return
        entry = (
            f"### Run report — {_timestamp()}\n"
            f"- run_id: {kwargs.get('run_id')}\n"
            f"- pipeline: {kwargs.get('pipeline')}\n"
            f"- status: {kwargs.get('status')}\n"
        )
        svc = ObsidianService(vault, dry_run=False)
        svc.append_daily(entry)
    except Exception as exc:  # noqa: BLE001 — a hook must never crash a run
        logger.warning("plugin.obsidian.on_pipeline_end_failed", error=str(exc))


def on_error(**kwargs: Any) -> None:
    """Append a structured failure-report block for a failed pipeline stage.

    Never raises — a broken hook must never crash a run. Silent no-op when
    the vault isn't configured or doesn't exist.
    """
    try:
        vault = _resolve_vault()
        if vault is None:
            return
        entry = (
            f"### Run error — {_timestamp()}\n"
            f"- run_id: {kwargs.get('run_id')}\n"
            f"- pipeline: {kwargs.get('pipeline')}\n"
            f"- stage: {kwargs.get('stage')}\n"
        )
        svc = ObsidianService(vault, dry_run=False)
        svc.append_daily(entry)
    except Exception as exc:  # noqa: BLE001 — a hook must never crash a run
        logger.warning("plugin.obsidian.on_error_failed", error=str(exc))


def register() -> dict[str, Any]:
    return {
        "notifiers": {"obsidian": notify},
        "on_pipeline_end": on_pipeline_end,
        "on_error": on_error,
    }
