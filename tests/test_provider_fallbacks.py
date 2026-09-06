"""HP-73 — recording + surfacing HP-70 provider fallbacks.

The orchestrator emits a `provider.fallback` event at the fallback site
(`orchestrator.py`); these tests validate the durable, queryable contract that
event feeds: `events.recent(...)` as a time-windowed kind lookup, and the
`GET /v1/providers/fallbacks` aggregation the Providers panel reads."""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import api_service, events, state_service
from hivepilot.services.token_service import add_token


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    from hivepilot.config import settings

    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    return TestClient(api_service.app, raise_server_exceptions=True)


def _auth(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


def _emit_fallback(from_runner: str, to_runner: str, reason: str) -> None:
    events.emit(
        "provider.fallback",
        "provider",
        from_runner,
        payload={"from": from_runner, "to": to_runner, "reason": reason},
    )


class TestRecent:
    def test_filters_by_kind_and_is_newest_first(self) -> None:
        state_service.init_db()
        events.emit("run.started", "run", 1)  # different kind — must be excluded
        _emit_fallback("claude", "codex", "quota")
        _emit_fallback("claude", "cursor", "unavailable")

        rows = events.recent("provider.fallback")
        assert [r["kind"] for r in rows] == ["provider.fallback", "provider.fallback"]
        # newest first
        assert rows[0]["payload"]["to"] == "cursor"
        assert rows[1]["payload"]["to"] == "codex"

    def test_respects_the_time_window(self) -> None:
        from datetime import datetime, timedelta, timezone

        from hivepilot.services import db

        state_service.init_db()
        _emit_fallback("claude", "codex", "quota")  # now
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        with db.connect() as conn:
            conn.execute(
                db.ph(
                    "INSERT INTO change_log (channel, kind, entity_type, entity_id, tenant, "
                    "payload, ts) VALUES (?, ?, ?, ?, ?, ?, ?)"
                ),
                (events.CHANNEL, "provider.fallback", "provider", "old", "default", "{}", old_ts),
            )

        names = {r.get("entity_id") for r in events.recent("provider.fallback", hours=1)}
        assert "claude" in names and "old" not in names  # 3h-old row excluded by the 1h window


class TestFallbacksEndpoint:
    def test_requires_read(self, api_client, tmp_tokens_file):
        assert api_client.get("/v1/providers/fallbacks").status_code == 401

    def test_aggregates_by_source_provider(self, api_client, tmp_tokens_file):
        _emit_fallback("claude", "codex", "quota")
        _emit_fallback("claude", "cursor", "unavailable")
        _emit_fallback("grok", "codex", "quota")

        raw, _ = add_token("read")
        body = api_client.get("/v1/providers/fallbacks", headers=_auth(raw)).json()

        by_name = {p["provider"]: p for p in body["providers"]}
        assert by_name["claude"]["count"] == 2
        assert by_name["grok"]["count"] == 1
        # most recent claude fallback wins the "last_*" fields
        assert by_name["claude"]["last_to"] == "cursor"
        assert by_name["claude"]["last_reason"] == "unavailable"
        assert by_name["claude"]["last_at"] is not None
        # ordered by count desc
        assert body["providers"][0]["provider"] == "claude"

    def test_empty_when_no_fallbacks(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        body = api_client.get("/v1/providers/fallbacks", headers=_auth(raw)).json()
        assert body["providers"] == []

    def test_hours_is_clamped(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        body = api_client.get("/v1/providers/fallbacks?hours=99999", headers=_auth(raw)).json()
        assert body["hours"] == 24 * 30
