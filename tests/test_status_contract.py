"""Tests for the derived-status contract (HP-42, Cycle 1 · P1).

Pins the raw-status → column/zone table (the same table `status-contract.ts`
mirrors for the Pollen board) and guards against drift with
`analytics_service`'s canonical outcome sets.
"""

from __future__ import annotations

import pytest

from hivepilot.services import status_contract as sc
from hivepilot.services.analytics_service import _FAILED_STATUSES, _SUCCEEDED_STATUSES

COLUMN_CASES = [
    ("new", "queued"),
    ("planned", "queued"),
    ("pending", "queued"),
    ("running", "running"),
    ("approval", "waiting_approval"),
    ("awaiting_approval", "waiting_approval"),
    ("review", "waiting_approval"),
    ("failed", "failed"),
    ("denied", "failed"),
    ("rate_limit", "failed"),
    ("auth_expired", "failed"),
    ("test_failure", "failed"),
    ("security_blocker", "failed"),
    ("success", "done"),
    ("complete", "done"),
    ("paused", "other"),
    ("cancelled", "other"),
    ("deferred", "other"),
    ("totally_unknown", "other"),
]

ZONE_CASES = [
    ("running", "working"),
    ("new", "queued"),
    ("planned", "queued"),
    ("approval", "needs_you"),
    ("awaiting_approval", "needs_you"),
    ("review", "in_review"),
    ("failed", "needs_you"),
    ("security_blocker", "needs_you"),
    ("success", "ready"),
    ("complete", "ready"),
    ("paused", "other"),
    ("cancelled", "other"),
    ("deferred", "other"),
]


@pytest.mark.parametrize("status,column", COLUMN_CASES)
def test_derive_column(status: str, column: str) -> None:
    assert sc.derive_column(status) == column


@pytest.mark.parametrize("status,zone", ZONE_CASES)
def test_derive_zone(status: str, zone: str) -> None:
    assert sc.derive_zone(status) == zone


def test_normalisation_is_case_and_whitespace_insensitive() -> None:
    assert sc.derive_column("  RUNNING ") == "running"
    assert sc.derive_zone("Failed") == "needs_you"


def test_none_and_empty_are_other() -> None:
    assert sc.derive_column(None) == "other"
    assert sc.derive_zone("") == "other"


def test_needs_attention_only_for_failures_and_decisions() -> None:
    assert sc.needs_attention("failed") is True
    assert sc.needs_attention("approval") is True
    assert sc.needs_attention("running") is False
    assert sc.needs_attention("success") is False
    assert sc.needs_attention("review") is False  # in_review, not needs_you


def test_derive_status_bundles_all_three() -> None:
    ds = sc.derive_status("test_failure")
    assert ds.raw == "test_failure"
    assert ds.column == "failed"
    assert ds.zone == "needs_you"
    assert ds.needs_attention is True


def test_failed_and_done_sets_match_analytics_no_drift() -> None:
    """The board's failure/success sets MUST equal `analytics_service`'s
    canonical sets — otherwise the board and the analytics would disagree about
    what a run outcome means."""
    assert sc.FAILED_STATUSES == frozenset(_FAILED_STATUSES)
    assert sc.DONE_STATUSES == frozenset(_SUCCEEDED_STATUSES)
