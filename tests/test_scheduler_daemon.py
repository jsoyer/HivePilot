"""Tests for hivepilot.services.scheduler_daemon.SchedulerDaemon."""

from __future__ import annotations

import json
import signal
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
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


def _tracked_true(calls: list[int]) -> Any:
    """Return a zero-arg callable that records a call then returns True --
    avoids `lambda: calls.append(1) or True` (mypy flags `list.append`'s
    `None` return used in a boolean `or` expression as `func-returns-value`)."""

    def _fn() -> bool:
        calls.append(1)
        return True

    return _fn


class TestSchedulerDaemonRolesHotReload:
    """Phase 14c (#249) — SIGHUP always force-reloads roles.yaml
    (unconditionally, unlike plugin hot-reload); the per-tick reload is
    opt-in via `settings.config_hot_reload`."""

    def test_sighup_calls_refresh_roles(self, monkeypatch) -> None:
        from hivepilot import roles as roles_mod
        from hivepilot.config import settings
        from hivepilot.services.scheduler_daemon import SchedulerDaemon

        # SIGHUP's roles reload is unconditional -- prove it fires even with
        # plugin hot-reload off (the two are independent).
        monkeypatch.setattr(settings, "plugins_hot_reload", False, raising=False)
        calls: list[int] = []
        monkeypatch.setattr(roles_mod, "refresh_roles", _tracked_true(calls))

        daemon = SchedulerDaemon()
        daemon._handle_sighup(signal.SIGHUP, None)

        assert calls == [1]

    def test_sighup_roles_reload_failure_does_not_crash(self, monkeypatch) -> None:
        from hivepilot import roles as roles_mod
        from hivepilot.services.scheduler_daemon import SchedulerDaemon

        def _boom():
            raise RuntimeError("bad yaml")

        monkeypatch.setattr(roles_mod, "refresh_roles", _boom)

        daemon = SchedulerDaemon()
        daemon._handle_sighup(signal.SIGHUP, None)  # must not raise

    def test_tick_skips_roles_reload_when_flag_off(self, monkeypatch) -> None:
        from hivepilot import roles as roles_mod
        from hivepilot.config import settings
        from hivepilot.services.scheduler_daemon import SchedulerDaemon

        monkeypatch.setattr(settings, "config_hot_reload", False, raising=False)
        calls: list[int] = []
        monkeypatch.setattr(roles_mod, "refresh_roles", _tracked_true(calls))

        daemon = SchedulerDaemon()
        daemon._maybe_hot_reload_roles()

        assert calls == []

    def test_tick_calls_refresh_roles_when_flag_on(self, monkeypatch) -> None:
        from hivepilot import roles as roles_mod
        from hivepilot.config import settings
        from hivepilot.services.scheduler_daemon import SchedulerDaemon

        monkeypatch.setattr(settings, "config_hot_reload", True, raising=False)
        calls: list[int] = []
        monkeypatch.setattr(roles_mod, "refresh_roles", _tracked_true(calls))

        daemon = SchedulerDaemon()
        daemon._maybe_hot_reload_roles()

        assert calls == [1]


class TestSchedulerDaemonSharedManagerInjection:
    """MUST-FIX (adversarial review): when `plugins_hot_reload` is ON, the
    daemon's dedicated hot-reload `PluginManager` registers runner/notifier/
    secrets kinds into the process-global maps. If `_run_due_schedules` /
    `_rerun_deferred_row` each construct a FRESH, un-injected `Orchestrator()`
    (-> a fresh `PluginManager()` with empty ownership), that fresh manager
    re-scans the SAME `plugins/*.py`, sees those kinds already live but NOT
    owned by it, and raises a collision -- breaking dispatch every tick for
    the rest of the process's life. The fix: inject the daemon's ONE shared
    manager into every `Orchestrator()` it constructs
    (`Orchestrator(plugins=self._hot_reload_manager)`) so exactly one
    PluginManager ever registers into the globals.

    These tests exercise the REAL (unmocked) `Orchestrator()` construction
    path -- not `patch("...Orchestrator", ...)` like the deferred-row tests
    above -- since the bug lives inside `PluginManager.__init__`, which a
    mocked `Orchestrator` would hide entirely.
    """

    def _write_minimal_valid_config(self, base_dir) -> None:
        """`Orchestrator._load()` reads `projects.yaml`/`tasks.yaml` (both
        REQUIRE their top-level key per `ProjectsFile`/`TasksFile` -- an
        empty/missing file fails pydantic validation) -- write the smallest
        schema-valid files so real `Orchestrator()` construction succeeds
        far enough to reach `PluginManager()` (the thing under test here),
        without needing any real project/task content.
        """
        (base_dir / "projects.yaml").write_text("projects: {}\n", encoding="utf-8")
        (base_dir / "tasks.yaml").write_text("tasks: {}\n", encoding="utf-8")
        (base_dir / "pipelines.yaml").write_text("pipelines: {}\n", encoding="utf-8")

    def _write_combo_plugin(self, plugin_dir, filename: str, kind: str) -> None:
        """A plugin contributing a RUNNER + NOTIFIER + SECRETS kind together
        -- the real production shape (1Password/Infisical/bitwarden/
        vaultwarden-style secrets plugins, obsidian/rtk-style notifiers) that
        triggers the self-collision when a second, independent
        `PluginManager()` re-scans the same file.
        """
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / filename).write_text(
            "class FixtureRunner:\n"
            "    def __init__(self, definition, settings):\n        pass\n"
            "    def run(self, payload):\n        return None\n"
            "def _notify(msg):\n    pass\n"
            "class _Secrets:\n"
            "    def resolve(self, ref, settings):\n        return 'x'\n"
            "def register():\n"
            f"    return {{'runners': {{'{kind}': FixtureRunner}}, "
            f"'notifiers': {{'{kind}': _notify}}, "
            f"'secrets': {{'{kind}': _Secrets()}}}}\n",
            encoding="utf-8",
        )

    def _spy_orchestrator(self, daemon_mod, monkeypatch):
        """Wrap the REAL `Orchestrator` class so we can capture the kwargs it
        was constructed with AND detect whether construction itself raised
        (e.g. a `*KindCollisionError` from a self-colliding `PluginManager()`)
        -- without needing that exception to propagate all the way out of
        the calling daemon method, since some call sites (`_rerun_deferred_row`)
        wrap the `Orchestrator()` call in their own broad `except Exception`.
        """
        real_cls = daemon_mod.Orchestrator
        captured: dict[str, Any] = {"kwargs": None, "raised": False}

        def _spy(*args: Any, **kwargs: Any) -> Any:
            captured["kwargs"] = kwargs
            try:
                return real_cls(*args, **kwargs)
            except Exception:
                captured["raised"] = True
                raise

        monkeypatch.setattr(daemon_mod, "Orchestrator", _spy)
        return captured

    def test_due_schedule_dispatch_reuses_shared_manager_no_collision(
        self, tmp_path, monkeypatch
    ) -> None:
        """The exact production scenario: construct the daemon's shared
        hot-reload manager (registering a combo plugin's kinds), THEN run
        `_run_due_schedules` -- it must NOT raise a collision. This test
        FAILS on the pre-fix code (`orch = Orchestrator()`, ignoring the
        already-populated globals) and PASSES once `_run_due_schedules`
        injects `self._hot_reload_manager`.
        """
        from hivepilot import plugins as plugins_mod
        from hivepilot.config import settings
        from hivepilot.registry import RUNNER_MAP
        from hivepilot.services import scheduler_daemon as daemon_mod
        from hivepilot.services.schedule_service import ScheduleEntry

        monkeypatch.setattr(settings, "plugins_hot_reload", True, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        self._write_minimal_valid_config(tmp_path)
        self._write_combo_plugin(tmp_path / "plugins", "combo.py", kind="fixture-combo")

        daemon = daemon_mod.SchedulerDaemon()
        daemon._maybe_hot_reload_plugins()  # constructs + registers the ONE shared manager
        assert daemon._hot_reload_manager is not None
        assert "fixture-combo" in RUNNER_MAP

        fake_schedule = ScheduleEntry(name="s", task="dev", projects=[])
        monkeypatch.setattr(daemon_mod, "due_schedules", lambda: [fake_schedule])
        monkeypatch.setattr(daemon_mod, "run_entry", lambda entry, orch: True)
        captured = self._spy_orchestrator(daemon_mod, monkeypatch)

        daemon._run_due_schedules()  # must NOT raise a *KindCollisionError

        assert captured["raised"] is False
        assert captured["kwargs"] == {"plugins": daemon._hot_reload_manager}

    def test_deferred_row_dispatch_reuses_shared_manager_no_collision(
        self, tmp_path, monkeypatch
    ) -> None:
        """Same scenario as above, exercised via `_rerun_deferred_row` (the
        other real, un-injected `Orchestrator()` construction site named by
        the review). `_rerun_deferred_row` wraps `Orchestrator()` in its own
        broad `except Exception` (a failed re-run is a normal, expected
        outcome it retries/dead-letters) -- so the collision would otherwise
        be silently swallowed as "task rerun failed" instead of surfacing as
        the registry-integrity bug it actually is. The `_spy_orchestrator`
        wrapper detects that swallowed raise directly.
        """
        from hivepilot import plugins as plugins_mod
        from hivepilot.config import settings
        from hivepilot.registry import RUNNER_MAP
        from hivepilot.services import scheduler_daemon as daemon_mod

        monkeypatch.setattr(settings, "plugins_hot_reload", True, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        self._write_minimal_valid_config(tmp_path)
        self._write_combo_plugin(tmp_path / "plugins", "combo.py", kind="fixture-combo2")

        daemon = daemon_mod.SchedulerDaemon()
        daemon._maybe_hot_reload_plugins()
        assert daemon._hot_reload_manager is not None
        assert "fixture-combo2" in RUNNER_MAP

        captured = self._spy_orchestrator(daemon_mod, monkeypatch)

        row = {
            "id": 1,
            "task": "dev",
            "projects": "[]",
            "attempt": 0,
            "max_attempts": 3,
        }
        # `_rerun_deferred_row` writes to the state DB on completion --
        # isolated to a tmp DB by the conftest autouse `_isolate_state_db`
        # fixture, same as every other test in this module.
        daemon._rerun_deferred_row(row, {"task": "dev"})  # must not raise

        assert captured["raised"] is False
        assert captured["kwargs"] == {"plugins": daemon._hot_reload_manager}

    def test_drift_scan_remediation_reuses_shared_manager(self, tmp_path, monkeypatch) -> None:
        """`_run_drift_scans` threads the shared manager through
        `run_drift_scan` -> `_attempt_remediation` -> `Orchestrator(plugins=...)`.
        Verified at the thinnest possible seam: `run_drift_scan` itself is
        stubbed to capture its call kwargs (the deeper `Orchestrator`
        threading through `drift_schedule.py` is covered by
        `test_drift_schedule.py`'s own remediation tests).

        Patched on `hivepilot.services.drift_schedule` (the SOURCE module),
        not on `scheduler_daemon` -- `_run_drift_scans` imports these names
        LOCALLY on every call (see its docstring), matching the existing
        `test_drift_schedule.py` suite's own patch convention.
        """
        import hivepilot.services.drift_schedule as drift_schedule_mod
        from hivepilot.config import settings
        from hivepilot.services import scheduler_daemon as daemon_mod
        from hivepilot.services.drift_schedule import DriftScanConfig

        monkeypatch.setattr(settings, "plugins_hot_reload", True, raising=False)

        daemon = daemon_mod.SchedulerDaemon()
        daemon._maybe_hot_reload_plugins()  # lazily construct the shared manager
        manager = daemon._hot_reload_manager
        assert manager is not None

        cfg = DriftScanConfig(
            enabled=True,
            auto_remediate=True,
            remediate_task="remediate",
            channels=[],
        )
        monkeypatch.setattr(drift_schedule_mod, "due_drift_projects", lambda cfg: ["proj-x"])
        monkeypatch.setattr(drift_schedule_mod, "load_drift_config", lambda: cfg)

        captured_kwargs: dict = {}

        def _fake_run_drift_scan(cfg_arg, project_name, **kwargs):
            captured_kwargs.update(kwargs)

        monkeypatch.setattr(drift_schedule_mod, "run_drift_scan", _fake_run_drift_scan)

        daemon._run_drift_scans()

        assert captured_kwargs.get("plugins") is manager
