"""Tests for the audit log in state_service (record_audit / list_audit_log)."""

from __future__ import annotations

from hivepilot.services import state_service


def test_record_audit_persists_row() -> None:
    state_service.record_audit(
        token_hash="abc123",
        role="admin",
        endpoint="/runs",
        method="GET",
        result="authorized",
    )
    rows = state_service.list_audit_log()
    assert len(rows) == 1
    row = rows[0]
    assert row["token_hash"] == "abc123"
    assert row["role"] == "admin"
    assert row["endpoint"] == "/runs"
    assert row["method"] == "GET"
    assert row["result"] == "authorized"
    assert "timestamp" in row


def test_list_audit_log_most_recent_first_and_limit() -> None:
    for i in range(5):
        state_service.record_audit(
            token_hash=f"h{i}",
            role="read",
            endpoint=f"/e{i}",
            method="POST",
            result="forbidden",
        )
    rows = state_service.list_audit_log(limit=3)
    assert len(rows) == 3
    # ORDER BY id DESC → most recently inserted first
    assert rows[0]["endpoint"] == "/e4"
    assert rows[-1]["endpoint"] == "/e2"


def test_audit_log_empty_by_default() -> None:
    assert state_service.list_audit_log() == []
