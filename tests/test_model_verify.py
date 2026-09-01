"""Verify-before-save (HP-78): a connection check that never persists and never
raises. HTTP is mocked at the single `_get_json` chokepoint."""

from __future__ import annotations

import urllib.error

import pytest

from hivepilot.services import model_verify as mv


def _ok(data):
    def _fake(url, headers, timeout):  # noqa: ANN001
        return 200, data

    return _fake


def _http_error(code):
    def _fake(url, headers, timeout):  # noqa: ANN001
        raise urllib.error.HTTPError(url, code, "denied", {}, None)

    return _fake


class TestOpenAiCompatible:
    def test_200_with_models_is_ok(self, monkeypatch):
        monkeypatch.setattr(mv, "_get_json", _ok({"data": [{"id": "gpt-x"}, {"id": "gpt-y"}]}))
        res = mv.verify_openai_compatible("https://api.openai.com/v1", "sk-test")
        assert res.ok is True
        assert res.models == ["gpt-x", "gpt-y"]

    def test_rejected_key_is_not_ok(self, monkeypatch):
        monkeypatch.setattr(mv, "_get_json", _http_error(401))
        res = mv.verify_openai_compatible("https://api.openai.com/v1", "bad")
        assert res.ok is False
        assert "401" in res.detail

    def test_unreachable_is_a_status_not_a_crash(self, monkeypatch):
        def _boom(url, headers, timeout):  # noqa: ANN001
            raise OSError("connection refused")

        monkeypatch.setattr(mv, "_get_json", _boom)
        res = mv.verify_openai_compatible("http://localhost:11434/v1", None)
        assert res.ok is False
        assert res.detail == "unreachable"


class TestDispatch:
    def test_openai_family_uses_models_endpoint(self, monkeypatch):
        monkeypatch.setattr(mv, "_get_json", _ok({"data": [{"id": "m"}]}))
        assert mv.verify("openai", api_key="k").ok is True
        assert mv.verify("openrouter", api_key="k").target == "openrouter"

    def test_ollama_defaults_to_local_endpoint(self, monkeypatch):
        seen = {}

        def _capture(url, headers, timeout):  # noqa: ANN001
            seen["url"] = url
            return 200, {"data": [{"id": "llama3.2"}]}

        monkeypatch.setattr(mv, "_get_json", _capture)
        res = mv.verify("ollama")
        assert res.ok is True
        assert seen["url"].startswith("http://localhost:11434/v1")

    def test_base_url_override_wins(self, monkeypatch):
        seen = {}

        def _capture(url, headers, timeout):  # noqa: ANN001
            seen["url"] = url
            return 200, {"data": []}

        monkeypatch.setattr(mv, "_get_json", _capture)
        mv.verify("openai", base_url="http://proxy:9/v1", api_key="k")
        assert seen["url"] == "http://proxy:9/v1/models"

    def test_anthropic_uses_x_api_key(self, monkeypatch):
        seen = {}

        def _capture(url, headers, timeout):  # noqa: ANN001
            seen["headers"] = headers
            return 200, {"data": [{"id": "claude-x"}]}

        monkeypatch.setattr(mv, "_get_json", _capture)
        res = mv.verify("anthropic", api_key="sk-ant")
        assert res.ok is True
        assert seen["headers"]["x-api-key"] == "sk-ant"
        assert seen["headers"]["anthropic-version"]

    def test_google_requires_a_key(self):
        assert mv.verify("google").ok is False

    def test_unknown_provider_is_honest(self):
        res = mv.verify("myster-provider")
        assert res.ok is False
        assert "not implemented" in res.detail


class TestKeyResolution:
    def test_env_var_is_used_when_no_explicit_key(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env")
        monkeypatch.setattr(mv, "_get_json", lambda url, headers, timeout: (200, {"data": []}))
        # would fail if the key weren't resolved into a Bearer header path
        assert mv.verify("openrouter").ok is True

    def test_openai_compatible_falls_back_to_openai_key(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fallback")
        assert mv._resolve_key("ollama", None) == "sk-fallback"


class TestAgentSession:
    def test_present_session_is_ok(self, monkeypatch):
        from hivepilot.services import agent_auth

        monkeypatch.setattr(agent_auth, "auth_state", lambda k: "present")
        res = mv.verify_agent("claude")
        assert res.ok is True and res.target == "agent:claude"

    def test_absent_session_is_not_ok_but_no_crash(self, monkeypatch):
        from hivepilot.services import agent_auth

        monkeypatch.setattr(agent_auth, "auth_state", lambda k: "absent")
        res = mv.verify_agent("codex")
        assert res.ok is False
        assert "absent" in res.detail


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google", "ollama", "bogus"])
def test_verify_never_raises(provider, monkeypatch):
    def _boom(url, headers, timeout):  # noqa: ANN001
        raise RuntimeError("network down")

    monkeypatch.setattr(mv, "_get_json", _boom)
    result = mv.verify(provider, api_key="k")
    assert isinstance(result, mv.VerifyResult)  # a status, never an exception
