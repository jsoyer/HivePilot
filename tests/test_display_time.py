"""Tests for hivepilot.utils.display_time — the single conversion helper
every HUMAN-facing surface uses to render a stored UTC timestamp.

Reproduces the production incident this module fixes: `state_service`
stores `2026-07-27 09:08:32` (naive-UTC, via SQLite's CURRENT_TIMESTAMP) for
a run that actually started at 11:08 local time in Europe/Paris (CEST,
UTC+2). Every test below asserts against that exact scenario or its winter
(CET, UTC+1) counterpart, so a fixed-offset shortcut (which would pass the
summer case but fail the winter one) cannot sneak through.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from hivepilot.config import settings
from hivepilot.utils import display_time

# The exact evidence string from the bug report: stored (naive UTC, SQLite
# CURRENT_TIMESTAMP format) for a run that started 11:08 local in Paris (CEST).
STORED_SUMMER_UTC = "2026-07-27 09:08:32"
# Same wall-clock UTC time, but in winter (CET, UTC+1) — proves DST is
# actually respected, not a snapshot of "today's" offset.
STORED_WINTER_UTC = "2026-01-27 09:08:32"


@pytest.fixture(autouse=True)
def _reset_override(monkeypatch):
    """Every test starts with no explicit override configured."""
    monkeypatch.setattr(settings, "display_timezone", None, raising=False)


# ---------------------------------------------------------------------------
# to_display — core conversion + marker
# ---------------------------------------------------------------------------


def test_to_display_converts_naive_utc_string_to_local_time():
    """The exact bug: a stored 09:08 UTC must render as 11:08 in CEST, not
    as a bare '09:08' that reads like local time."""
    rendered = display_time.to_display(STORED_SUMMER_UTC, zone=ZoneInfo("Europe/Paris"))
    assert "09:08" not in rendered
    assert "11:08" in rendered


def test_to_display_carries_a_timezone_marker():
    rendered = display_time.to_display(STORED_SUMMER_UTC, zone=ZoneInfo("Europe/Paris"))
    # Either an abbreviation (CEST) or an explicit numeric offset — never a
    # bare, unmarked clock time (that ambiguity is exactly how the bug hid).
    assert "CEST" in rendered or "+02:00" in rendered


def test_to_display_dst_correctness_summer_vs_winter():
    """The same wall-clock UTC time renders DIFFERENTLY in summer (CEST,
    +2h) vs winter (CET, +1h) — a fixed-offset shortcut fails this because
    it would apply the SAME offset to both."""
    paris = ZoneInfo("Europe/Paris")
    summer = display_time.to_display(STORED_SUMMER_UTC, zone=paris)
    winter = display_time.to_display(STORED_WINTER_UTC, zone=paris)
    assert "11:08" in summer
    assert "10:08" in winter
    assert "CEST" in summer
    assert "CET" in winter
    assert "CET" not in summer.replace("CEST", "")  # CEST contains "CET" as substring guard
    assert summer != winter


def test_to_display_accepts_aware_datetime():
    dt = datetime(2026, 7, 27, 9, 8, 32, tzinfo=timezone.utc)
    rendered = display_time.to_display(dt, zone=ZoneInfo("Europe/Paris"))
    assert "11:08" in rendered


def test_to_display_none_or_empty_is_unknown_not_a_crash():
    assert display_time.to_display(None) == display_time.UNKNOWN_DISPLAY
    assert display_time.to_display("") == display_time.UNKNOWN_DISPLAY


def test_to_display_unparseable_string_is_unknown_not_a_crash():
    assert display_time.to_display("not-a-timestamp") == display_time.UNKNOWN_DISPLAY


# ---------------------------------------------------------------------------
# resolve_display_zone — explicit override honoured
# ---------------------------------------------------------------------------


def test_explicit_override_is_honoured(monkeypatch):
    monkeypatch.setattr(settings, "display_timezone", "Asia/Tokyo", raising=False)
    zone, source = display_time.resolve_display_zone()
    assert source == "override"
    rendered = display_time.to_display(STORED_SUMMER_UTC, zone=zone)
    # 09:08 UTC -> 18:08 JST (+9h, no DST in Japan)
    assert "18:08" in rendered
    assert "JST" in rendered


def test_invalid_override_falls_back_without_crashing(monkeypatch):
    monkeypatch.setattr(settings, "display_timezone", "Not/AZone", raising=False)
    zone, source = display_time.resolve_display_zone()
    assert source != "override"
    # Must still render something sane, never raise.
    rendered = display_time.to_display(STORED_SUMMER_UTC, zone=zone)
    assert rendered != display_time.UNKNOWN_DISPLAY


def test_no_override_uses_detected_system_zone_when_available(monkeypatch):
    monkeypatch.setattr(display_time, "detect_system_zone_name", lambda: "Europe/Paris")
    zone, source = display_time.resolve_display_zone()
    assert source == "system"
    assert zone.key == "Europe/Paris"


def test_no_override_and_no_system_zone_falls_back_to_utc(monkeypatch):
    monkeypatch.setattr(display_time, "detect_system_zone_name", lambda: None)
    zone, source = display_time.resolve_display_zone()
    assert source == "fallback-utc"
    assert zone is timezone.utc


# ---------------------------------------------------------------------------
# detect_system_zone_name — resolution chain
# ---------------------------------------------------------------------------


def test_detect_system_zone_prefers_tz_env(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    assert display_time.detect_system_zone_name() == "America/New_York"


def test_detect_system_zone_falls_back_to_etc_timezone(monkeypatch, tmp_path):
    monkeypatch.delenv("TZ", raising=False)
    fake_etc_timezone = tmp_path / "timezone"
    fake_etc_timezone.write_text("Europe/Berlin\n")
    monkeypatch.setattr(display_time, "_ETC_TIMEZONE_PATH", fake_etc_timezone)
    assert display_time.detect_system_zone_name() == "Europe/Berlin"


def test_detect_system_zone_returns_none_when_undetectable(monkeypatch, tmp_path):
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(display_time, "_ETC_TIMEZONE_PATH", tmp_path / "does-not-exist")
    monkeypatch.setattr(display_time, "_ETC_LOCALTIME_PATH", tmp_path / "does-not-exist-either")
    assert display_time.detect_system_zone_name() is None
