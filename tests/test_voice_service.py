"""Voice config + cloud TTS proxy (HP-62)."""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import voice_service
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


class TestPublicConfig:
    def test_defaults_to_browser(self):
        cfg = voice_service.public_config()
        assert cfg["stt"] == "browser"
        assert cfg["tts"] == "browser"
        assert cfg["cloud_tts"] is False
        assert cfg["cloud_stt"] is False

    def test_cloud_tts_needs_key(self, monkeypatch):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "voice_tts_provider", "openai")
        monkeypatch.setattr(settings, "voice_tts_api_key", None)
        assert voice_service.public_config()["cloud_tts"] is False
        monkeypatch.setattr(settings, "voice_tts_api_key", "sk-test")
        assert voice_service.public_config()["cloud_tts"] is True


class TestSynthesize:
    def test_browser_only_raises(self):
        with pytest.raises(ValueError, match="not configured"):
            voice_service.synthesize("hello")

    def test_openai_posts_to_official_url(self, monkeypatch):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "voice_tts_provider", "openai")
        monkeypatch.setattr(settings, "voice_tts_api_key", "sk-test")

        class _Resp:
            content = b"ID3fake"

            def raise_for_status(self) -> None:
                return None

        def fake_post(url, headers=None, json=None, timeout=None):
            assert url == "https://api.openai.com/v1/audio/speech"
            assert headers["Authorization"] == "Bearer sk-test"
            assert json["input"] == "bonjour"
            return _Resp()

        monkeypatch.setattr(voice_service.requests, "post", fake_post)
        assert voice_service.synthesize("bonjour") == b"ID3fake"

    def test_elevenlabs_posts_to_official_url(self, monkeypatch):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "voice_tts_provider", "elevenlabs")
        monkeypatch.setattr(settings, "voice_tts_api_key", "xi-test")

        class _Resp:
            content = b"ID3xi"

            def raise_for_status(self) -> None:
                return None

        def fake_post(url, headers=None, json=None, timeout=None):
            assert url.startswith("https://api.elevenlabs.io/v1/text-to-speech/")
            assert headers["xi-api-key"] == "xi-test"
            assert json["text"] == "salut"
            return _Resp()

        monkeypatch.setattr(voice_service.requests, "post", fake_post)
        assert voice_service.synthesize("salut") == b"ID3xi"

    def test_cartesia_posts_to_official_url(self, monkeypatch):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "voice_tts_provider", "cartesia")
        monkeypatch.setattr(settings, "voice_tts_api_key", "cart-test")

        class _Resp:
            content = b"ID3ca"

            def raise_for_status(self) -> None:
                return None

        def fake_post(url, headers=None, json=None, timeout=None):
            assert url == "https://api.cartesia.ai/tts/bytes"
            assert headers["X-API-Key"] == "cart-test"
            assert json["transcript"] == "hey"
            return _Resp()

        monkeypatch.setattr(voice_service.requests, "post", fake_post)
        assert voice_service.synthesize("hey") == b"ID3ca"

    def test_empty_text_raises(self, monkeypatch):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "voice_tts_provider", "openai")
        monkeypatch.setattr(settings, "voice_tts_api_key", "sk-test")
        with pytest.raises(ValueError, match="text is required"):
            voice_service.synthesize("   ")


class TestVoiceEndpoints:
    def test_config_requires_auth(self, api_client):
        assert api_client.get("/v1/voice/config").status_code == 401

    def test_config_hides_keys(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "voice_tts_provider", "elevenlabs")
        monkeypatch.setattr(settings, "voice_tts_api_key", "xi-secret")
        raw, _ = add_token("read")
        resp = api_client.get("/v1/voice/config", headers=_auth(raw))
        assert resp.status_code == 200
        body = resp.json()
        assert body["tts"] == "elevenlabs"
        assert body["cloud_tts"] is True
        assert "xi-secret" not in resp.text
        assert "api_key" not in body

    def test_tts_404_without_cloud(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        assert (
            api_client.post("/v1/voice/tts", headers=_auth(raw), json={"text": "hi"}).status_code
            == 404
        )
