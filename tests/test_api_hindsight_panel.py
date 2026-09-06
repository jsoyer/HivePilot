"""HTTP contract for HP-55 `/v1/hindsight/*` Pollen Memory panel."""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

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


@pytest.fixture()
def read_token(tmp_tokens_file):
    raw, _ = add_token("read")
    return raw


@pytest.fixture()
def run_token(tmp_tokens_file):
    raw, _ = add_token("run")
    return raw


class _FakePanelClient:
    def list_mental_models(self, bank_id, **kwargs):
        return {
            "items": [
                {
                    "id": "prefs",
                    "name": "Preferences",
                    "source_query": "What does the user prefer?",
                    "content": "Dark mode.",
                    "tags": ["ui"],
                }
            ]
        }

    def list_memories(self, bank_id, **kwargs):
        return {
            "items": [
                {
                    "id": "obs-1",
                    "text": "Prefers dark mode",
                    "fact_type": "observation",
                    "proof_count": 1,
                    "confidence": 0.9,
                    "proofs": [{"text": "use dark theme", "id": "w1"}],
                    "evidence": [{"id": "w1", "text": "use dark theme", "fact_type": "world"}],
                }
            ]
        }

    def create_mental_model(self, bank_id, **kwargs):
        return {"id": "new", "name": kwargs["name"], "source_query": kwargs["source_query"]}

    def update_mental_model(self, bank_id, mental_model_id, **kwargs):
        return {"id": mental_model_id, **kwargs}

    def refresh_mental_model(self, bank_id, mental_model_id):
        return {"operation_id": "op-1"}

    def update_memory(self, bank_id, memory_id, **kwargs):
        return {"id": memory_id, "text": kwargs.get("text"), "fact_type": "world"}


class TestHindsightPanelAuth:
    def test_status_requires_auth(self, api_client):
        assert api_client.get("/v1/hindsight/status").status_code == 401

    def test_role_panel_requires_auth(self, api_client):
        assert api_client.get("/v1/hindsight/roles/developer").status_code == 401

    def test_create_requires_run(self, api_client, read_token):
        resp = api_client.post(
            "/v1/hindsight/roles/developer/mental-models",
            headers=_auth(read_token),
            json={"name": "x", "source_query": "y"},
        )
        assert resp.status_code == 403


class TestHindsightPanelReads:
    def test_status_unconfigured(self, api_client, read_token, monkeypatch):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "hindsight_enabled", False)
        resp = api_client.get("/v1/hindsight/status", headers=_auth(read_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is False
        assert "roles" in data
        assert any(row["name"] == "developer" for row in data["roles"])

    def test_unknown_role_404(self, api_client, read_token):
        resp = api_client.get("/v1/hindsight/roles/not-a-real-role-xyz", headers=_auth(read_token))
        assert resp.status_code == 404

    def test_role_panel_shape(self, api_client, read_token, monkeypatch):
        from hivepilot.services import hindsight_panel

        monkeypatch.setattr(hindsight_panel, "_enabled", lambda: True)
        monkeypatch.setattr(hindsight_panel, "default_client", lambda: _FakePanelClient())
        resp = api_client.get("/v1/hindsight/roles/developer", headers=_auth(read_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is True
        assert data["role"] == "developer"
        assert data["bank_id"] == "role:developer"
        assert set(data["mental_models"][0].keys()) == {
            "id",
            "name",
            "source_query",
            "content",
            "last_refreshed_at",
            "is_stale",
            "tags",
        }
        obs = data["observations"][0]
        assert set(obs.keys()) == {
            "id",
            "text",
            "fact_type",
            "state",
            "proof_count",
            "confidence",
            "quotes",
            "evidence",
            "edited_at",
        }
        assert obs["proof_count"] == 1
        assert obs["quotes"][0]["text"] == "use dark theme"


class TestHindsightPanelWrites:
    def test_create_update_refresh_and_curate(self, api_client, run_token, monkeypatch):
        from hivepilot.services import hindsight_panel

        fake = _FakePanelClient()
        monkeypatch.setattr(hindsight_panel, "_enabled", lambda: True)
        monkeypatch.setattr(hindsight_panel, "default_client", lambda: fake)

        created = api_client.post(
            "/v1/hindsight/roles/developer/mental-models",
            headers=_auth(run_token),
            json={"name": "Prefs", "source_query": "What does the user prefer?"},
        )
        assert created.status_code == 200
        assert created.json()["ok"] is True
        assert created.json()["mental_model"]["name"] == "Prefs"

        patched = api_client.patch(
            "/v1/hindsight/roles/developer/mental-models/prefs",
            headers=_auth(run_token),
            json={"name": "User prefs"},
        )
        assert patched.status_code == 200
        assert patched.json()["mental_model"]["name"] == "User prefs"

        refreshed = api_client.post(
            "/v1/hindsight/roles/developer/mental-models/prefs/refresh",
            headers=_auth(run_token),
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["operation_id"] == "op-1"

        curated = api_client.patch(
            "/v1/hindsight/roles/developer/memories/w1",
            headers=_auth(run_token),
            json={"text": "Prefers dark mode.", "reason": "typo"},
        )
        assert curated.status_code == 200
        assert curated.json()["ok"] is True

    def test_disabled_create_is_503(self, api_client, run_token, monkeypatch):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "hindsight_enabled", False)
        resp = api_client.post(
            "/v1/hindsight/roles/developer/mental-models",
            headers=_auth(run_token),
            json={"name": "x", "source_query": "y"},
        )
        assert resp.status_code == 503
