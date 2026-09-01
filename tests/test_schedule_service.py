"""Tests for hivepilot.services.schedule_service — the `ScheduleEntry`
task/source mutual-exclusion validation, and the new `source: "autopilot"`
drain branch in `run_entry` (Autopilot dynamic schedule).

Existing fixed-`task` behavior must stay byte-identical: these tests assert
the autopilot branch is only ever reached when `entry.source == "autopilot"`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.services.schedule_service import (
    ScheduleEntry,
    due_schedules,
    run_entry,
    schedule_input_hash,
)


class TestScheduleEntryValidation:
    def test_task_only_is_valid(self) -> None:
        entry = ScheduleEntry(name="docs-weekly", projects=["p"], task="docs")
        assert entry.task == "docs"
        assert entry.source is None

    def test_source_only_is_valid(self) -> None:
        entry = ScheduleEntry(name="autopilot-drain", projects=["p"], source="autopilot")
        assert entry.source == "autopilot"
        assert entry.task is None

    def test_neither_task_nor_source_raises(self) -> None:
        with pytest.raises(ValueError):
            ScheduleEntry(name="broken", projects=["p"])

    def test_both_task_and_source_raises(self) -> None:
        with pytest.raises(ValueError):
            ScheduleEntry(name="broken", projects=["p"], task="docs", source="autopilot")

    def test_unsupported_source_value_raises(self) -> None:
        with pytest.raises(ValueError):
            ScheduleEntry(name="broken", projects=["p"], source="something-else")


class TestRunEntryAutopilotBranch:
    def test_autopilot_source_drains_queue_not_run_task(self) -> None:
        entry = ScheduleEntry(name="autopilot-drain", projects=["p"], source="autopilot")
        orchestrator = MagicMock()
        with patch("hivepilot.services.autopilot_queue.drain_one") as mock_drain:
            result = run_entry(entry, orchestrator)

        assert result is True
        mock_drain.assert_called_once_with(orchestrator, tenant="default")
        orchestrator.run_task.assert_not_called()

    def test_autopilot_source_marks_schedule_run(self) -> None:
        entry = ScheduleEntry(name="autopilot-drain", projects=["p"], source="autopilot")
        orchestrator = MagicMock()
        with (
            patch("hivepilot.services.autopilot_queue.drain_one"),
            patch("hivepilot.services.schedule_service.mark_run") as mock_mark,
        ):
            run_entry(entry, orchestrator)
        mock_mark.assert_called_once_with(entry)

    def test_fixed_task_entry_unaffected_by_autopilot_branch(self) -> None:
        entry = ScheduleEntry(name="docs-weekly", projects=["p"], task="docs")
        orchestrator = MagicMock()
        with patch("hivepilot.services.autopilot_queue.drain_one") as mock_drain:
            result = run_entry(entry, orchestrator)

        assert result is True
        mock_drain.assert_not_called()
        orchestrator.run_task.assert_called_once_with(
            project_names=["p"],
            task_name="docs",
            extra_prompt=None,
            auto_git=False,
        )


class TestRunEntryStampsLastRunOnFailure:
    """Regression tests for the busy-loop bug: a permanently-failing
    fixed-task schedule must still stamp `last_run` so the schedule's own
    `interval_minutes` cadence is respected. Retries stay governed
    separately by `retry_service`'s own backoff queue -- this class only
    covers the schedule's own re-dispatch cadence.
    """

    def test_failed_run_still_calls_mark_run(self) -> None:
        entry = ScheduleEntry(name="nightly", projects=["p"], task="docs", interval_minutes=60)
        orchestrator = MagicMock()
        orchestrator.run_task.side_effect = RuntimeError("boom")
        with (
            patch("hivepilot.services.schedule_service.mark_run") as mock_mark,
            patch("hivepilot.services.retry_service.enqueue") as mock_enqueue,
        ):
            result = run_entry(entry, orchestrator)

        assert result is False
        mock_mark.assert_called_once_with(entry)
        mock_enqueue.assert_called_once()

    def test_successful_run_calls_mark_run_once(self) -> None:
        entry = ScheduleEntry(name="nightly", projects=["p"], task="docs")
        orchestrator = MagicMock()
        with patch("hivepilot.services.schedule_service.mark_run") as mock_mark:
            result = run_entry(entry, orchestrator)

        assert result is True
        mock_mark.assert_called_once_with(entry)


class TestDueSchedulesNoBusyLoopOnFailure:
    """`due_schedules()` treats a never-stamped `last_run` as perpetually
    due. Before the fix, `run_entry` only called `mark_run` on success, so
    a persistently-failing schedule was re-dispatched every daemon tick
    (~30s) forever, on top of the independent retry_service backoff queue.
    """

    def test_failing_entry_not_due_again_until_interval_elapses(self) -> None:
        entry = ScheduleEntry(name="nightly", projects=["p"], task="docs", interval_minutes=60)
        orchestrator = MagicMock()
        orchestrator.run_task.side_effect = RuntimeError("boom")

        stored_last_run: dict[str, object] = {}

        def _fake_update(name: str) -> None:
            from datetime import datetime, timezone

            stored_last_run[name] = datetime.now(timezone.utc)

        def _fake_get(name: str):
            return stored_last_run.get(name)

        with (
            patch("hivepilot.services.state_service.update_schedule_run", _fake_update),
            patch("hivepilot.services.state_service.get_schedule_last_run", _fake_get),
            patch("hivepilot.services.retry_service.enqueue"),
        ):
            result = run_entry(entry, orchestrator)
            assert result is False
            # last_run WAS stamped despite the failure -- this is the fix.
            assert entry.name in stored_last_run

            with patch(
                "hivepilot.services.schedule_service.load_schedules",
                return_value={entry.name: entry},
            ):
                due = due_schedules()

        # Not due again -- the 60-minute interval hasn't elapsed, so the
        # daemon must NOT re-dispatch it on the next ~30s tick.
        assert due == []


class TestScheduleInputHash:
    def test_is_stable_for_the_same_inputs(self) -> None:
        entry = ScheduleEntry(name="n", projects=["b", "a"], task="docs")
        orch = MagicMock()
        assert schedule_input_hash(entry, orch) == schedule_input_hash(entry, orch)

    def test_changes_with_the_task(self) -> None:
        orch = MagicMock()
        a = schedule_input_hash(ScheduleEntry(name="n", projects=["p"], task="docs"), orch)
        b = schedule_input_hash(ScheduleEntry(name="n", projects=["p"], task="pentest"), orch)
        assert a != b

    def test_a_mock_orchestrator_registry_does_not_crash(self) -> None:
        # MagicMock().projects.projects is not a dict → no paths, no git calls.
        assert isinstance(
            schedule_input_hash(ScheduleEntry(name="n", projects=["p"], task="d"), MagicMock()), str
        )


class TestRememberMemory:
    def _entry(self, remember: bool) -> ScheduleEntry:
        return ScheduleEntry(name="watch", projects=["p"], task="docs", remember=remember)

    def test_remember_false_is_byte_identical_and_writes_no_memory(self) -> None:
        orchestrator = MagicMock()
        with (
            patch("hivepilot.services.schedule_service.mark_run"),
            patch("hivepilot.services.state_service.upsert_schedule_memory") as mock_upsert,
        ):
            run_entry(self._entry(remember=False), orchestrator)

        orchestrator.run_task.assert_called_once_with(
            project_names=["p"], task_name="docs", extra_prompt=None, auto_git=False
        )
        mock_upsert.assert_not_called()

    def test_first_remembered_run_persists_output_and_hash(self) -> None:
        orchestrator = MagicMock()
        orchestrator.run_task.return_value = [SimpleNamespace(detail="did the work")]
        with (
            patch("hivepilot.services.schedule_service.mark_run"),
            patch("hivepilot.services.schedule_service.schedule_input_hash", return_value="h1"),
            patch("hivepilot.services.state_service.get_schedule_memory", return_value=None),
            patch("hivepilot.services.state_service.upsert_schedule_memory") as mock_upsert,
        ):
            result = run_entry(self._entry(remember=True), orchestrator)

        assert result is True
        orchestrator.run_task.assert_called_once()
        assert "prior_context" not in orchestrator.run_task.call_args.kwargs  # first run
        mock_upsert.assert_called_once()
        assert mock_upsert.call_args.kwargs["last_input_hash"] == "h1"
        assert mock_upsert.call_args.kwargs["scratch"] == "did the work"

    def test_unchanged_inputs_skip_the_model(self) -> None:
        orchestrator = MagicMock()
        with (
            patch("hivepilot.services.schedule_service.mark_run") as mock_mark,
            patch("hivepilot.services.schedule_service.schedule_input_hash", return_value="h1"),
            patch(
                "hivepilot.services.state_service.get_schedule_memory",
                return_value={"last_input_hash": "h1", "scratch": "prev"},
            ),
        ):
            result = run_entry(self._entry(remember=True), orchestrator)

        assert result is True
        orchestrator.run_task.assert_not_called()  # no-op skip
        mock_mark.assert_called_once()  # cadence still stamped

    def test_changed_inputs_carry_prior_output_as_context(self) -> None:
        orchestrator = MagicMock()
        orchestrator.run_task.return_value = [SimpleNamespace(detail="new output")]
        with (
            patch("hivepilot.services.schedule_service.mark_run"),
            patch("hivepilot.services.schedule_service.schedule_input_hash", return_value="h2"),
            patch(
                "hivepilot.services.state_service.get_schedule_memory",
                return_value={"last_input_hash": "h1", "scratch": "yesterday's output"},
            ),
            patch("hivepilot.services.state_service.upsert_schedule_memory"),
        ):
            run_entry(self._entry(remember=True), orchestrator)

        assert orchestrator.run_task.call_args.kwargs["prior_context"] == "yesterday's output"
