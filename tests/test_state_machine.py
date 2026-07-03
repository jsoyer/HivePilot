"""
Tests for the formal RunStatus enum added to state_service.py.

Covers:
- All formal status values exist and are importable
- Legacy string values ('running', 'pending', 'complete') are still accepted
  via the canonical .value attribute and by RunStatus.from_str()
- Failure states are present
- Enum members have the expected string values (backward-compatible)
"""

from __future__ import annotations

import pytest

from hivepilot.services.state_service import RunStatus


class TestRunStatusEnum:
    """RunStatus enum correctness."""

    def test_all_primary_states_exist(self) -> None:
        """Every specified primary state must be a member."""
        expected = {"NEW", "PLANNED", "RUNNING", "PAUSED", "REVIEW", "APPROVAL", "COMPLETE"}
        actual = {m.name for m in RunStatus}
        assert expected <= actual, f"Missing states: {expected - actual}"

    def test_all_failure_states_exist(self) -> None:
        """Every specified failure state must be a member."""
        expected = {"RATE_LIMIT", "AUTH_EXPIRED", "TEST_FAILURE", "SECURITY_BLOCKER"}
        actual = {m.name for m in RunStatus}
        assert expected <= actual, f"Missing failure states: {expected - actual}"

    def test_enum_is_importable_directly(self) -> None:
        """Import path must work."""
        from hivepilot.services.state_service import RunStatus as RS  # noqa: F401

        assert RS is RunStatus

    def test_enum_values_are_strings(self) -> None:
        """All enum values must be strings (used as DB status column values)."""
        for member in RunStatus:
            assert isinstance(member.value, str), f"{member.name} value is not a str"

    # ------------------------------------------------------------------
    # Backward compatibility — legacy strings
    # ------------------------------------------------------------------

    def test_legacy_running_is_accepted(self) -> None:
        """The legacy string 'running' must map to RunStatus.RUNNING."""
        assert RunStatus.from_str("running") is RunStatus.RUNNING

    def test_legacy_pending_is_accepted(self) -> None:
        """The legacy string 'pending' must map to a formal state."""
        result = RunStatus.from_str("pending")
        # 'pending' → NEW (closest semantic match for not-yet-started)
        assert result is RunStatus.NEW

    def test_legacy_complete_is_accepted(self) -> None:
        """The legacy string 'complete' must map to RunStatus.COMPLETE."""
        assert RunStatus.from_str("complete") is RunStatus.COMPLETE

    def test_from_str_case_insensitive(self) -> None:
        """from_str must accept mixed-case input."""
        assert RunStatus.from_str("Running") is RunStatus.RUNNING
        assert RunStatus.from_str("RUNNING") is RunStatus.RUNNING

    def test_from_str_formal_names(self) -> None:
        """Formal member names must round-trip through from_str."""
        for member in RunStatus:
            assert RunStatus.from_str(member.name) is member

    def test_from_str_unknown_raises(self) -> None:
        """Unknown strings must raise ValueError."""
        with pytest.raises(ValueError, match="Unknown status"):
            RunStatus.from_str("bogus_status_xyz")

    # ------------------------------------------------------------------
    # Value sanity
    # ------------------------------------------------------------------

    def test_running_value_is_running(self) -> None:
        """RunStatus.RUNNING.value should equal 'running' for DB compat."""
        assert RunStatus.RUNNING.value == "running"

    def test_complete_value_is_complete(self) -> None:
        """RunStatus.COMPLETE.value should equal 'complete' for DB compat."""
        assert RunStatus.COMPLETE.value == "complete"
