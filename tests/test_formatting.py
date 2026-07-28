"""Tests for hivepilot.ui.formatting — pure, no textual dependency."""

from __future__ import annotations

import pytest

from hivepilot.ui.formatting import INTERACTION_COLUMNS, interaction_rows


@pytest.fixture(autouse=True)
def _reset_display_timezone_override(monkeypatch):
    """Every test starts with no explicit override configured, matching
    tests/test_display_time.py's fixture -- keeps the module's
    fallback-to-system-timezone path from making assertions flaky across
    hosts unless a test pins an explicit override."""
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "display_timezone", None, raising=False)


def test_interaction_columns_length() -> None:
    assert len(INTERACTION_COLUMNS) == 6


def test_empty_list_returns_empty() -> None:
    assert interaction_rows([]) == []


def test_normal_row_values_and_order(monkeypatch) -> None:
    """fix/linear-sync-display-time sweep: the Timestamp column was found
    rendering the raw stored value verbatim -- same bug class as the
    pre-fix `schedule health`/`linear sync` CLI tables (a naive-looking
    string that reads as local time while actually being UTC). Now routed
    through `display_time.to_display` like every other human-facing sink,
    so this test pins the CONVERTED, timezone-marked value rather than the
    old raw passthrough."""
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
    interactions = [
        {
            "id": 1,
            "run_id": 42,
            "actor": "ceo",
            "action": "send_message",
            "target": "cto",
            "summary": "Hello there",
            "metadata": {},
            # Naive-UTC (no offset) -- exactly what state_service's
            # CURRENT_TIMESTAMP-backed writers store.
            "timestamp": "2026-07-27 09:08:32",
        }
    ]
    rows = interaction_rows(interactions)
    assert len(rows) == 1
    row = rows[0]
    # tuple length must match columns
    assert len(row) == len(INTERACTION_COLUMNS)
    # check each field in order: Run, Actor, Action, Target, Summary, Timestamp
    assert row[0] == "42"
    assert row[1] == "ceo"
    assert row[2] == "send_message"
    assert row[3] == "cto"
    assert row[4] == "Hello there"
    assert "09:08" not in row[5]
    assert "11:08" in row[5]
    assert "CEST" in row[5]


def test_timestamp_dst_correctness_summer_vs_winter(monkeypatch) -> None:
    """Same wall-clock UTC time must render differently in July (CEST) vs
    January (CET) -- a fixed-offset shortcut fails this."""
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
    base = {
        "id": 1,
        "run_id": 1,
        "actor": "ceo",
        "action": "send_message",
        "target": "cto",
        "summary": "hi",
        "metadata": {},
    }
    summer_row = interaction_rows([{**base, "timestamp": "2026-07-27 09:08:32"}])[0]
    winter_row = interaction_rows([{**base, "timestamp": "2026-01-27 09:08:32"}])[0]

    assert "11:08" in summer_row[5] and "CEST" in summer_row[5]
    assert "10:08" in winter_row[5] and "CET" in winter_row[5]
    assert summer_row[5] != winter_row[5]


def test_timestamp_missing_or_unparseable_renders_unknown_not_fabricated() -> None:
    """Fail-closed: an absent/garbage timestamp must render an explicit
    unknown marker, never a fabricated or silently-wrong local time."""
    from hivepilot.utils import display_time

    interactions = [
        {
            "id": 2,
            "run_id": 2,
            "actor": "ceo",
            "action": "send_message",
            "target": "cto",
            "summary": "hi",
            "metadata": {},
            "timestamp": "",
        }
    ]
    rows = interaction_rows(interactions)
    assert rows[0][5] == display_time.UNKNOWN_DISPLAY


def test_run_id_none_becomes_dash() -> None:
    interactions = [
        {
            "id": 2,
            "run_id": None,
            "actor": "cfo",
            "action": "query",
            "target": "all",
            "summary": "budget check",
            "metadata": {},
            "timestamp": "2026-06-19T11:00:00",
        }
    ]
    rows = interaction_rows(interactions)
    assert rows[0][0] == "-"


def test_target_none_becomes_all() -> None:
    interactions = [
        {
            "id": 3,
            "run_id": 7,
            "actor": "cto",
            "action": "broadcast",
            "target": None,
            "summary": "tech update",
            "metadata": {},
            "timestamp": "2026-06-19T12:00:00",
        }
    ]
    rows = interaction_rows(interactions)
    assert rows[0][3] == "all"


def test_summary_truncated_to_80_chars() -> None:
    long_summary = "x" * 120
    interactions = [
        {
            "id": 4,
            "run_id": 5,
            "actor": "a",
            "action": "b",
            "target": "c",
            "summary": long_summary,
            "metadata": {},
            "timestamp": "2026-06-19T13:00:00",
        }
    ]
    rows = interaction_rows(interactions)
    assert len(rows[0][4]) == 80


def test_summary_exactly_80_chars_unchanged() -> None:
    summary_80 = "y" * 80
    interactions = [
        {
            "id": 5,
            "run_id": 5,
            "actor": "a",
            "action": "b",
            "target": "c",
            "summary": summary_80,
            "metadata": {},
            "timestamp": "2026-06-19T13:00:00",
        }
    ]
    rows = interaction_rows(interactions)
    assert rows[0][4] == summary_80


def test_summary_none_becomes_empty_string() -> None:
    interactions = [
        {
            "id": 6,
            "run_id": 5,
            "actor": "a",
            "action": "b",
            "target": "c",
            "summary": None,
            "metadata": {},
            "timestamp": "2026-06-19T13:00:00",
        }
    ]
    rows = interaction_rows(interactions)
    assert rows[0][4] == ""


def test_actor_none_becomes_empty_string() -> None:
    interactions = [
        {
            "id": 7,
            "run_id": 5,
            "actor": None,
            "action": "b",
            "target": "c",
            "summary": "s",
            "metadata": {},
            "timestamp": "2026-06-19T13:00:00",
        }
    ]
    rows = interaction_rows(interactions)
    assert rows[0][1] == ""


def test_action_none_becomes_empty_string() -> None:
    interactions = [
        {
            "id": 8,
            "run_id": 5,
            "actor": "a",
            "action": None,
            "target": "c",
            "summary": "s",
            "metadata": {},
            "timestamp": "2026-06-19T13:00:00",
        }
    ]
    rows = interaction_rows(interactions)
    assert rows[0][2] == ""


def test_multiple_rows_all_have_6_elements() -> None:
    interactions = [
        {
            "id": i,
            "run_id": i * 10,
            "actor": f"actor_{i}",
            "action": f"action_{i}",
            "target": f"target_{i}",
            "summary": f"summary_{i}",
            "metadata": {},
            "timestamp": f"2026-06-19T{i:02d}:00:00",
        }
        for i in range(5)
    ]
    rows = interaction_rows(interactions)
    assert len(rows) == 5
    for row in rows:
        assert len(row) == 6


def test_all_tuples_are_strings() -> None:
    interactions = [
        {
            "id": 9,
            "run_id": 99,
            "actor": "agent",
            "action": "decide",
            "target": "team",
            "summary": "made decision",
            "metadata": {},
            "timestamp": "2026-06-19T15:00:00",
        }
    ]
    rows = interaction_rows(interactions)
    for element in rows[0]:
        assert isinstance(element, str)
