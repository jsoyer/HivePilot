"""Tests for hivepilot.services.scheduler_daemon.SchedulerDaemon."""

from __future__ import annotations

import json
import signal
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_db(tmp_path: Path) -> Path:
    """Create a minimal retry_queue table in a temp DB."""
    db = tmp_path / "state.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """
            CREATE TABLE retry_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_name TEXT, task TEXT, projects TEXT, error TEXT,
                attempt INTEGER, max_attempts INTEGER, status TEXT DEFAULT 'pending',
                next_retry_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                context TEXT
            )
            """
        )
        conn.commit()
    return db


def _insert_deferred_row(
    db: Path,
    *,
    next_retry_at: datetime,
    ctx: dict,
    attempt: int = 0,
    max_attempts: int = 3,
    status: str = "pending",
) -> int:
    with sqlite3.connect(str(db)) as conn:
        cur = conn.execute(
            "INSERT INTO retry_queue "
            "(schedule_name, task, projects, error, attempt, max_attempts, status, next_retry_at, context) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "quota-deferred",
                ctx.get("task", "dev"),
                json.dumps(["repo-x"]),
                "quota exceeded",
                attempt,
                max_attempts,
                status,
                next_retry_at.isoformat(),
                json.dumps(ctx),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)  # type: ignore[arg-type]


class TestSchedulerDaemonDeferredProcessing:
    """Tests for the deferred-row re-run logic."""

    def test_due_deferred_row_is_rerun(self, tmp_path, monkeypatch):
        """A past-due deferred row is picked up and run_task is called."""
        db = _make_db(tmp_path)

        import hivepilot.services.state_service as svc

        monkeypatch.setattr(svc, "DB_PATH", str(db))
        monkeypatch.setattr(svc, "init_db", lambda: None)

        ctx = {"task": "dev", "extra_prompt": "fix it", "auto_git": False}
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        row_id = _insert_deferred_row(db, next_retry_at=past, ctx=ctx)

        run_task_calls: list[dict] = []
        mock_orch = MagicMock()
        mock_orch.run_task.side_effect = lambda **kw: run_task_calls.append(kw)

        with patch("hivepilot.services.scheduler_daemon.Orchestrator", return_value=mock_orch):
            from hivepilot.services.scheduler_daemon import SchedulerDaemon

            daemon = SchedulerDaemon()
            daemon._process_deferred_rows()

        assert len(run_task_calls) == 1
        assert run_task_calls[0]["task_name"] == "dev"
        assert run_task_calls[0]["project_names"] == ["repo-x"]
        assert run_task_calls[0]["extra_prompt"] == "fix it"

        with sqlite3.connect(str(db)) as conn:
            row = conn.execute("SELECT status FROM retry_queue WHERE id=?", (row_id,)).fetchone()
        assert row[0] == "done"

    def test_future_deferred_row_is_skipped(self, tmp_path, monkeypatch):
        """A deferred row that is not yet due is NOT processed."""
        db = _make_db(tmp_path)

        import hivepilot.services.state_service as svc

        monkeypatch.setattr(svc, "DB_PATH", str(db))
        monkeypatch.setattr(svc, "init_db", lambda: None)

        ctx = {"task": "dev", "extra_prompt": None, "auto_git": False}
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        row_id = _insert_deferred_row(db, next_retry_at=future, ctx=ctx)

        run_task_calls: list[dict] = []
        mock_orch = MagicMock()
        mock_orch.run_task.side_effect = lambda **kw: run_task_calls.append(kw)

        with patch("hivepilot.services.scheduler_daemon.Orchestrator", return_value=mock_orch):
            from hivepilot.services.scheduler_daemon import SchedulerDaemon

            daemon = SchedulerDaemon()
            daemon._process_deferred_rows()

        assert len(run_task_calls) == 0

        with sqlite3.connect(str(db)) as conn:
            row = conn.execute("SELECT status FROM retry_queue WHERE id=?", (row_id,)).fetchone()
        assert row[0] == "pending"

    def test_deferred_row_without_context_is_skipped(self, tmp_path, monkeypatch):
        """Legacy retry rows (no context) are not picked up by deferred processing."""
        db = _make_db(tmp_path)

        import hivepilot.services.state_service as svc

        monkeypatch.setattr(svc, "DB_PATH", str(db))
        monkeypatch.setattr(svc, "init_db", lambda: None)

        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "INSERT INTO retry_queue "
                "(schedule_name, task, projects, error, attempt, max_attempts, status, next_retry_at, context) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "nightly",
                    "dev",
                    json.dumps(["repo-y"]),
                    "err",
                    1,
                    3,
                    "pending",
                    past.isoformat(),
                    None,
                ),
            )
            conn.commit()

        run_task_calls: list[dict] = []
        mock_orch = MagicMock()
        mock_orch.run_task.side_effect = lambda **kw: run_task_calls.append(kw)

        with patch("hivepilot.services.scheduler_daemon.Orchestrator", return_value=mock_orch):
            from hivepilot.services.scheduler_daemon import SchedulerDaemon

            daemon = SchedulerDaemon()
            daemon._process_deferred_rows()

        assert len(run_task_calls) == 0

    def test_deferred_row_quota_again_reschedules(self, tmp_path, monkeypatch):
        """If re-run hits quota again, the row is rescheduled (not marked dead)."""
        db = _make_db(tmp_path)

        import hivepilot.services.state_service as svc

        monkeypatch.setattr(svc, "DB_PATH", str(db))
        monkeypatch.setattr(svc, "init_db", lambda: None)

        ctx = {"task": "dev", "extra_prompt": None, "auto_git": False}
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        row_id = _insert_deferred_row(db, next_retry_at=past, ctx=ctx, attempt=0, max_attempts=3)

        mock_orch = MagicMock()
        mock_orch.run_task.side_effect = Exception("session limit exceeded — resets 3:00pm (UTC)")

        with patch("hivepilot.services.scheduler_daemon.Orchestrator", return_value=mock_orch):
            from hivepilot.services.scheduler_daemon import SchedulerDaemon

            daemon = SchedulerDaemon()
            daemon._process_deferred_rows()

        with sqlite3.connect(str(db)) as conn:
            row = conn.execute(
                "SELECT status, attempt FROM retry_queue WHERE id=?", (row_id,)
            ).fetchone()
        # Status stays pending (rescheduled), attempt incremented
        assert row[0] == "pending"
        assert row[1] == 1

    def test_deferred_row_non_quota_failure_increments_attempt(self, tmp_path, monkeypatch):
        """Non-quota failure increments attempt; row becomes dead after max_attempts."""
        db = _make_db(tmp_path)

        import hivepilot.services.state_service as svc

        monkeypatch.setattr(svc, "DB_PATH", str(db))
        monkeypatch.setattr(svc, "init_db", lambda: None)

        ctx = {"task": "dev", "extra_prompt": None, "auto_git": False}
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        row_id = _insert_deferred_row(db, next_retry_at=past, ctx=ctx, attempt=2, max_attempts=3)

        mock_orch = MagicMock()
        mock_orch.run_task.side_effect = RuntimeError("connection refused")

        with patch("hivepilot.services.scheduler_daemon.Orchestrator", return_value=mock_orch):
            from hivepilot.services.scheduler_daemon import SchedulerDaemon

            daemon = SchedulerDaemon()
            daemon._process_deferred_rows()

        with sqlite3.connect(str(db)) as conn:
            row = conn.execute(
                "SELECT status, attempt FROM retry_queue WHERE id=?", (row_id,)
            ).fetchone()
        assert row[0] == "dead"
        assert row[1] == 3


class TestSchedulerDaemonHotReload:
    """Phase 26b — opt-in (`settings.plugins_hot_reload`) mtime-based plugin
    hot-reload wiring on `SchedulerDaemon`. Uses the daemon's OWN dedicated,
    long-lived `PluginManager` (`daemon._hot_reload_manager`), NOT the ad-hoc
    one each per-schedule/per-deferred-row `Orchestrator()` construction
    builds fresh (see `scheduler_daemon.py` module/method docstrings) — the
    real lifecycle finding from Phase 26b Step 0.
    """

    def _write_runner_plugin(self, plugin_dir, filename: str, kind: str) -> None:
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / filename).write_text(
            "class FixtureRunner:\n"
            "    def __init__(self, definition, settings):\n        pass\n"
            "    def run(self, payload):\n        return None\n"
            f"def register():\n    return {{'runners': {{'{kind}': FixtureRunner}}}}\n",
            encoding="utf-8",
        )

    def test_opt_in_off_never_constructs_hot_reload_manager(self, monkeypatch) -> None:
        from hivepilot.config import settings
        from hivepilot.services.scheduler_daemon import SchedulerDaemon

        monkeypatch.setattr(settings, "plugins_hot_reload", False, raising=False)

        daemon = SchedulerDaemon()
        assert daemon._hot_reload_manager is None

        daemon._maybe_hot_reload_plugins()

        assert daemon._hot_reload_manager is None

    def test_opt_in_on_lazily_constructs_then_reloads_on_change(
        self, tmp_path, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.config import settings
        from hivepilot.registry import RUNNER_MAP
        from hivepilot.services.scheduler_daemon import SchedulerDaemon

        monkeypatch.setattr(settings, "plugins_hot_reload", True, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        daemon = SchedulerDaemon()
        assert daemon._hot_reload_manager is None

        # First call: lazily constructs the dedicated manager (baseline
        # snapshot only, nothing to reload yet since there's no prior state).
        daemon._maybe_hot_reload_plugins()
        assert daemon._hot_reload_manager is not None

        # A plugin file appears on disk after the baseline was captured.
        self._write_runner_plugin(tmp_path / "plugins", "a.py", kind="fixture-daemon-a")

        daemon._maybe_hot_reload_plugins()

        assert "fixture-daemon-a" in RUNNER_MAP

    def test_failing_reload_does_not_crash_tick(self, monkeypatch) -> None:
        from hivepilot.config import settings
        from hivepilot.plugins import ReloadResult
        from hivepilot.services.scheduler_daemon import SchedulerDaemon

        monkeypatch.setattr(settings, "plugins_hot_reload", True, raising=False)

        daemon = SchedulerDaemon()
        daemon._maybe_hot_reload_plugins()  # lazily construct
        assert daemon._hot_reload_manager is not None

        bad_manager = MagicMock()
        bad_manager.plugins_changed_on_disk.return_value = True
        bad_manager.reload.return_value = ReloadResult(ok=False, error="boom")
        daemon._hot_reload_manager = bad_manager

        daemon._maybe_hot_reload_plugins()  # must not raise

        bad_manager.reload.side_effect = RuntimeError("kaboom")
        daemon._maybe_hot_reload_plugins()  # must not raise even on a raising reload()

    def test_sighup_forces_reload_regardless_of_disk_change(self, monkeypatch) -> None:
        from hivepilot.config import settings
        from hivepilot.plugins import ReloadResult
        from hivepilot.services.scheduler_daemon import SchedulerDaemon

        monkeypatch.setattr(settings, "plugins_hot_reload", True, raising=False)

        daemon = SchedulerDaemon()
        daemon._maybe_hot_reload_plugins()  # lazily construct
        manager = daemon._hot_reload_manager
        assert manager is not None

        manager.plugins_changed_on_disk = MagicMock(return_value=False)  # type: ignore[method-assign]
        manager.reload = MagicMock(return_value=ReloadResult(ok=True))  # type: ignore[method-assign]

        daemon._handle_sighup(signal.SIGHUP, None)

        manager.reload.assert_called_once()

    def test_sighup_is_noop_and_logged_when_opt_in_off(self, monkeypatch) -> None:
        from hivepilot.config import settings
        from hivepilot.services.scheduler_daemon import SchedulerDaemon

        monkeypatch.setattr(settings, "plugins_hot_reload", False, raising=False)

        daemon = SchedulerDaemon()
        daemon._handle_sighup(signal.SIGHUP, None)  # must not raise

        assert daemon._hot_reload_manager is None
