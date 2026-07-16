"""SchedulerDaemon — polls schedules and re-runs quota-deferred retry rows."""

from __future__ import annotations

import json
import logging
import signal
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from hivepilot.orchestrator import Orchestrator
from hivepilot.services import state_service
from hivepilot.services.schedule_service import due_schedules, run_entry

logger = logging.getLogger(__name__)

# Phrases that indicate a quota/rate-limit failure on re-run
_QUOTA_PHRASES = ("session limit", "usage limit", "rate limit")


class SchedulerDaemon:
    """Background daemon that drives two responsibilities:

    1. **Schedule polling** — calls ``schedule_service.due_schedules()`` and
       dispatches each via ``run_entry()``.
    2. **Deferred-row processing** — scans ``retry_queue`` rows that were
       parked by ``enqueue_deferred()`` (they carry a ``context`` JSON blob)
       and re-runs them via ``Orchestrator.run_task()`` once their
       ``next_retry_at`` timestamp is past.
    """

    def __init__(
        self,
        check_interval: int = 30,
        shutdown_timeout: int = 120,
    ) -> None:
        self._check_interval = check_interval
        self._shutdown_timeout = shutdown_timeout
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the daemon loop (blocking).  Handles SIGTERM / SIGINT cleanly."""
        from hivepilot.config import settings
        from hivepilot.observability.tracing import init_tracing

        # Phase 18 — opt-in, no-op unless HIVEPILOT_ENABLE_TRACING=1 + the
        # `tracing` extra is installed. This is "a run begins" for the
        # scheduler daemon entry point (mirrors the API server startup and
        # the CLI's `run-pipeline` command).
        init_tracing(settings)

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        logger.info("scheduler_daemon.start", extra={"check_interval": self._check_interval})
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("scheduler_daemon.tick_error")
            self._stop_event.wait(timeout=self._check_interval)
        logger.info("scheduler_daemon.stop")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_signal(self, signum: int, frame: Any) -> None:  # noqa: ANN401
        logger.info("scheduler_daemon.signal_received", extra={"signum": signum})
        self._stop_event.set()

    def _tick(self) -> None:
        self._run_due_schedules()
        self._process_deferred_rows()

    def _run_due_schedules(self) -> None:
        try:
            schedules = due_schedules()
        except Exception:  # noqa: BLE001
            logger.exception("scheduler_daemon.due_schedules_error")
            return
        if not schedules:
            return
        orch = Orchestrator()
        for sched in schedules:
            try:
                run_entry(sched, orch)
            except Exception:  # noqa: BLE001
                logger.exception("scheduler_daemon.run_entry_error", extra={"schedule": sched})

    def _process_deferred_rows(self) -> None:
        """Fetch and re-run all past-due deferred rows (those with a context blob)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        state_service.init_db()
        with sqlite3.connect(state_service.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM retry_queue "
                "WHERE status='pending' AND next_retry_at <= ? AND context IS NOT NULL",
                (now_iso,),
            ).fetchall()

        for row in rows:
            ctx_raw = row["context"]
            if not ctx_raw:
                # Legacy row without context — skip
                continue
            try:
                ctx = json.loads(ctx_raw)
            except json.JSONDecodeError:
                logger.warning(
                    "scheduler_daemon.bad_context_json",
                    extra={"row_id": row["id"]},
                )
                continue
            self._rerun_deferred_row(dict(row), ctx)

    def _rerun_deferred_row(self, row: dict, ctx: dict) -> None:
        row_id: int = row["id"]
        task_name: str = ctx.get("task", row.get("task", "dev"))
        project_names: list[str] = json.loads(row.get("projects") or "[]")
        extra_prompt: str | None = ctx.get("extra_prompt")
        auto_git: bool = bool(ctx.get("auto_git", False))
        attempt: int = int(row.get("attempt", 0))
        max_attempts: int = int(row.get("max_attempts", 3))

        logger.info(
            "scheduler_daemon.rerun_deferred",
            extra={"row_id": row_id, "task": task_name, "projects": project_names},
        )
        try:
            orch = Orchestrator()
            orch.run_task(
                task_name=task_name,
                project_names=project_names,
                extra_prompt=extra_prompt,
                auto_git=auto_git,
            )
        except Exception as exc:  # noqa: BLE001
            exc_str = str(exc).lower()
            is_quota = any(phrase in exc_str for phrase in _QUOTA_PHRASES)
            new_attempt = attempt + 1

            if is_quota:
                # Reschedule for another 30 min regardless of attempt count
                next_at = datetime.now(timezone.utc) + timedelta(minutes=30)
                self._set_row_status(row_id, "pending", attempt=new_attempt, next_retry_at=next_at)
                logger.warning(
                    "scheduler_daemon.rerun_quota_again",
                    extra={"row_id": row_id, "next_retry_at": next_at.isoformat()},
                )
            elif new_attempt >= max_attempts:
                self._set_row_status(row_id, "dead", attempt=new_attempt)
                logger.error(
                    "scheduler_daemon.rerun_dead",
                    extra={"row_id": row_id, "error": str(exc)},
                )
            else:
                self._set_row_status(row_id, "pending", attempt=new_attempt)
                logger.warning(
                    "scheduler_daemon.rerun_failed",
                    extra={"row_id": row_id, "attempt": new_attempt, "error": str(exc)},
                )
        else:
            self._set_row_status(row_id, "done")
            logger.info("scheduler_daemon.rerun_done", extra={"row_id": row_id})

    def _set_row_status(
        self,
        row_id: int,
        status: str,
        attempt: int | None = None,
        next_retry_at: datetime | None = None,
    ) -> None:
        state_service.init_db()
        with sqlite3.connect(state_service.DB_PATH) as conn:
            if attempt is not None and next_retry_at is not None:
                conn.execute(
                    "UPDATE retry_queue SET status=?, attempt=?, next_retry_at=? WHERE id=?",
                    (status, attempt, next_retry_at.isoformat(), row_id),
                )
            elif attempt is not None:
                conn.execute(
                    "UPDATE retry_queue SET status=?, attempt=? WHERE id=?",
                    (status, attempt, row_id),
                )
            else:
                conn.execute(
                    "UPDATE retry_queue SET status=? WHERE id=?",
                    (status, row_id),
                )
            conn.commit()
