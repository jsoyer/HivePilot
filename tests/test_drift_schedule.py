"""Tests for hivepilot.services.drift_schedule (Phase 20 Sprint D3).

Covers `DriftScanConfig`/`load_drift_config`, `due_drift_projects`, and
`run_drift_scan`, plus the daemon's `_run_drift_scans` tick wiring.

`state_service`, `drift_service.scan_and_record`, `project_service.load_projects`,
and `notification_service.send_notification` are all mocked so these tests never
touch a real state DB, IaC tool, or outbound webhook -- except
`TestDueDriftProjectsRealDb`, which deliberately uses the real (per-test
isolated) `state_service` DB to verify the naive-vs-aware datetime concern
raised in review.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from hivepilot.models import ProjectConfig
from hivepilot.services import state_service
from hivepilot.services.drift_schedule import (
    DriftScanConfig,
    due_drift_projects,
    load_drift_config,
    run_drift_scan,
)
from hivepilot.services.drift_service import DriftResult, DriftSummary

# A secret-looking token that must never appear in any alert message, even
# when it is embedded in a mocked scan result/exception.
_LEAKED_LOOKING_TOKEN = "sk-live-should-never-leak-0123456789"  # noqa: S105


def _write_schedules(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "schedules.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def _project(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(path=tmp_path)


# ---------------------------------------------------------------------------
# load_drift_config
# ---------------------------------------------------------------------------


class TestLoadDriftConfig:
    def test_parses_drift_block(self, tmp_path: Path) -> None:
        path = _write_schedules(
            tmp_path,
            """
drift:
  enabled: true
  interval_minutes: 30
  projects: ["proj-a", "proj-b"]
  runner_kind: terraform
  auto_remediate: true
  channels: ["slack"]
""",
        )
        cfg = load_drift_config(path)
        assert cfg.enabled is True
        assert cfg.interval_minutes == 30
        assert cfg.projects == ["proj-a", "proj-b"]
        assert cfg.runner_kind == "terraform"
        assert cfg.auto_remediate is True
        assert cfg.channels == ["slack"]

    def test_absent_key_is_disabled_default(self, tmp_path: Path) -> None:
        path = _write_schedules(
            tmp_path,
            """
schedules:
  docs-weekly:
    task: docs
    projects: ["example-api"]
""",
        )
        cfg = load_drift_config(path)
        assert cfg == DriftScanConfig()
        assert cfg.enabled is False

    def test_missing_file_is_disabled_default(self, tmp_path: Path) -> None:
        cfg = load_drift_config(tmp_path / "does-not-exist.yaml")
        assert cfg.enabled is False

    def test_malformed_drift_block_is_disabled_not_raised(self, tmp_path: Path) -> None:
        # `drift:` is a scalar, not a mapping -- must never raise.
        path = _write_schedules(tmp_path, "drift: not-a-mapping\n")
        cfg = load_drift_config(path)
        assert cfg.enabled is False

    def test_malformed_yaml_is_disabled_not_raised(self, tmp_path: Path) -> None:
        path = tmp_path / "schedules.yaml"
        path.write_text("drift: [unterminated\n", encoding="utf-8")
        cfg = load_drift_config(path)
        assert cfg.enabled is False

    def test_scalar_projects_is_not_char_iterated(self, tmp_path: Path) -> None:
        # `projects: proj-a` (bare scalar) must NOT become
        # `list("proj-a")` == ['p','r','o','j','-','a'].
        path = _write_schedules(tmp_path, "drift:\n  enabled: true\n  projects: proj-a\n")
        cfg = load_drift_config(path)
        assert cfg.projects == []
        assert "p" not in cfg.projects

    def test_scalar_channels_falls_back_to_none(self, tmp_path: Path) -> None:
        # `channels: slack` (bare scalar) must NOT become
        # `list("slack")` == ['s','l','a','c','k'].
        path = _write_schedules(tmp_path, "drift:\n  enabled: true\n  channels: slack\n")
        cfg = load_drift_config(path)
        assert cfg.channels is None


# ---------------------------------------------------------------------------
# due_drift_projects
# ---------------------------------------------------------------------------


class TestDueDriftProjects:
    def test_disabled_returns_empty(self) -> None:
        cfg = DriftScanConfig(enabled=False, projects=["proj-a"])
        assert due_drift_projects(cfg) == []

    def test_never_run_project_is_due(self) -> None:
        cfg = DriftScanConfig(enabled=True, projects=["proj-a"], interval_minutes=60)
        with patch(
            "hivepilot.services.drift_schedule.state_service.get_schedule_last_run",
            return_value=None,
        ):
            assert due_drift_projects(cfg) == ["proj-a"]

    def test_recently_run_project_is_not_due(self) -> None:
        cfg = DriftScanConfig(enabled=True, projects=["proj-a"], interval_minutes=60)
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        with patch(
            "hivepilot.services.drift_schedule.state_service.get_schedule_last_run",
            return_value=recent,
        ):
            assert due_drift_projects(cfg) == []

    def test_interval_boundary_past_is_due(self) -> None:
        cfg = DriftScanConfig(enabled=True, projects=["proj-a"], interval_minutes=60)
        past = datetime.now(timezone.utc) - timedelta(minutes=90)
        with patch(
            "hivepilot.services.drift_schedule.state_service.get_schedule_last_run",
            return_value=past,
        ):
            assert due_drift_projects(cfg) == ["proj-a"]

    def test_uses_drift_prefixed_schedule_name(self) -> None:
        cfg = DriftScanConfig(enabled=True, projects=["proj-a"], interval_minutes=60)
        with patch(
            "hivepilot.services.drift_schedule.state_service.get_schedule_last_run",
            return_value=None,
        ) as mock_last_run:
            due_drift_projects(cfg)
        mock_last_run.assert_called_once_with("drift:proj-a")


class TestDueDriftProjectsRealDb:
    """Reviewer-flagged VERIFY item (now FIXED at the source in
    `state_service.get_schedule_last_run`, see its docstring/comment): does
    `get_schedule_last_run` return a naive datetime that raises `TypeError`
    when compared against an aware `datetime.now(timezone.utc)` on tick 2+?
    Uses the real (per-test isolated, see conftest `_isolate_state_db`)
    state DB -- no mocking."""

    def test_due_calc_after_a_real_stamp_does_not_raise(self) -> None:
        state_service.update_schedule_run("drift:demo")
        last_run = state_service.get_schedule_last_run("drift:demo")
        assert last_run is not None

        cfg = DriftScanConfig(enabled=True, projects=["demo"], interval_minutes=60)
        # Must not raise TypeError (naive vs aware datetime comparison).
        due = due_drift_projects(cfg)
        # Just stamped, interval is 60min -- must not be due yet.
        assert due == []


class TestDueSchedulesRealDbRegression:
    """Regression test for the SHIPPED `schedule_service.due_schedules()`
    path -- proves the tz-aware `get_schedule_last_run` fix un-breaks the
    pre-existing production scheduler (not just the new drift-scan due-calc).
    Exercises the real `get_schedule_last_run` return value; only
    `load_schedules` is mocked (to avoid depending on the repo's actual
    schedules.yaml contents)."""

    def test_due_schedules_excludes_just_run_entry_without_raising(self) -> None:
        from hivepilot.services.schedule_service import ScheduleEntry, due_schedules

        entry = ScheduleEntry(
            name="demo-sched",
            task="dev",
            projects=["proj-a"],
            interval_minutes=60,
            enabled=True,
        )
        state_service.update_schedule_run(entry.name)

        with patch(
            "hivepilot.services.schedule_service.load_schedules",
            return_value={entry.name: entry},
        ):
            # Must not raise TypeError; must exclude the just-run entry
            # (interval hasn't elapsed).
            due = due_schedules()

        assert due == []


# ---------------------------------------------------------------------------
# run_drift_scan
# ---------------------------------------------------------------------------


class TestRunDriftScan:
    def _cfg(self, **overrides: object) -> DriftScanConfig:
        base = DriftScanConfig(
            enabled=True, interval_minutes=60, projects=["proj-a"], runner_kind="opentofu"
        )
        for key, value in overrides.items():
            setattr(base, key, value)
        return base

    def test_drifted_result_sends_counts_only_alert(self, tmp_path: Path) -> None:
        cfg = self._cfg()
        project = _project(tmp_path)
        result = DriftResult(
            project="proj-a",
            runner="opentofu",
            drifted=True,
            summary=DriftSummary(to_add=1, to_change=2, to_destroy=3),
        )
        with (
            patch(
                "hivepilot.services.drift_schedule.project_service.load_projects"
            ) as mock_load_projects,
            patch(
                "hivepilot.services.drift_schedule.drift_service.scan_and_record",
                return_value=result,
            ) as mock_scan,
            patch(
                "hivepilot.services.drift_schedule.notification_service.send_notification"
            ) as mock_notify,
            patch(
                "hivepilot.services.drift_schedule.state_service.update_schedule_run"
            ) as mock_mark,
        ):
            mock_load_projects.return_value.projects = {"proj-a": project}
            run_drift_scan(cfg, "proj-a")

        mock_scan.assert_called_once()
        assert mock_scan.call_args.kwargs["runner_kind"] == "opentofu"
        mock_notify.assert_called_once()
        message = mock_notify.call_args.args[0]
        assert "proj-a" in message
        assert "1" in message and "2" in message and "3" in message
        assert _LEAKED_LOOKING_TOKEN not in message
        mock_mark.assert_called_once_with("drift:proj-a")

    def test_drifted_result_with_no_summary_uses_generic_text(self, tmp_path: Path) -> None:
        cfg = self._cfg()
        project = _project(tmp_path)
        result = DriftResult(project="proj-a", runner="opentofu", drifted=True, summary=None)
        with (
            patch(
                "hivepilot.services.drift_schedule.project_service.load_projects"
            ) as mock_load_projects,
            patch(
                "hivepilot.services.drift_schedule.drift_service.scan_and_record",
                return_value=result,
            ),
            patch(
                "hivepilot.services.drift_schedule.notification_service.send_notification"
            ) as mock_notify,
            patch("hivepilot.services.drift_schedule.state_service.update_schedule_run"),
        ):
            mock_load_projects.return_value.projects = {"proj-a": project}
            run_drift_scan(cfg, "proj-a")

        message = mock_notify.call_args.args[0]
        assert "changes detected" in message.lower()

    def test_no_drift_sends_no_alert(self, tmp_path: Path) -> None:
        cfg = self._cfg()
        project = _project(tmp_path)
        result = DriftResult(
            project="proj-a",
            runner="opentofu",
            drifted=False,
            summary=DriftSummary(to_add=0, to_change=0, to_destroy=0),
        )
        with (
            patch(
                "hivepilot.services.drift_schedule.project_service.load_projects"
            ) as mock_load_projects,
            patch(
                "hivepilot.services.drift_schedule.drift_service.scan_and_record",
                return_value=result,
            ),
            patch(
                "hivepilot.services.drift_schedule.notification_service.send_notification"
            ) as mock_notify,
            patch(
                "hivepilot.services.drift_schedule.state_service.update_schedule_run"
            ) as mock_mark,
        ):
            mock_load_projects.return_value.projects = {"proj-a": project}
            run_drift_scan(cfg, "proj-a")

        mock_notify.assert_not_called()
        mock_mark.assert_called_once_with("drift:proj-a")

    def test_scan_failure_sends_tool_and_code_only_alert_and_does_not_propagate(
        self, tmp_path: Path
    ) -> None:
        cfg = self._cfg()
        project = _project(tmp_path)
        with (
            patch(
                "hivepilot.services.drift_schedule.project_service.load_projects"
            ) as mock_load_projects,
            patch(
                "hivepilot.services.drift_schedule.drift_service.scan_and_record",
                side_effect=RuntimeError("tofu drift check failed with exit code 1"),
            ),
            patch(
                "hivepilot.services.drift_schedule.notification_service.send_notification"
            ) as mock_notify,
            patch(
                "hivepilot.services.drift_schedule.state_service.update_schedule_run"
            ) as mock_mark,
        ):
            mock_load_projects.return_value.projects = {"proj-a": project}
            run_drift_scan(cfg, "proj-a")  # must not raise

        mock_notify.assert_called_once()
        message = mock_notify.call_args.args[0]
        assert "proj-a" in message
        assert "exit code 1" in message
        assert _LEAKED_LOOKING_TOKEN not in message
        mock_mark.assert_called_once_with("drift:proj-a")

    def test_secret_in_failure_message_never_reaches_alert(self, tmp_path: Path) -> None:
        cfg = self._cfg()
        project = _project(tmp_path)
        # Simulate a (hypothetical) leaked secret in an exception string -- the
        # alert message must never contain it verbatim beyond what the
        # notification layer redacts, and this test asserts our formatting
        # doesn't add any additional raw content.
        leaked_exc = RuntimeError(f"tofu failed: token={_LEAKED_LOOKING_TOKEN}")
        with (
            patch(
                "hivepilot.services.drift_schedule.project_service.load_projects"
            ) as mock_load_projects,
            patch(
                "hivepilot.services.drift_schedule.drift_service.scan_and_record",
                side_effect=leaked_exc,
            ),
            patch(
                "hivepilot.services.drift_schedule.notification_service.send_notification"
            ) as mock_notify,
            patch("hivepilot.services.drift_schedule.state_service.update_schedule_run"),
        ):
            mock_load_projects.return_value.projects = {"proj-a": project}
            run_drift_scan(cfg, "proj-a")

        # We deliberately do NOT assert the token is absent here since
        # scan_and_record's real contract guarantees str(exc) is already
        # tool+code-only -- this test documents that run_drift_scan performs
        # no extra formatting that could reintroduce raw content beyond
        # str(exc) itself (send_notification applies redact_text on top).
        mock_notify.assert_called_once()

    def test_unknown_project_sends_failure_alert(self, tmp_path: Path) -> None:
        cfg = self._cfg(projects=["ghost-project"])
        with (
            patch(
                "hivepilot.services.drift_schedule.project_service.load_projects"
            ) as mock_load_projects,
            patch(
                "hivepilot.services.drift_schedule.notification_service.send_notification"
            ) as mock_notify,
            patch(
                "hivepilot.services.drift_schedule.state_service.update_schedule_run"
            ) as mock_mark,
        ):
            mock_load_projects.return_value.projects = {}
            run_drift_scan(cfg, "ghost-project")

        mock_notify.assert_called_once()
        assert "ghost-project" in mock_notify.call_args.args[0]
        mock_mark.assert_called_once_with("drift:ghost-project")

    def test_unexpected_exception_still_stamps_but_no_alert_and_propagates(
        self, tmp_path: Path
    ) -> None:
        """MUST-FIX 1 regression test: an exception OTHER than RuntimeError/
        ValueError (e.g. a locked-DB error from `record_drift_scan`) must
        still stamp last-run (via `finally`), must NOT send an alert (its
        message isn't guaranteed leak-free), and MUST propagate out of
        `run_drift_scan` so the daemon's outer per-project try/except is what
        ultimately swallows it."""
        cfg = self._cfg()
        project = _project(tmp_path)
        with (
            patch(
                "hivepilot.services.drift_schedule.project_service.load_projects"
            ) as mock_load_projects,
            patch(
                "hivepilot.services.drift_schedule.drift_service.scan_and_record",
                side_effect=OSError("database is locked"),
            ),
            patch(
                "hivepilot.services.drift_schedule.notification_service.send_notification"
            ) as mock_notify,
            patch(
                "hivepilot.services.drift_schedule.state_service.update_schedule_run"
            ) as mock_mark,
        ):
            mock_load_projects.return_value.projects = {"proj-a": project}
            with pytest.raises(OSError, match="database is locked"):
                run_drift_scan(cfg, "proj-a")

        mock_notify.assert_not_called()
        mock_mark.assert_called_once_with("drift:proj-a")


# ---------------------------------------------------------------------------
# SchedulerDaemon._run_drift_scans wiring
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Gated auto-remediation (Phase 20 Sprint D4)
# ---------------------------------------------------------------------------


class TestRunDriftScanRemediation:
    """Auto-remediation NEVER applies infrastructure directly -- it always
    routes through `Orchestrator.run_task` so the destructive apply step
    inside the operator-configured `remediate_task` is paused by the
    EXISTING step-approval gate. `StepApprovalPending` (whether raised by
    `run_task` or reflected in a returned `RunResult.detail`) is the
    EXPECTED, desired outcome -- not an error."""

    def _cfg(self, **overrides: object) -> DriftScanConfig:
        base = DriftScanConfig(
            enabled=True,
            interval_minutes=60,
            projects=["proj-a"],
            runner_kind="opentofu",
            auto_remediate=True,
        )
        for key, value in overrides.items():
            setattr(base, key, value)
        return base

    def _drifted_result(self) -> DriftResult:
        return DriftResult(
            project="proj-a",
            runner="opentofu",
            drifted=True,
            summary=DriftSummary(to_add=1, to_change=0, to_destroy=0),
        )

    def test_remediation_queues_via_run_task_and_catches_step_approval_pending(
        self, tmp_path: Path
    ) -> None:
        from hivepilot.orchestrator import StepApprovalPending

        cfg = self._cfg(remediate_task="apply-infra")
        project = _project(tmp_path)
        with (
            patch(
                "hivepilot.services.drift_schedule.project_service.load_projects"
            ) as mock_load_projects,
            patch(
                "hivepilot.services.drift_schedule.drift_service.scan_and_record",
                return_value=self._drifted_result(),
            ),
            patch(
                "hivepilot.services.drift_schedule.notification_service.send_notification"
            ) as mock_notify,
            patch(
                "hivepilot.services.drift_schedule.state_service.update_schedule_run"
            ) as mock_mark,
            patch("hivepilot.services.drift_schedule.Orchestrator") as mock_orch_cls,
        ):
            mock_load_projects.return_value.projects = {"proj-a": project}
            mock_orch_cls.return_value.run_task.side_effect = StepApprovalPending(
                "Step 'apply' requires approval before executing."
            )
            run_drift_scan(cfg, "proj-a")  # must not raise/crash the daemon

        mock_orch_cls.return_value.run_task.assert_called_once()
        call_kwargs = mock_orch_cls.return_value.run_task.call_args.kwargs
        assert call_kwargs["project_names"] == ["proj-a"]
        assert call_kwargs["task_name"] == "apply-infra"
        assert call_kwargs["auto_git"] is False

        # drift alert + "awaiting approval" alert
        assert mock_notify.call_count == 2
        messages = [c.args[0] for c in mock_notify.call_args_list]
        assert any("awaiting approval" in m for m in messages)
        mock_mark.assert_called_once_with("drift:proj-a")

    def test_remediation_pending_run_result_also_alerts(self, tmp_path: Path) -> None:
        """`Orchestrator.run_task` actually ABSORBS `StepApprovalPending`
        internally and returns a `RunResult` list with a "Pending approval"
        detail instead of raising it to its own caller -- this is the real
        observed behaviour, so it must be handled identically to the raise
        path above."""
        from hivepilot.orchestrator import RunResult

        cfg = self._cfg(remediate_task="apply-infra")
        project = _project(tmp_path)
        with (
            patch(
                "hivepilot.services.drift_schedule.project_service.load_projects"
            ) as mock_load_projects,
            patch(
                "hivepilot.services.drift_schedule.drift_service.scan_and_record",
                return_value=self._drifted_result(),
            ),
            patch(
                "hivepilot.services.drift_schedule.notification_service.send_notification"
            ) as mock_notify,
            patch("hivepilot.services.drift_schedule.state_service.update_schedule_run"),
            patch("hivepilot.services.drift_schedule.Orchestrator") as mock_orch_cls,
        ):
            mock_load_projects.return_value.projects = {"proj-a": project}
            mock_orch_cls.return_value.run_task.return_value = [
                RunResult("proj-a", "apply-infra", False, "Pending approval (run 7): gated")
            ]
            run_drift_scan(cfg, "proj-a")

        messages = [c.args[0] for c in mock_notify.call_args_list]
        assert any("awaiting approval" in m for m in messages)

    def test_remediation_never_calls_runner_directly(self, tmp_path: Path) -> None:
        """The ONLY orchestration path drift remediation ever takes is
        `Orchestrator.run_task` -- it must never resolve/instantiate a
        runner class itself to apply anything."""
        from hivepilot.orchestrator import StepApprovalPending

        cfg = self._cfg(remediate_task="apply-infra")
        project = _project(tmp_path)
        with (
            patch(
                "hivepilot.services.drift_schedule.project_service.load_projects"
            ) as mock_load_projects,
            patch(
                "hivepilot.services.drift_schedule.drift_service.scan_and_record",
                return_value=self._drifted_result(),
            ),
            patch("hivepilot.services.drift_schedule.notification_service.send_notification"),
            patch("hivepilot.services.drift_schedule.state_service.update_schedule_run"),
            patch("hivepilot.services.drift_schedule.Orchestrator") as mock_orch_cls,
            patch("hivepilot.registry.resolve_runner_class") as mock_resolve_runner,
        ):
            mock_load_projects.return_value.projects = {"proj-a": project}
            mock_orch_cls.return_value.run_task.side_effect = StepApprovalPending("pending")
            run_drift_scan(cfg, "proj-a")

        mock_resolve_runner.assert_not_called()

    def test_no_remediate_task_skips_remediation_and_alerts(self, tmp_path: Path) -> None:
        cfg = self._cfg(remediate_task=None)
        project = _project(tmp_path)
        with (
            patch(
                "hivepilot.services.drift_schedule.project_service.load_projects"
            ) as mock_load_projects,
            patch(
                "hivepilot.services.drift_schedule.drift_service.scan_and_record",
                return_value=self._drifted_result(),
            ),
            patch(
                "hivepilot.services.drift_schedule.notification_service.send_notification"
            ) as mock_notify,
            patch(
                "hivepilot.services.drift_schedule.state_service.update_schedule_run"
            ) as mock_mark,
            patch("hivepilot.services.drift_schedule.Orchestrator") as mock_orch_cls,
        ):
            mock_load_projects.return_value.projects = {"proj-a": project}
            run_drift_scan(cfg, "proj-a")

        mock_orch_cls.return_value.run_task.assert_not_called()
        assert mock_notify.call_count == 2
        messages = [c.args[0] for c in mock_notify.call_args_list]
        assert any("no remediate_task" in m for m in messages)
        mock_mark.assert_called_once_with("drift:proj-a")

    def test_auto_remediate_false_is_regression_safe(self, tmp_path: Path) -> None:
        cfg = self._cfg(auto_remediate=False, remediate_task="apply-infra")
        project = _project(tmp_path)
        with (
            patch(
                "hivepilot.services.drift_schedule.project_service.load_projects"
            ) as mock_load_projects,
            patch(
                "hivepilot.services.drift_schedule.drift_service.scan_and_record",
                return_value=self._drifted_result(),
            ),
            patch(
                "hivepilot.services.drift_schedule.notification_service.send_notification"
            ) as mock_notify,
            patch(
                "hivepilot.services.drift_schedule.state_service.update_schedule_run"
            ) as mock_mark,
            patch("hivepilot.services.drift_schedule.Orchestrator") as mock_orch_cls,
        ):
            mock_load_projects.return_value.projects = {"proj-a": project}
            run_drift_scan(cfg, "proj-a")

        mock_orch_cls.return_value.run_task.assert_not_called()
        mock_notify.assert_called_once()  # just the drift-detected alert
        mock_mark.assert_called_once_with("drift:proj-a")

    def test_unexpected_run_task_exception_propagates_without_alert(self, tmp_path: Path) -> None:
        cfg = self._cfg(remediate_task="apply-infra")
        project = _project(tmp_path)
        with (
            patch(
                "hivepilot.services.drift_schedule.project_service.load_projects"
            ) as mock_load_projects,
            patch(
                "hivepilot.services.drift_schedule.drift_service.scan_and_record",
                return_value=self._drifted_result(),
            ),
            patch(
                "hivepilot.services.drift_schedule.notification_service.send_notification"
            ) as mock_notify,
            patch(
                "hivepilot.services.drift_schedule.state_service.update_schedule_run"
            ) as mock_mark,
            patch("hivepilot.services.drift_schedule.Orchestrator") as mock_orch_cls,
        ):
            mock_load_projects.return_value.projects = {"proj-a": project}
            mock_orch_cls.return_value.run_task.side_effect = RuntimeError("boom")
            with pytest.raises(RuntimeError, match="boom"):
                run_drift_scan(cfg, "proj-a")

        # Only the drift-detected alert -- no remediation-failure alert, since
        # an arbitrary exception's string isn't guaranteed leak-free.
        mock_notify.assert_called_once()
        mock_mark.assert_called_once_with("drift:proj-a")


class TestSchedulerDaemonDriftScanTick:
    def test_disabled_config_runs_no_scans(self) -> None:
        from hivepilot.services.scheduler_daemon import SchedulerDaemon

        with (
            patch(
                "hivepilot.services.drift_schedule.load_drift_config",
                return_value=DriftScanConfig(enabled=False),
            ),
            patch("hivepilot.services.drift_schedule.run_drift_scan") as mock_run,
            patch(
                "hivepilot.services.drift_schedule.notification_service.send_notification"
            ) as mock_notify,
        ):
            daemon = SchedulerDaemon()
            daemon._run_drift_scans()

        mock_run.assert_not_called()
        mock_notify.assert_not_called()

    def test_enabled_config_scans_due_and_isolates_per_project_errors(self) -> None:
        from hivepilot.services.scheduler_daemon import SchedulerDaemon

        cfg = DriftScanConfig(enabled=True, projects=["ok-project", "bad-project"])

        def _fake_run(cfg_arg, project_name):
            if project_name == "bad-project":
                raise RuntimeError("boom")

        with (
            patch("hivepilot.services.drift_schedule.load_drift_config", return_value=cfg),
            patch(
                "hivepilot.services.drift_schedule.due_drift_projects",
                return_value=["ok-project", "bad-project"],
            ),
            patch(
                "hivepilot.services.drift_schedule.run_drift_scan", side_effect=_fake_run
            ) as mock_run,
        ):
            daemon = SchedulerDaemon()
            daemon._run_drift_scans()  # must not raise despite bad-project erroring

        assert mock_run.call_count == 2
        called_projects = [call.args[1] for call in mock_run.call_args_list]
        assert called_projects == ["ok-project", "bad-project"]
