"""HP-73 audit quick-win — local Ollama connectivity probe.

Confirms the probe reports reachability + pulled model ids from the OpenAI-
compatible `/v1/models` endpoint and, crucially, NEVER raises (a dead daemon is
a `reachable=False` status, not a crash)."""

from __future__ import annotations

import json

from hivepilot.services import ollama_probe


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body


class _CtxResp:
    def __init__(self, body: bytes) -> None:
        self._resp = _FakeResp(body)

    def __enter__(self) -> _FakeResp:
        return self._resp

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        return None


def test_reachable_lists_models(monkeypatch):
    body = json.dumps({"data": [{"id": "llama3.2"}, {"id": "qwen2.5-coder"}]}).encode()
    monkeypatch.setattr(
        ollama_probe.urllib.request, "urlopen", lambda url, timeout=None: _CtxResp(body)
    )
    result = ollama_probe.probe_ollama("http://localhost:11434/v1")
    assert result.reachable is True
    assert result.models == ["llama3.2", "qwen2.5-coder"]
    assert result.error is None


def test_unreachable_is_a_status_not_a_crash(monkeypatch):
    def _boom(url, timeout=None):  # noqa: ANN001
        raise OSError("connection refused")

    monkeypatch.setattr(ollama_probe.urllib.request, "urlopen", _boom)
    result = ollama_probe.probe_ollama("http://localhost:11434/v1")
    assert result.reachable is False
    assert result.models == []
    assert "connection refused" in (result.error or "")


def test_default_base_url_honors_env(monkeypatch):
    monkeypatch.setenv("HIVEPILOT_OLLAMA_BASE_URL", "http://box:1234/v1")
    assert ollama_probe.default_base_url() == "http://box:1234/v1"


def test_malformed_payload_yields_no_models(monkeypatch):
    monkeypatch.setattr(
        ollama_probe.urllib.request,
        "urlopen",
        lambda url, timeout=None: _CtxResp(b'{"data": [{"noid": 1}, "junk"]}'),
    )
    result = ollama_probe.probe_ollama()
    assert result.reachable is True
    assert result.models == []
