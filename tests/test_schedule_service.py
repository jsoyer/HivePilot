"""Tests for hivepilot.services.schedule_service — the `ScheduleEntry`
task/source mutual-exclusion validation, and the new `source: "autopilot"`
drain branch in `run_entry` (Autopilot dynamic schedule).

Existing fixed-`task` behavior must stay byte-identical: these tests assert
the autopilot branch is only ever reached when `entry.source == "autopilot"`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hivepilot.services.schedule_service import ScheduleEntry, run_entry


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
