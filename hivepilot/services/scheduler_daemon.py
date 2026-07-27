"""SchedulerDaemon -- polls schedules and re-runs quota-deferred retry rows.

fix/retry-queue-drain: this daemon's per-tick responsibilities also include
expiring stale `retry_queue` rows (`_expire_stale_retries`) and draining
plain exponential-backoff retry rows (`_process_backoff_retries`) -- the
subtype `retry_service.enqueue()` writes on an ordinary schedule task
failure (`schedule_service.run_entry`'s except branch), which
`_process_deferred_rows` below deliberately never touches (it filters
`context IS NOT NULL`, a guard written only for the quota-deferred
subtype). Before this fix, nothing ever read a context-less row: 197
`groomer-scan` retries sat `pending` and past-due for 7 days with zero
operator-visible signal.
"""

from __future__ import annotations

import json
import logging
import signal
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from hivepilot.orchestrator import Orchestrator
from hivepilot.services import state_service
from hivepilot.services.schedule_service import due_schedules, run_entry

if TYPE_CHECKING:
    from hivepilot.plugins import PluginManager, ReloadResult

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
        # Phase 26b — opt-in hot-reload (`settings.plugins_hot_reload`). This
        # is a DEDICATED, long-lived `PluginManager` owned by the daemon
        # itself, lazily constructed on first use (`_ensure_hot_reload_manager`)
        # — NOT the ad-hoc `PluginManager` each `Orchestrator()` construction
        # below builds fresh per schedule-dispatch / per-deferred-row (see
        # `_run_due_schedules` / `_rerun_deferred_row`: `Orchestrator()` is
        # never cached here, so there is no single long-lived Orchestrator to
        # attach reload to — reconciling with that real lifecycle is why this
        # manager is a SEPARATE object; see `_ensure_hot_reload_manager`).
        self._hot_reload_manager: PluginManager | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the daemon loop (blocking).  Handles SIGTERM / SIGINT / SIGHUP."""
        from hivepilot.config import settings
        from hivepilot.observability.tracing import init_tracing

        # Phase 18 — opt-in, no-op unless HIVEPILOT_ENABLE_TRACING=1 + the
        # `tracing` extra is installed. This is "a run begins" for the
        # scheduler daemon entry point (mirrors the API server startup and
        # the CLI's `run-pipeline` command).
        init_tracing(settings)

        # Bug-debt fix — log the RESOLVED, ABSOLUTE paths this daemon
        # process is actually using (state DB / topics registry / config
        # dir / prompts dir / vault): see `hivepilot.utils.startup_paths`
        # for the full rationale.
        self._log_startup_paths_once()

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        # SIGHUP is POSIX-only (no-op guard for Windows, which has no
        # signal.SIGHUP). Always registered — not gated by
        # `plugins_hot_reload` itself — so an operator sending SIGHUP always
        # gets a clear log line either way (a forced reload attempt when hot-
        # reload is enabled, or an explicit "not enabled" note when it isn't)
        # rather than a silent, unexplained no-op.
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, self._handle_sighup)

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

    def _log_startup_paths_once(self) -> None:
        """Log this process's resolved, absolute startup paths (bug-debt
        fix). Split out from `run()` as its own method so it can be tested
        directly — `run()` itself registers real, process-wide signal
        handlers before entering its blocking loop, which makes actually
        invoking `run()` from a unit test a side-effect risk (see
        `tests/test_scheduler_daemon.py`'s `TestSchedulerDaemonStartupPath
        Logging` for the full rationale)."""
        from hivepilot.config import settings
        from hivepilot.utils.startup_paths import log_resolved_startup_paths

        log_resolved_startup_paths(settings)

    def _handle_signal(self, signum: int, frame: Any) -> None:  # noqa: ANN401
        logger.info("scheduler_daemon.signal_received", extra={"signum": signum})
        self._stop_event.set()

    def _handle_sighup(self, signum: int, frame: Any) -> None:  # noqa: ANN401
        """Classic reload idiom: SIGHUP forces an immediate `reload()`
        attempt on the daemon's dedicated hot-reload manager, bypassing the
        `plugins_changed_on_disk()` gate the regular tick uses (`_maybe_hot_
        reload_plugins`). A no-op (logged) when `plugins_hot_reload` is off.

        Phase 14c (#249) — ALSO force-reloads roles.yaml via
        `roles.refresh_roles()`, unconditionally (not gated by
        `plugins_hot_reload`/`config_hot_reload` — an explicit SIGHUP is
        always an explicit reload request, same as `hivepilot reload` /
        `POST /v1/admin/reload`). Projects/tasks/pipelines are already hot
        in this daemon (fresh `Orchestrator()` per tick), so there is
        nothing else to reload here. Runs on the signal-handling thread
        (single-threaded, never a worker thread) — matches `refresh_roles()`'s
        own "safe reload point" contract.
        """
        logger.info("scheduler_daemon.sighup_received")
        self._reload_roles()
        manager = self._ensure_hot_reload_manager()
        if manager is None:
            return
        self._apply_reload(manager)

    def _reload_roles(self) -> None:
        """Call `roles.refresh_roles()` and log the outcome. Never raises —
        `refresh_roles()` already fails closed internally (keeps the
        previous roles config on any load error), but this wrapper is
        defensive against an unexpected exception so a roles reload can
        never crash the tick / SIGHUP handler."""
        from hivepilot import roles

        try:
            ok = roles.refresh_roles()
        except Exception:  # noqa: BLE001 — must never crash the tick/handler
            logger.exception("scheduler_daemon.roles_reload_error")
            return
        if ok:
            logger.info("scheduler_daemon.roles_reloaded")
        else:
            logger.warning("scheduler_daemon.roles_reload_failed")

    def _tick(self) -> None:
        self._maybe_hot_reload_plugins()
        self._maybe_hot_reload_roles()
        self._run_due_schedules()
        self._run_drift_scans()
        self._expire_stale_retries()
        self._process_deferred_rows()
        self._process_backoff_retries()

    def _maybe_hot_reload_roles(self) -> None:
        """Phase 14c (#249) — opt-in AUTOMATIC per-tick roles.yaml reload,
        gated by `settings.config_hot_reload` (default OFF — byte-identical
        scheduler behavior otherwise). Independent of `plugins_hot_reload`;
        explicit reload (SIGHUP / CLI / admin endpoint) is always available
        regardless of this flag."""
        from hivepilot.config import settings

        if not settings.config_hot_reload:
            return
        self._reload_roles()

    # ------------------------------------------------------------------
    # Phase 26b — plugin hot-reload
    # ------------------------------------------------------------------

    def _ensure_hot_reload_manager(self) -> PluginManager | None:
        """Return the daemon's dedicated hot-reload `PluginManager`,
        constructing it lazily on first call. Returns `None` (and never
        constructs anything) when `settings.plugins_hot_reload` is off — the
        default, byte-identical-behavior state.

        The FIRST call after enabling just captures a fresh baseline (there
        is nothing meaningful to diff against yet), so it returns `None` too
        — callers that need "an existing, ready-to-reload manager" check for
        that; `_handle_sighup` treats a fresh construction as satisfying the
        "force a reload" intent (the freshly-constructed manager already
        reflects current disk state).
        """
        from hivepilot.config import settings

        if not settings.plugins_hot_reload:
            return None
        if self._hot_reload_manager is None:
            from hivepilot.plugins import PluginManager

            self._hot_reload_manager = PluginManager()
            logger.info("scheduler_daemon.hot_reload_enabled")
            return None
        return self._hot_reload_manager

    def _apply_reload(self, manager: PluginManager) -> None:
        """Call `manager.reload()` and log the outcome. Never raises — a
        failing reload (whether `reload()` itself raises, which it should
        not per its own contract, or returns `ok=False`) must only log, per
        Phase 26b's "never crash the tick" requirement.
        """
        try:
            result: ReloadResult = manager.reload()
        except Exception:  # noqa: BLE001 — hot-reload must never crash the tick
            logger.exception("scheduler_daemon.hot_reload_error")
            return
        if result.ok:
            logger.info(
                "scheduler_daemon.hot_reload_applied",
                extra={"added": result.added, "removed": result.removed, "updated": result.updated},
            )
        else:
            logger.warning("scheduler_daemon.hot_reload_failed", extra={"error": result.error})

    def _maybe_hot_reload_plugins(self) -> None:
        """Opt-in, mtime-gated hot-reload check, run once per tick. A failing
        `plugins_changed_on_disk()` call or `reload()` call only logs — it
        must never crash the tick (schedules/deferred-rows still run after).
        """
        manager = self._ensure_hot_reload_manager()
        if manager is None:
            return
        try:
            changed = manager.plugins_changed_on_disk()
        except Exception:  # noqa: BLE001 — hot-reload must never crash the tick
            logger.exception("scheduler_daemon.hot_reload_error")
            return
        if not changed:
            return
        self._apply_reload(manager)

    def _run_due_schedules(self) -> None:
        try:
            schedules = due_schedules()
        except Exception:  # noqa: BLE001
            logger.exception("scheduler_daemon.due_schedules_error")
            return
        if not schedules:
            return
        # Phase 26b — inject the daemon's shared hot-reload manager (`None`
        # when `plugins_hot_reload` is off, which is IDENTICAL to calling
        # `Orchestrator()` with no args: the default path is byte-for-byte
        # unchanged). See `Orchestrator.__init__`'s docstring for why a
        # fresh, un-injected `PluginManager()` here would self-collide once
        # hot-reload is on.
        orch = Orchestrator(plugins=self._hot_reload_manager)
        for sched in schedules:
            try:
                run_entry(sched, orch)
            except Exception:  # noqa: BLE001
                logger.exception("scheduler_daemon.run_entry_error", extra={"schedule": sched})

    def _run_drift_scans(self) -> None:
        """Phase 20 D3 — scan due IaC projects for drift and alert.

        Cheap no-op when disabled (the common case): `load_drift_config`
        early-returns a disabled default without touching the state DB. Each
        due project's scan is wrapped in its own try/except so one project's
        failure (or a bug in `run_drift_scan` itself) can never stop the tick
        or block the remaining due projects / the deferred-row pass below.

        `due_drift_projects`/`load_drift_config`/`run_drift_scan` are
        imported LOCALLY (not at module level) deliberately: the existing
        `tests/test_drift_schedule.py` suite patches these by name on
        `hivepilot.services.drift_schedule` (`patch("hivepilot.services.
        drift_schedule.run_drift_scan", ...)`) around a call to
        `daemon._run_drift_scans()` -- a module-level `from ... import ...`
        here would bind a reference at IMPORT time that a later patch on the
        SOURCE module can no longer reach ("patch where used, not where
        defined"), silently un-mocking every one of those tests.
        """
        from hivepilot.services.drift_schedule import (
            due_drift_projects,
            load_drift_config,
            run_drift_scan,
        )

        cfg = load_drift_config()
        if not cfg.enabled:
            return
        try:
            due = due_drift_projects(cfg)
        except Exception:  # noqa: BLE001
            logger.exception("scheduler_daemon.due_drift_projects_error")
            return
        for project_name in due:
            try:
                # Phase 26b — thread the shared hot-reload manager through
                # to `_attempt_remediation`'s `Orchestrator()` construction
                # (only reachable when `cfg.auto_remediate` is on AND drift
                # was detected). Same rationale as `_run_due_schedules` above.
                run_drift_scan(cfg, project_name, plugins=self._hot_reload_manager)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "scheduler_daemon.run_drift_scan_error", extra={"project": project_name}
                )

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
            # Phase 26b — see `_run_due_schedules` above for why the shared
            # hot-reload manager (`None` when opt-in is off) is injected here.
            orch = Orchestrator(plugins=self._hot_reload_manager)
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

    # ------------------------------------------------------------------
    # fix/retry-queue-drain -- expiry + the plain backoff-retry drain
    # ------------------------------------------------------------------

    def _expire_stale_retries(self) -> None:
        """Mark PENDING `retry_queue` rows older than `settings.
        retry_queue_ttl_days` as `expired` (never dispatched). Never
        silent: every expired row is logged at WARNING with enough context
        (id/schedule/task/error/age) for an operator to see exactly what
        was dropped and why -- 'expiry must be visible, never silent'.
        A failure here must never block the rest of the tick (schedule
        dispatch, drift scans, the two retry-queue drains below all still
        run) -- matches `_run_due_schedules`/`_run_drift_scans`'s existing
        catch-and-log discipline.
        """
        from hivepilot.config import settings
        from hivepilot.services import retry_service

        try:
            expired = retry_service.expire_stale(settings.retry_queue_ttl_days)
        except Exception:  # noqa: BLE001
            logger.exception("scheduler_daemon.expire_stale_retries_error")
            return

        for row in expired:
            logger.warning(
                "scheduler_daemon.retry_expired",
                extra={
                    "row_id": row.get("id"),
                    "schedule_name": row.get("schedule_name"),
                    "task": row.get("task"),
                    "created_at": row.get("created_at"),
                    "error": (row.get("error") or "")[:200],
                },
            )

    def _process_backoff_retries(self) -> None:
        """Drain AT MOST ONE plain backoff-retry row per tick -- the rows
        `retry_service.enqueue()` writes on an ordinary schedule task
        failure (`schedule_service.run_entry`'s except branch). This is the
        subtype `_process_deferred_rows` above deliberately never touches
        (it filters `context IS NOT NULL`); before this method existed,
        NOTHING ever drained it -- see the retry-queue-drain incident this
        module's docstring describes.

        Bounded to one row per tick for the same reason `autopilot_queue.
        drain_one()` is bounded to one objective per tick: a backlog must
        never fire N runs at once (the exact risk a 197-row backlog would
        have posed had the drain simply been un-gated instead of fixed
        properly).
        """
        from hivepilot.services import retry_service

        try:
            due = retry_service.due_backoff_rows(limit=1)
        except Exception:  # noqa: BLE001
            logger.exception("scheduler_daemon.due_backoff_rows_error")
            return
        if not due:
            return

        row = due[0]
        if not retry_service.claim_row(row["id"]):
            # Lost the claim race (or another process already handled it) --
            # simply try again next tick rather than forcing it.
            return
        self._rerun_backoff_row(row)

    def _rerun_backoff_row(self, row: dict) -> None:
        from hivepilot.services import retry_service

        row_id: int = row["id"]
        task_name: str | None = row.get("task")
        project_names: list[str] = json.loads(row.get("projects") or "[]")
        attempt: int = int(row.get("attempt", 0))
        max_attempts: int = int(row.get("max_attempts", 3))

        logger.info(
            "scheduler_daemon.rerun_backoff",
            extra={"row_id": row_id, "task": task_name, "projects": project_names},
        )
        try:
            orch = Orchestrator(plugins=self._hot_reload_manager)
            orch.run_task(
                task_name=task_name,
                project_names=project_names,
                extra_prompt=None,
                auto_git=False,
            )
        except Exception as exc:  # noqa: BLE001 -- never silently swallowed: the row's
            # own status always reflects the outcome (pending/dead), and every branch
            # below logs -- "a drain that silently does nothing is worse than no drain".
            exc_str = str(exc).lower()
            is_quota = any(phrase in exc_str for phrase in _QUOTA_PHRASES)
            new_attempt = attempt + 1

            if is_quota:
                next_at = datetime.now(timezone.utc) + timedelta(minutes=30)
                retry_service.mark_retry(row_id, attempt=new_attempt, next_retry_at=next_at)
                logger.warning(
                    "scheduler_daemon.rerun_backoff_quota_again",
                    extra={"row_id": row_id, "next_retry_at": next_at.isoformat()},
                )
            elif new_attempt >= max_attempts:
                retry_service.mark_dead(row_id, attempt=new_attempt)
                logger.error(
                    "scheduler_daemon.rerun_backoff_dead",
                    extra={"row_id": row_id, "error": str(exc)},
                )
            else:
                # Same exponential-backoff shape as retry_service.enqueue()'s
                # own formula (base_delay_minutes defaults to 2 there too).
                next_at = datetime.now(timezone.utc) + timedelta(
                    minutes=2 * (2 ** max(new_attempt - 1, 0))
                )
                retry_service.mark_retry(row_id, attempt=new_attempt, next_retry_at=next_at)
                logger.warning(
                    "scheduler_daemon.rerun_backoff_failed",
                    extra={"row_id": row_id, "attempt": new_attempt, "error": str(exc)},
                )
        else:
            retry_service.mark_done(row_id)
            logger.info("scheduler_daemon.rerun_backoff_done", extra={"row_id": row_id})
