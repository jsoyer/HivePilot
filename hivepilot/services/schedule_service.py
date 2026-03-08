from __future__ import annotations

import yaml
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

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
    resolved = settings.resolve_path(path or settings.schedules_file)
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
            else datetime.utcnow()
        )
        if next_run_time <= datetime.utcnow():
            due.append(entry)
    return due


def mark_run(entry: ScheduleEntry) -> None:
    state_service.update_schedule_run(entry.name)
