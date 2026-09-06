"""HP-64 curated OpenAPI contract + GET /v1/schedules."""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services.openapi_contract import (
    CONTRACT_PATH_PREFIXES,
    dumps_contract,
    export_contract,
    is_contract_path,
)
from hivepilot.services.token_service import add_token


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _auth(raw_token: str) -> dict:
    return {"Authorization": f"Bearer {raw_token}"}


class TestContractFilter:
    def test_keeps_roles_concierge_schedules(self):
        schema = export_contract()
        paths = set(schema["paths"])
        assert "/v1/roles" in paths
        assert "/v1/roles/{name}" in paths
        assert "/v1/concierge" in paths
        assert "/v1/schedules" in paths
        assert "/v1/webhook/trigger/{schedule_name}" in paths
        assert "/v1/hindsight/roles/{role}" not in paths
        assert "/roles" not in paths

    def test_prefixes_do_not_match_hindsight_roles(self):
        assert is_contract_path("/v1/roles")
        assert is_contract_path("/v1/roles/{name}")
        assert not is_contract_path("/v1/hindsight/roles/{role}")
        assert CONTRACT_PATH_PREFIXES[0] == "/v1/roles"

    def test_dumps_is_deterministic(self):
        assert dumps_contract() == dumps_contract()

    def test_response_models_are_named(self):
        schema = export_contract()
        components = schema.get("components", {}).get("schemas", {})
        for name in (
            "ConciergeDecisionOut",
            "ConciergeAsk",
            "RoleWrite",
            "RoleListResponse",
            "ScheduleListResponse",
            "TriggerResponse",
        ):
            assert name in components, name


class TestSchedulesEndpoint:
    def test_list_requires_auth(self, api_client):
        assert api_client.get("/v1/schedules").status_code == 401

    def test_list_returns_yaml_entries(self, api_client, tmp_tokens_file, tmp_path, monkeypatch):
        from hivepilot.config import settings

        schedules = tmp_path / "schedules.yaml"
        schedules.write_text(
            yaml.safe_dump(
                {
                    "schedules": {
                        "docs-weekly": {
                            "task": "docs",
                            "projects": ["example-api"],
                            "interval_minutes": 60,
                            "enabled": True,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(settings, "schedules_file", schedules)
        raw, _ = add_token("read")
        resp = api_client.get("/v1/schedules", headers=_auth(raw))
        assert resp.status_code == 200
        body = resp.json()
        assert body["schedules"][0]["name"] == "docs-weekly"
        assert body["schedules"][0]["task"] == "docs"
        assert body["schedules"][0]["interval_minutes"] == 60
        assert body["schedules"][0]["last_run_at"] is None

    def test_trigger_404_unknown(self, api_client, tmp_tokens_file, tmp_path, monkeypatch):
        from hivepilot.config import settings

        schedules = tmp_path / "schedules.yaml"
        schedules.write_text(yaml.safe_dump({"schedules": {}}), encoding="utf-8")
        monkeypatch.setattr(settings, "schedules_file", schedules)
        raw, _ = add_token("run")
        resp = api_client.post("/v1/webhook/trigger/missing", headers=_auth(raw))
        assert resp.status_code == 404
