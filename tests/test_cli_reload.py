"""Tests for `hivepilot reload` (Phase 14c, #249) -- CLI counterpart of
`POST /v1/admin/reload`. Mocks the HTTP call; never hits a real server."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub out optional heavy dependencies before importing hivepilot.cli
# (mirrors test_cli.py's stub list so this file can run standalone).
# ---------------------------------------------------------------------------

_STUBS = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
    "langchain_openai",
    "openai",
    "boto3",
    "docker",
    "telegram",
    "telegram.ext",
    "fastapi",
    "fastapi.responses",
    "fastapi.security",
    "uvicorn",
    "textual",
    "slack_bolt",
    "slack_bolt.adapter",
    "slack_bolt.adapter.fastapi",
    "slack_bolt.adapter.socket_mode",
    "discord",
    "PyNaCl",
    "nacl",
    "nacl.exceptions",
    "nacl.signing",
]

import importlib  # noqa: E402

for _mod in _STUBS:
    if _mod in sys.modules:
        continue
    try:
        importlib.import_module(_mod)
    except Exception:
        sys.modules[_mod] = MagicMock()

import requests  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from hivepilot.cli import app  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text

    def json(self):
        return self._json


class TestReloadCommand:
    def test_reload_success_prints_both_bools(self, monkeypatch) -> None:
        captured = {}

        def fake_post(url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            return _FakeResponse(200, {"roles_reloaded": True, "config_reloaded": True})

        monkeypatch.setattr(requests, "post", fake_post)

        runner = CliRunner()
        result = runner.invoke(app, ["reload", "--token", "sometoken"])

        assert result.exit_code == 0, result.output
        assert "roles_reloaded: True" in result.output
        assert "config_reloaded: True" in result.output
        assert captured["url"].endswith("/v1/admin/reload")
        assert captured["headers"]["Authorization"] == "Bearer sometoken"

    def test_reload_partial_failure_notes_fail_closed(self, monkeypatch) -> None:
        def fake_post(url, headers=None, timeout=None):
            return _FakeResponse(200, {"roles_reloaded": False, "config_reloaded": True})

        monkeypatch.setattr(requests, "post", fake_post)

        runner = CliRunner()
        result = runner.invoke(app, ["reload", "--token", "sometoken"])

        assert result.exit_code == 0
        assert "roles_reloaded: False" in result.output
        assert "kept the previous config" in result.output.lower()

    def test_reload_missing_token_errors(self, monkeypatch) -> None:
        monkeypatch.delenv("HIVEPILOT_API_TOKEN", raising=False)
        runner = CliRunner()
        result = runner.invoke(app, ["reload"])

        assert result.exit_code != 0

    def test_reload_forbidden_role_errors(self, monkeypatch) -> None:
        def fake_post(url, headers=None, timeout=None):
            return _FakeResponse(403, text="Insufficient role")

        monkeypatch.setattr(requests, "post", fake_post)

        runner = CliRunner()
        result = runner.invoke(app, ["reload", "--token", "sometoken"])

        assert result.exit_code != 0
        assert "admin" in result.output.lower()

    def test_reload_unreachable_api_errors_gracefully(self, monkeypatch) -> None:
        def fake_post(url, headers=None, timeout=None):
            raise requests.ConnectionError("connection refused")

        monkeypatch.setattr(requests, "post", fake_post)

        runner = CliRunner()
        result = runner.invoke(app, ["reload", "--token", "sometoken"])

        assert result.exit_code != 0
        assert "could not reach" in result.output.lower()
