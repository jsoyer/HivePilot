"""
Tests for drift-scan persistence (Phase 20 Sprint D2).

Covers `hivepilot.services.state_service.record_drift_scan` /
`get_recent_drift_scans` / `get_drift_baseline`, and
`hivepilot.services.drift_service.scan_and_record`.

The autouse `_isolate_state_db` fixture (conftest.py) redirects
`state_service.DB_PATH` to a per-test tmp file, so these tests never touch
the real ./state.db.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from hivepilot.models import ProjectConfig
from hivepilot.services import state_service
from hivepilot.services.config_provenance import register_secret_value
from hivepilot.services.drift_service import DriftResult, DriftSummary, scan_and_record
from hivepilot.services.state_service import (
    get_drift_baseline,
    get_recent_drift_scans,
    init_db,
    record_drift_scan,
)

# A secret-looking token that must never survive unredacted into the
# persisted `detail` column, even though it appears in a raised exception
# message from a (hypothetically misbehaving) `detect_drift`.
_LEAKED_LOOKING_TOKEN = "sk-live-should-never-leak-0123456789"  # noqa: S105


def _ok_result(project: str = "proj-a", runner: str = "opentofu") -> DriftResult:
    return DriftResult(
        project=project,
        runner=runner,
        drifted=False,
        summary=DriftSummary(to_add=0, to_change=0, to_destroy=0),
    )


def _drift_result(project: str = "proj-a", runner: str = "opentofu") -> DriftResult:
    return DriftResult(
        project=project,
        runner=runner,
        drifted=True,
        summary=DriftSummary(to_add=1, to_change=2, to_destroy=3),
    )


def _error_result(
    project: str = "proj-a", runner: str = "opentofu", error: str = "boom"
) -> DriftResult:
    return DriftResult(project=project, runner=runner, drifted=False, summary=None, error=error)


def _project(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(path=tmp_path)


# ---------------------------------------------------------------------------
# init_db — table existence + idempotency
# ---------------------------------------------------------------------------


class TestInitDb:
    def test_drift_scans_table_exists_after_init_db(self) -> None:
        init_db()
        with sqlite3.connect(state_service.DB_PATH) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='drift_scans'"
            ).fetchone()
        assert row is not None, "drift_scans table must be created by init_db()"

    def test_init_db_is_idempotent(self) -> None:
        init_db()
        init_db()  # must not raise / duplicate the table
        with sqlite3.connect(state_service.DB_PATH) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='drift_scans'"
            ).fetchall()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# record_drift_scan — roundtrip per status
# ---------------------------------------------------------------------------


class TestRecordDriftScan:
    def test_ok_status_roundtrip(self) -> None:
        row_id = record_drift_scan(_ok_result())
        assert isinstance(row_id, int)
        assert row_id >= 1

        rows = get_recent_drift_scans()
        assert len(rows) == 1
        row = rows[0]
        assert row["project"] == "proj-a"
        assert row["runner"] == "opentofu"
        assert row["drifted"] == 0
        assert row["status"] == "ok"
        assert row["to_add"] == 0
        assert row["to_change"] == 0
        assert row["to_destroy"] == 0
        assert row["detail"] is None
        assert row["tenant"] == "default"

    def test_drift_status_roundtrip(self) -> None:
        record_drift_scan(_drift_result())
        row = get_recent_drift_scans()[0]
        assert row["drifted"] == 1
        assert row["status"] == "drift"
        assert row["to_add"] == 1
        assert row["to_change"] == 2
        assert row["to_destroy"] == 3
        assert row["detail"] is None

    def test_error_status_roundtrip(self) -> None:
        record_drift_scan(_error_result(error="tofu drift check failed with exit code 1"))
        row = get_recent_drift_scans()[0]
        assert row["drifted"] == 0
        assert row["status"] == "error"
        assert row["detail"] == "tofu drift check failed with exit code 1"
        assert row["to_add"] is None
        assert row["to_change"] is None
        assert row["to_destroy"] is None

    def test_custom_tenant_stored(self) -> None:
        record_drift_scan(_ok_result(), tenant="acme")
        row = get_recent_drift_scans(tenant="acme")[0]
        assert row["tenant"] == "acme"


# ---------------------------------------------------------------------------
# get_recent_drift_scans — ordering, limit, filters
# ---------------------------------------------------------------------------


class TestGetRecentDriftScans:
    def test_ordering_is_newest_first(self) -> None:
        record_drift_scan(_ok_result(project="p1"))
        record_drift_scan(_drift_result(project="p2"))
        record_drift_scan(_error_result(project="p3"))
        rows = get_recent_drift_scans()
        assert [r["project"] for r in rows] == ["p3", "p2", "p1"]

    def test_limit_is_respected(self) -> None:
        for i in range(5):
            record_drift_scan(_ok_result(project=f"p{i}"))
        rows = get_recent_drift_scans(limit=2)
        assert len(rows) == 2
        assert [r["project"] for r in rows] == ["p4", "p3"]

    def test_project_filter(self) -> None:
        record_drift_scan(_ok_result(project="p1"))
        record_drift_scan(_drift_result(project="p2"))
        record_drift_scan(_ok_result(project="p1"))
        rows = get_recent_drift_scans(project="p1")
        assert len(rows) == 2
        assert all(r["project"] == "p1" for r in rows)

    def test_tenant_filter_scopes_results(self) -> None:
        record_drift_scan(_ok_result(project="p1"), tenant="acme")
        record_drift_scan(_ok_result(project="p2"), tenant="default")
        rows = get_recent_drift_scans(tenant="acme")
        assert len(rows) == 1
        assert rows[0]["project"] == "p1"

    def test_no_tenant_filter_returns_all_tenants(self) -> None:
        record_drift_scan(_ok_result(project="p1"), tenant="acme")
        record_drift_scan(_ok_result(project="p2"), tenant="default")
        rows = get_recent_drift_scans()
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# get_drift_baseline — most-recent 'ok' scan
# ---------------------------------------------------------------------------


class TestGetDriftBaseline:
    def test_returns_most_recent_ok_scan(self) -> None:
        record_drift_scan(_ok_result(project="p1"))
        record_drift_scan(_drift_result(project="p1"))
        record_drift_scan(_ok_result(project="p1"))
        baseline = get_drift_baseline("p1")
        assert baseline is not None
        assert baseline["status"] == "ok"
        assert baseline["drifted"] == 0

    def test_skips_drift_and_error_rows(self) -> None:
        record_drift_scan(_ok_result(project="p1"))
        record_drift_scan(_drift_result(project="p1"))
        record_drift_scan(_error_result(project="p1"))
        baseline = get_drift_baseline("p1")
        assert baseline is not None
        assert baseline["status"] == "ok"

    def test_none_when_no_ok_scan_exists(self) -> None:
        record_drift_scan(_drift_result(project="p1"))
        record_drift_scan(_error_result(project="p1"))
        assert get_drift_baseline("p1") is None

    def test_none_when_no_scans_at_all(self) -> None:
        assert get_drift_baseline("nonexistent") is None

    def test_scoped_to_project_and_tenant(self) -> None:
        record_drift_scan(_ok_result(project="p1"), tenant="acme")
        record_drift_scan(_ok_result(project="p2"), tenant="default")
        assert get_drift_baseline("p1", tenant="default") is None
        assert get_drift_baseline("p1", tenant="acme") is not None
        assert get_drift_baseline("p2", tenant="acme") is None


# ---------------------------------------------------------------------------
# scan_and_record — wraps detect_drift + persists (including error rows)
# ---------------------------------------------------------------------------


class TestScanAndRecord:
    def test_success_records_and_returns(self, tmp_path: Path) -> None:
        result = _ok_result(project="proj-a")
        with patch(
            "hivepilot.services.drift_service.detect_drift", return_value=result
        ) as mock_detect:
            returned = scan_and_record(_project(tmp_path), runner_kind="opentofu")

        mock_detect.assert_called_once()
        assert returned == result
        rows = get_recent_drift_scans()
        assert len(rows) == 1
        assert rows[0]["status"] == "ok"

    def test_failure_persists_error_row_and_reraises(self, tmp_path: Path) -> None:
        with patch(
            "hivepilot.services.drift_service.detect_drift",
            side_effect=RuntimeError("tofu drift check failed with exit code 1"),
        ):
            with pytest.raises(RuntimeError):
                scan_and_record(_project(tmp_path), runner_kind="opentofu")

        rows = get_recent_drift_scans()
        assert len(rows) == 1
        assert rows[0]["status"] == "error"
        assert "exit code 1" in rows[0]["detail"]

    def test_failure_secret_in_error_never_lands_unredacted(self, tmp_path: Path) -> None:
        register_secret_value(_LEAKED_LOOKING_TOKEN)
        try:
            with patch(
                "hivepilot.services.drift_service.detect_drift",
                side_effect=RuntimeError(f"boom {_LEAKED_LOOKING_TOKEN}"),
            ):
                with pytest.raises(RuntimeError):
                    scan_and_record(_project(tmp_path), runner_kind="opentofu")

            rows = get_recent_drift_scans()
            assert len(rows) == 1
            assert _LEAKED_LOOKING_TOKEN not in (rows[0]["detail"] or "")
        finally:
            # Registered secret values are process-global; keep this test
            # from bleeding into others in the same session.
            from hivepilot.services.config_provenance import _SECRET_VALUES, _secret_values_lock

            with _secret_values_lock:
                _SECRET_VALUES.discard(_LEAKED_LOOKING_TOKEN)
