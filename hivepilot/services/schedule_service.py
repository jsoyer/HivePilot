from __future__ import annotations

import hashlib
import subprocess  # nosec B404 — only `git rev-parse` on a configured project path
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from hivepilot.config import settings
from hivepilot.services import events, state_service
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

#: Cap on the carried-forward scratch so memory never grows unbounded.
_MAX_SCRATCH = 4000


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
    # Cron that remembers (HP-74). When true, a fixed-`task` entry carries the
    # prior run's output into the next run's context AND skips the model call
    # entirely when its inputs (task + projects + each project's git HEAD) are
    # unchanged since the last run. Default false = byte-identical legacy
    # behavior; no-op on autopilot entries.
    remember: bool = False

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
            remember=bool(values.get("remember", False)),
        )
    return entries


def _repo_head(path: str) -> str | None:
    try:
        result = subprocess.run(  # nosec B603 B607 — fixed argv, configured path
            ["git", "-C", path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:  # noqa: BLE001 — HEAD is best-effort; None just widens the hash
        return None


def _project_paths(entry: ScheduleEntry, orchestrator: Any) -> list[str]:
    """Resolve each project's filesystem path from the orchestrator's registry
    (best-effort; a mock/empty registry simply yields no paths)."""
    registry = getattr(getattr(orchestrator, "projects", None), "projects", None)
    if not isinstance(registry, dict):
        return []
    paths: list[str] = []
    for name in entry.projects:
        pc = registry.get(name)
        path = getattr(pc, "path", None)
        if path is not None:
            paths.append(str(path))
    return paths


def schedule_input_hash(entry: ScheduleEntry, orchestrator: Any) -> str:
    """Stable fingerprint of everything that affects a remembered run's output:
    the task, its projects, and each project's git HEAD. An identical hash to
    the last run means nothing changed — the run is a no-op (HP-74)."""
    h = hashlib.sha256()
    h.update((entry.task or "").encode())
    for name in sorted(entry.projects):
        h.update(b"\x00")
        h.update(name.encode())
    for path in _project_paths(entry, orchestrator):
        h.update(b"\x00")
        h.update((_repo_head(path) or "").encode())
    return h.hexdigest()


def _aggregate_output(results: Any) -> str | None:
    """Join the `detail` of each RunResult into the scratch carried to the next
    run, capped. Tolerant of a non-list return (e.g. a test mock)."""
    if not isinstance(results, (list, tuple)):
        return None
    parts = [str(getattr(r, "detail", "")) for r in results if getattr(r, "detail", None)]
    if not parts:
        return None
    return "\n\n".join(parts)[:_MAX_SCRATCH]


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

    # Cron that remembers (HP-74): when enabled, short-circuit a no-op run and
    # carry the prior run's output into this one's context.
    prior_context: str | None = None
    current_hash: str | None = None
    if entry.remember:
        current_hash = schedule_input_hash(entry, orchestrator)
        memory = state_service.get_schedule_memory(entry.name)
        if memory is not None:
            if memory.get("last_input_hash") == current_hash:
                # Nothing changed since the last run — skip the model entirely.
                # Cadence is still stamped so the entry doesn't stay due and
                # busy-loop the daemon.
                logger.info("schedule.noop_skip", schedule=entry.name)
                events.emit(
                    "schedule.noop_skip",
                    "schedule",
                    entry.name,
                    payload={"schedule": entry.name, "reason": "unchanged"},
                )
                mark_run(entry)
                return True
            prior_context = memory.get("scratch") or None

    try:
        run_kwargs: dict[str, Any] = {
            "project_names": entry.projects,
            "task_name": entry.task,
            "extra_prompt": None,
            "auto_git": False,
        }
        if prior_context:
            run_kwargs["prior_context"] = prior_context
        results = orchestrator.run_task(**run_kwargs)
        if entry.remember:
            output = _aggregate_output(results)
            state_service.upsert_schedule_memory(
                entry.name,
                scratch=output,
                last_output=output,
                last_input_hash=current_hash,
            )
        mark_run(entry)
        return True
    except Exception as exc:  # noqa: BLE001
        # Stamp last_run even on failure so the schedule's own
        # interval_minutes cadence is respected by due_schedules() --
        # otherwise a persistently-failing task never stamps last_run and
        # stays perpetually "due", causing the daemon to re-dispatch it
        # every tick (~30s) on top of the independent retry_service backoff
        # below. Retries remain governed entirely by retry_service's own
        # retry_queue/backoff -- this only stops the schedule ITSELF from
        # busy-looping.
        mark_run(entry)
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
