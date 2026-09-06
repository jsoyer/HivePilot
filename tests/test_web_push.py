"""Web Push (HP-63) — config gate, subscribe validation, notifier wiring."""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import state_service
from hivepilot.services.token_service import add_token
from hivepilot.services.web_push_service import (
    WebPushNotConfigured,
    list_subscriptions,
    public_config,
    send_web_push_notification,
    upsert_subscription,
)


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
def vapid_on(monkeypatch):
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "web_push_vapid_public_key", "public-test-key")
    monkeypatch.setattr(settings, "web_push_vapid_private_key", "private-test-key")


class TestPublicConfig:
    def test_disabled_without_keys(self):
        cfg = public_config()
        assert cfg == {"enabled": False, "vapid_public_key": None}

    def test_enabled_with_keys(self, vapid_on):
        cfg = public_config()
        assert cfg["enabled"] is True
        assert cfg["vapid_public_key"] == "public-test-key"
        assert "private" not in cfg


class TestSubscribeStore:
    def test_rejects_http_endpoint(self):
        with pytest.raises(ValueError, match="https"):
            upsert_subscription(
                tenant="default",
                endpoint="http://push.example/sub",
                p256dh="k",
                auth="a",
            )

    def test_rejects_localhost(self):
        with pytest.raises(ValueError, match="push service"):
            upsert_subscription(
                tenant="default",
                endpoint="https://localhost/sub",
                p256dh="k",
                auth="a",
            )

    def test_round_trip(self):
        state_service.init_db()
        upsert_subscription(
            tenant="acme",
            endpoint="https://fcm.googleapis.com/fcm/send/abc",
            p256dh="p256",
            auth="auth1",
        )
        rows = list_subscriptions(tenant="acme")
        assert len(rows) == 1
        assert rows[0]["p256dh"] == "p256"
        assert list_subscriptions(tenant="other") == []


class TestSend:
    def test_unconfigured_raises(self):
        with pytest.raises(WebPushNotConfigured):
            send_web_push_notification("hello")


class TestPushEndpoints:
    def test_config_requires_auth(self, api_client):
        assert api_client.get("/v1/push/config").status_code == 401

    def test_config_disabled_for_read_token(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/push/config", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False, "vapid_public_key": None}

    def test_subscribe_404_when_unconfigured(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        resp = api_client.post(
            "/v1/push/subscribe",
            headers=_auth(raw),
            json={
                "endpoint": "https://fcm.googleapis.com/fcm/send/x",
                "keys": {"p256dh": "k", "auth": "a"},
            },
        )
        assert resp.status_code == 404

    def test_subscribe_forbidden_for_read_token(self, api_client, tmp_tokens_file, vapid_on):
        raw, _ = add_token("read")
        resp = api_client.post(
            "/v1/push/subscribe",
            headers=_auth(raw),
            json={
                "endpoint": "https://fcm.googleapis.com/fcm/send/x",
                "keys": {"p256dh": "k", "auth": "a"},
            },
        )
        assert resp.status_code == 403

    def test_subscribe_and_unsubscribe(self, api_client, tmp_tokens_file, vapid_on):
        state_service.init_db()
        raw, _ = add_token("run")
        body = {
            "endpoint": "https://fcm.googleapis.com/fcm/send/xyz",
            "keys": {"p256dh": "k", "auth": "a"},
        }
        assert (
            api_client.post("/v1/push/subscribe", headers=_auth(raw), json=body).status_code == 200
        )
        assert list_subscriptions(tenant="default")[0]["endpoint"].endswith("/xyz")
        assert (
            api_client.post(
                "/v1/push/unsubscribe",
                headers=_auth(raw),
                json={"endpoint": body["endpoint"]},
            ).status_code
            == 200
        )
        assert list_subscriptions(tenant="default") == []
