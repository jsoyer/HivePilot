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
    task: str
    projects: list[str]
    interval_minutes: int = 1440
    enabled: bool = True


def load_schedules(path: Path | None = None) -> dict[str, ScheduleEntry]:
    resolved = settings.resolve_config_path(path or settings.schedules_file)
    if not resolved.exists():
        return {}
    data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    entries = {}
    for name, values in data.get("schedules", {}).items():
        entries[name] = ScheduleEntry(
            name=name,
            task=values["task"],
            projects=values.get("projects", []),
            interval_minutes=values.get("interval_minutes", 1440),
            enabled=values.get("enabled", True),
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


def run_entry(entry: ScheduleEntry, orchestrator, *, max_attempts: int = 3, base_delay_minutes: int = 2) -> bool:
    """
    Run a schedule entry via the orchestrator.
    On failure, enqueue into the retry queue with exponential backoff.
    Returns True if the run succeeded immediately.
    """
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
