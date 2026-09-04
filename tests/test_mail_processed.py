"""HP-75 — inbound-mail dedup/admission store (DB isolated per test)."""

from __future__ import annotations

from hivepilot.services import state_service


def test_get_returns_none_before_any_write() -> None:
    assert state_service.get_mail_processed("w", "<m1>") is None


def test_upsert_then_get_round_trips() -> None:
    state_service.upsert_mail_processed("w", "<m1>", status="dispatched", attempts=1)
    rec = state_service.get_mail_processed("w", "<m1>")
    assert rec["status"] == "dispatched"
    assert rec["attempts"] == 1


def test_upsert_updates_status_and_attempts() -> None:
    state_service.upsert_mail_processed("w", "<m1>", status="pending", attempts=1, error="x")
    state_service.upsert_mail_processed("w", "<m1>", status="skipped", attempts=3, error="y")
    rec = state_service.get_mail_processed("w", "<m1>")
    assert rec["status"] == "skipped" and rec["attempts"] == 3 and rec["error"] == "y"


def test_message_ids_are_scoped_per_watcher() -> None:
    state_service.upsert_mail_processed("a", "<m1>", status="dispatched")
    assert state_service.get_mail_processed("b", "<m1>") is None
