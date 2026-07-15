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
  or doesn't exist on disk. Does NOT honor `dry_run` — see the "Known
  limitation" note in `notify()`'s docstring.
- Hooks: never raise. A broken/misconfigured vault is a silent no-op — a
  hook must never crash a pipeline run. `on_pipeline_end` / `on_error` honor
  the run's `dry_run` flag (threaded in via `run_hook(..., dry_run=...)` by
  `Orchestrator.run_pipeline` — `hivepilot/orchestrator.py`): a dry-run
  pipeline builds `ObsidianService(vault, dry_run=True)`, which plans the
  run-report write but never touches the vault.

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

from hivepilot.plugins import HealthStatus
from hivepilot.services.notification_service import NotConfigured
from hivepilot.services.obsidian_service import ObsidianService
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# `settings.obsidian_vault` (hivepilot/config.py) is a non-Optional `Path`
# field defaulting to `Path("obsidian-vault")` rather than `None` — there is
# no clean "unset" sentinel on the field itself. `health()` below treats a
# vault still equal to this field default (and absent on disk) as "unset /
# not configured" (degraded), distinct from an operator-set path that simply
# doesn't exist on disk (error). Read once, lazily, from the `Settings`
# class's own field metadata rather than hardcoded here, so it can never
# drift from the real default.
from hivepilot.config import Settings  # noqa: E402

_DEFAULT_OBSIDIAN_VAULT = Settings.model_fields["obsidian_vault"].default


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

    **Known limitation — does NOT honor `dry_run`.** Unlike `on_pipeline_end`
    / `on_error`, this notifier always writes for real
    (`ObsidianService(vault, dry_run=False)`). The notifier contract
    (`NOTIFIER_MAP: dict[str, Callable[[str], None]]`,
    `hivepilot/services/notification_service.py`) is a bare
    `Callable[[str], None]` shared by every notifier (built-in
    slack/discord/telegram + any plugin's) — there is no per-call `dry_run`
    parameter to thread through without changing that shared contract for
    all of them, which is out of scope for this kwarg-threading change (see
    the hook-context-enrichment investigation). In practice this is low-risk
    today: no call site in this repo passes `channels=["obsidian", ...]` to
    `notification_service.send_notification()` (the default channel list is
    `["slack", "discord", "telegram"]`), so `notify()` is only reachable via
    a caller that explicitly opts in to the "obsidian" channel — a case this
    codebase doesn't exercise. Left undone, not silently — a future revision
    could widen the notifier `Callable` to `Callable[[str], None] |
    Callable[[str, bool], None]` (or add an optional `dry_run` kwarg) if a
    real dry-run-aware notifier caller emerges.
    """
    vault = _resolve_vault()
    if vault is None:
        raise NotConfigured("obsidian_vault not configured or does not exist")

    entry = f"- {_timestamp()} — {message}"
    svc = ObsidianService(vault, dry_run=False)
    svc.append_daily(entry)


def on_pipeline_end(**kwargs: Any) -> None:
    """Append a structured run-report block for a finished pipeline run.

    Honors the run's ``dry_run`` flag (``Orchestrator.run_pipeline`` passes
    it through ``run_hook("on_pipeline_end", ..., dry_run=...)``): a dry-run
    pipeline builds an ``ObsidianService(vault, dry_run=True)``, which plans
    the write but never touches the vault (see
    ``hivepilot/services/obsidian_service.py::_write_or_plan``). Absent the
    kwarg (older caller / direct test invocation), defaults to ``False`` —
    a real write, preserving prior behavior.

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
        svc = ObsidianService(vault, dry_run=bool(kwargs.get("dry_run", False)))
        svc.append_daily(entry)
    except Exception as exc:  # noqa: BLE001 — a hook must never crash a run
        logger.warning("plugin.obsidian.on_pipeline_end_failed", error=str(exc))


def on_error(**kwargs: Any) -> None:
    """Append a structured failure-report block for a failed pipeline stage.

    Honors the run's ``dry_run`` flag the same way ``on_pipeline_end`` does
    — see that function's docstring.

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
        svc = ObsidianService(vault, dry_run=bool(kwargs.get("dry_run", False)))
        svc.append_daily(entry)
    except Exception as exc:  # noqa: BLE001 — a hook must never crash a run
        logger.warning("plugin.obsidian.on_error_failed", error=str(exc))


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `settings.obsidian_vault` is set (differs from the field
    default) AND exists on disk; `error` when it's set but the path is
    missing; `degraded` ("not configured") when it's still the field
    default — see the `_DEFAULT_OBSIDIAN_VAULT` note above. Only the
    boolean/existence is reported, never the path's contents.
    """
    from hivepilot.config import settings

    vault = settings.obsidian_vault
    path = Path(vault).expanduser()
    if path.exists():
        return HealthStatus("ok", "vault configured and present")
    if path == _DEFAULT_OBSIDIAN_VAULT:
        return HealthStatus("degraded", "not configured")
    return HealthStatus("error", "obsidian_vault configured but path does not exist")


def register() -> dict[str, Any]:
    return {
        "notifiers": {"obsidian": notify},
        "on_pipeline_end": on_pipeline_end,
        "on_error": on_error,
        "health": {"obsidian": health},
    }
