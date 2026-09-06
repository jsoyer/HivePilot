"""HP-74 — durable per-schedule memory (state layer). The DB is isolated to a
tmp file per test by conftest's `_isolate_state_db`."""

from __future__ import annotations

from hivepilot.services import state_service


def test_get_returns_none_before_any_write() -> None:
    assert state_service.get_schedule_memory("never-seen") is None


def test_upsert_then_get_round_trips() -> None:
    state_service.upsert_schedule_memory(
        "docs", scratch="carried", last_output="out", last_input_hash="abc"
    )
    mem = state_service.get_schedule_memory("docs")
    assert mem is not None
    assert mem["scratch"] == "carried"
    assert mem["last_output"] == "out"
    assert mem["last_input_hash"] == "abc"


def test_upsert_overwrites_the_same_name() -> None:
    state_service.upsert_schedule_memory("docs", scratch="v1", last_input_hash="h1")
    state_service.upsert_schedule_memory("docs", scratch="v2", last_input_hash="h2")
    mem = state_service.get_schedule_memory("docs")
    assert mem["scratch"] == "v2"
    assert mem["last_input_hash"] == "h2"


def test_two_schedules_keep_separate_memory() -> None:
    state_service.upsert_schedule_memory("a", scratch="A", last_input_hash="ha")
    state_service.upsert_schedule_memory("b", scratch="B", last_input_hash="hb")
    assert state_service.get_schedule_memory("a")["scratch"] == "A"
    assert state_service.get_schedule_memory("b")["scratch"] == "B"
