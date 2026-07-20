from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from hivepilot.config import settings
from hivepilot.services import state_service


@dataclass
class ScheduleEntry:
    name: str
    projects: list[str]
    task: str | None = None
    interval_minutes: int = 1440
    enabled: bool = True
    # Phase (Autopilot) — mutually exclusive with `task`. The only supported
    # value is "autopilot": `run_entry` then drains at most one objective
    # from the guarded autopilot queue (see `autopilot_queue.py`) instead of
    # running a fixed task. Existing fixed-`task` entries are byte-identical
    # (this field defaults to `None` and is never populated by them).
    source: str | None = None

    def __post_init__(self) -> None:
        has_task = bool(self.task)
        has_source = bool(self.source)
        if has_task == has_source:
            raise ValueError(
                f"schedule {self.name!r}: exactly one of 'task' or 'source' must be set "
                f"(got task={self.task!r}, source={self.source!r})"
            )
        if has_source and self.source != "autopilot":
            raise ValueError(
                f"schedule {self.name!r}: unsupported source {self.source!r} "
                "(only 'autopilot' is supported)"
            )


def load_schedules(path: Path | None = None) -> dict[str, ScheduleEntry]:
    resolved = settings.resolve_config_path(path or settings.schedules_file)
    if not resolved.exists():
        return {}
    data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    entries = {}
    for name, values in data.get("schedules", {}).items():
        entries[name] = ScheduleEntry(
            name=name,
            task=values.get("task"),
            projects=values.get("projects", []),
            interval_minutes=values.get("interval_minutes", 1440),
            enabled=values.get("enabled", True),
            source=values.get("source"),
        )
    return entries


def due_schedules() -> list[ScheduleEntry]:
    entries = load_schedules()
    due: list[ScheduleEntry] = []
    for entry in entries.values():
        if not entry.enabled:
            continue
        last_run = state_service.get_schedule_last_run(entry.name)
        next_run_time = (
            last_run + timedelta(minutes=entry.interval_minutes)
            if last_run
            else datetime.now(timezone.utc)
        )
        if next_run_time <= datetime.now(timezone.utc):
            due.append(entry)
    return due


def mark_run(entry: ScheduleEntry) -> None:
    state_service.update_schedule_run(entry.name)


def run_entry(
    entry: ScheduleEntry, orchestrator, *, max_attempts: int = 3, base_delay_minutes: int = 2
) -> bool:
    """
    Run a schedule entry via the orchestrator.
    On failure, enqueue into the retry queue with exponential backoff.
    Returns True if the run succeeded immediately.

    Entries with `source == "autopilot"` skip the fixed-task path entirely
    and instead drain at most one objective from the guarded autopilot
    queue via `autopilot_queue.drain_one` -- see that module's docstring
    for the fail-closed gate contract. This branch never touches the
    retry_service backoff path (a denied/blocked drain is an expected,
    visible "awaiting human" state, not a schedule failure), and always
    calls `mark_run(entry)` so the schedule's interval-based due-calc
    still applies normally. Fixed-`task` entries (source is None) are
    completely unaffected -- behavior below this branch is unchanged.
    """
    if entry.source == "autopilot":
        from hivepilot.services import autopilot_queue

        autopilot_queue.drain_one(orchestrator, tenant="default")
        mark_run(entry)
        return True

    from hivepilot.services import retry_service

    try:
        orchestrator.run_task(
            project_names=entry.projects,
            task_name=entry.task,
            extra_prompt=None,
            auto_git=False,
        )
        mark_run(entry)
        return True
    except Exception as exc:  # noqa: BLE001
        retry_service.enqueue(
            schedule_name=entry.name,
            task=entry.task,
            projects=entry.projects,
            error=str(exc),
            attempt=1,
            max_attempts=max_attempts,
            base_delay_minutes=base_delay_minutes,
        )
        return False
