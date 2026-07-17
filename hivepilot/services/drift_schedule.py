"""Scheduled periodic IaC drift scans + alerting (Phase 20 Sprint D3).

Reads an opt-in `drift:` block from `schedules.yaml` (the same file
`schedule_service.load_schedules` reads), decides which configured IaC
projects are due for a drift scan (mirroring `schedule_service.due_schedules`'
due-calc exactly, keyed by a `drift:<project>` schedule name so it never
collides with a real `ScheduleEntry`), runs the scan via
`drift_service.scan_and_record`, and sends a SECRET-SAFE, counts-only alert
when drift is detected (or a tool+exit-code-only alert when the scan itself
fails).

Gated auto-remediation (Phase 20 Sprint D4)
--------------------------------------------
When `cfg.auto_remediate` is set AND drift was detected, this module kicks
off the operator-configured `cfg.remediate_task` -- a project task whose
step(s) resolve to a destructive IaC operation (`apply`/`destroy`) -- via
`Orchestrator.run_task`. It NEVER applies infrastructure directly: routing
through `Orchestrator.run_task` means the destructive step is paused by the
EXISTING step-level approval gate (`hivepilot.orchestrator.
step_requires_approval` / `StepApprovalPending`), the exact same gate every
other orchestrator-run destructive step goes through. A human must
separately approve the paused run via `Orchestrator.run_approved` (existing
Telegram/Slack/CLI approval flow) -- this module only ever *queues*
remediation.

Fail-closed: without an operator-configured `remediate_task` there is no
task to gate remediation on, so remediation is skipped entirely (with an
alert) rather than guessing one. `Orchestrator.run_task` currently absorbs
a mid-task `StepApprovalPending` internally and returns it reflected in a
`RunResult.detail` (rather than raising it to its own caller) -- both that
returned-status shape AND a direct raise (in case that internal contract
ever changes) are treated identically: the EXPECTED, desired "awaiting
approval" outcome, never an error. Any OTHER exception from `run_task` is
NOT caught here -- it propagates (after the last-run stamp still fires) so
the daemon's per-project try/except is what ultimately swallows it, since an
arbitrary exception's string isn't guaranteed leak-free.

Fail-safe by design: `load_drift_config` never raises (missing file/key or
malformed YAML all resolve to a disabled default), and `run_drift_scan` never
propagates a *scan* failure -- both are load-bearing for the scheduler
daemon, which must never die because one project's drift check misbehaves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from hivepilot.config import settings
from hivepilot.orchestrator import Orchestrator, StepApprovalPending
from hivepilot.services import drift_service, notification_service, project_service, state_service
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_INTERVAL_MINUTES = 60


@dataclass
class DriftScanConfig:
    """Parsed `drift:` block from `schedules.yaml`. Disabled by default."""

    enabled: bool = False
    interval_minutes: int = _DEFAULT_INTERVAL_MINUTES
    projects: list[str] = field(default_factory=list)
    runner_kind: str = "opentofu"
    auto_remediate: bool = False
    # The project task to run for gated auto-remediation (Phase 20 D4) --
    # its step(s) must resolve to a destructive IaC operation for the
    # existing step-approval gate to actually pause it. `None` (the default)
    # means auto-remediation is fail-closed: `run_drift_scan` skips
    # remediation entirely rather than guessing a task.
    remediate_task: str | None = None
    channels: list[str] | None = None


def load_drift_config(path: Path | None = None) -> DriftScanConfig:
    """Read the `drift:` block from `settings.schedules_file` (or *path*).

    Fail-safe: a missing file, a missing `drift:` key, or malformed YAML/shape
    all resolve to a disabled default rather than raising -- this must never
    crash the scheduler daemon's tick loop.
    """
    try:
        resolved = settings.resolve_config_path(path or settings.schedules_file)
        if not resolved.exists():
            return DriftScanConfig()
        data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        drift = data.get("drift")
        if not isinstance(drift, dict):
            return DriftScanConfig()
        projects_raw = drift.get("projects", [])
        channels_raw = drift.get("channels")
        remediate_task_raw = drift.get("remediate_task")
        return DriftScanConfig(
            enabled=bool(drift.get("enabled", False)),
            interval_minutes=int(drift.get("interval_minutes", _DEFAULT_INTERVAL_MINUTES)),
            # Guard against a bare scalar (e.g. `projects: proj-a`) being
            # silently char-iterated by `list(...)` into bogus single-letter
            # "projects" -- only a real list is ever accepted.
            projects=list(projects_raw) if isinstance(projects_raw, list) else [],
            runner_kind=str(drift.get("runner_kind", "opentofu")),
            auto_remediate=bool(drift.get("auto_remediate", False)),
            # Fail-closed: only a real, non-empty string is ever accepted --
            # anything else (missing key, wrong type) resolves to None so
            # run_drift_scan's remediation guard skips rather than guesses.
            remediate_task=remediate_task_raw
            if isinstance(remediate_task_raw, str) and remediate_task_raw
            else None,
            # Same guard for `channels: slack` (bare scalar) -- fall back to
            # None (send_notification's own all-channels default) rather
            # than char-iterating it.
            channels=list(channels_raw)
            if isinstance(channels_raw, list) and channels_raw
            else None,
        )
    except Exception:  # noqa: BLE001 -- fail-safe: never crash the daemon on bad config
        logger.warning("drift_schedule.config_load_failed", exc_info=True)
        return DriftScanConfig()


def _drift_schedule_name(project_name: str) -> str:
    """Namespaced schedule-run key so a drift scan's cadence never collides
    with a same-named `ScheduleEntry` in `schedule_runs`."""
    return f"drift:{project_name}"


def due_drift_projects(cfg: DriftScanConfig) -> list[str]:
    """Return the subset of `cfg.projects` due for a drift scan.

    Mirrors `schedule_service.due_schedules()`'s due-calc exactly: a
    never-scanned project is immediately due; otherwise due once
    `interval_minutes` has elapsed since its last recorded run.
    """
    if not cfg.enabled:
        return []
    due: list[str] = []
    now = datetime.now(timezone.utc)
    for project_name in cfg.projects:
        last_run = state_service.get_schedule_last_run(_drift_schedule_name(project_name))
        next_run_time = last_run + timedelta(minutes=cfg.interval_minutes) if last_run else now
        if next_run_time <= now:
            due.append(project_name)
    return due


def _attempt_remediation(cfg: DriftScanConfig, project_name: str) -> None:
    """Kick off gated auto-remediation for *project_name* (Phase 20 D4).

    NEVER applies infrastructure directly -- always routes through
    `Orchestrator.run_task`, so the destructive apply step inside
    `cfg.remediate_task` is paused by the EXISTING step-approval gate
    exactly like any other orchestrator-run destructive step. This function
    only ever *queues* remediation; a human must separately approve it via
    `Orchestrator.run_approved`.

    Fail-closed guard: without an operator-configured `remediate_task` there
    is no task to gate on, so remediation is skipped (with an alert) rather
    than guessing one.

    `Orchestrator.run_task` currently absorbs a mid-task
    `StepApprovalPending` internally and reflects it in a returned
    `RunResult.detail` instead of raising it to its own caller -- both that
    shape and a direct raise (defense-in-depth, in case that internal
    contract ever changes) are handled identically here as the EXPECTED,
    desired "awaiting approval" outcome. Any OTHER exception is deliberately
    NOT caught -- it propagates to `run_drift_scan`'s caller (after the
    last-run stamp still fires via its `finally`), since an arbitrary
    exception's string isn't guaranteed leak-free.
    """
    if cfg.remediate_task is None:
        notification_service.send_notification(
            f"⚠️ auto_remediate enabled for {project_name} but no remediate_task "
            "configured — skipping remediation",
            channels=cfg.channels,
        )
        return

    try:
        results = Orchestrator().run_task(
            project_names=[project_name],
            task_name=cfg.remediate_task,
            extra_prompt=None,
            auto_git=False,
        )
    except StepApprovalPending:
        notification_service.send_notification(
            f"🔒 Remediation for {project_name} is queued and awaiting approval",
            channels=cfg.channels,
        )
        return

    if any(
        result.detail is not None and result.detail.startswith("Pending approval")
        for result in results
    ):
        notification_service.send_notification(
            f"🔒 Remediation for {project_name} is queued and awaiting approval",
            channels=cfg.channels,
        )


def run_drift_scan(cfg: DriftScanConfig, project_name: str) -> None:
    """Scan *project_name* for drift and alert if needed.

    Stamps the `drift:<project_name>` last-run marker in a `finally` block so
    it fires regardless of outcome -- success, a known-safe scan failure
    (`RuntimeError`/`ValueError`), OR an unexpected exception (e.g. a
    `sqlite3.OperationalError` from a lock contended by the daemon/API/CLI
    sharing one state DB). Without this, an unexpected exception would skip
    the stamp entirely and `due_drift_projects` would re-select the project
    every tick forever (retry-spam). Alerts contain ONLY the project name,
    runner kind, and integer plan counts (or a generic "changes detected"
    when the summary line couldn't be parsed) -- never raw plan output.

    Only the known-safe `RuntimeError`/`ValueError` path (already
    tool+exit-code-only per `drift_service`'s anti-leak guarantee) gets a
    failure alert -- an arbitrary/unexpected exception's string is NOT
    guaranteed leak-free, so it is deliberately not alerted on; it re-raises
    after the `finally` stamp so the caller (the daemon's per-project
    try/except) logs+swallows it instead.
    """
    schedule_name = _drift_schedule_name(project_name)
    try:
        try:
            project = project_service.load_projects().projects.get(project_name)
            if project is None:
                raise RuntimeError(f"Unknown project: {project_name!r}")
            result = drift_service.scan_and_record(
                project, runner_kind=cfg.runner_kind, tenant="default"
            )
        except (RuntimeError, ValueError) as exc:
            notification_service.send_notification(
                f"⚠️ Drift scan FAILED on {project_name}: {exc}",
                channels=cfg.channels,
            )
            return

        if not result.drifted:
            return

        if result.summary is not None:
            s = result.summary
            counts = f"+{s.to_add} ~{s.to_change} -{s.to_destroy}"
        else:
            counts = "changes detected"
        notification_service.send_notification(
            f"⚠️ Drift detected on {project_name} ({cfg.runner_kind}): {counts}",
            channels=cfg.channels,
        )

        if cfg.auto_remediate:
            _attempt_remediation(cfg, project_name)
    finally:
        state_service.update_schedule_run(schedule_name)
