"""HP-65: verify-then-save a cloud API key. Never writes on a failed check."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from hivepilot.services import model_connect as mc
from hivepilot.services import model_verify as mv


def _ok(**kw):
    return mv.VerifyResult(
        ok=True, target="openai", detail="HTTP 200 · 1 models", models=["gpt-x"], **kw
    )


def _fail(**kw):
    return mv.VerifyResult(
        ok=False, target="openai", detail="HTTP 401 (key rejected?)", error="401", **kw
    )


class TestConnect:
    def test_writes_env_after_verify(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(mv, "verify", lambda *a, **k: _ok())
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        env = tmp_path / ".env"
        env.write_text("OTHER=keep\n", encoding="utf-8")
        result = mc.connect("openai", "sk-secret-value", env_path=env)
        assert result.ok is True
        assert result.saved is True
        assert result.env_key == "OPENAI_API_KEY"
        assert "sk-secret-value" not in result.detail
        text = env.read_text(encoding="utf-8")
        assert "OPENAI_API_KEY=sk-secret-value" in text
        assert "OTHER=keep" in text
        assert oct(env.stat().st_mode & 0o777) == "0o600"

    def test_failed_verify_does_not_write(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(mv, "verify", lambda *a, **k: _fail())
        env = tmp_path / ".env"
        result = mc.connect("openai", "sk-bad", env_path=env)
        assert result.ok is False
        assert result.saved is False
        assert not env.exists()

    def test_rejects_local_provider(self, tmp_path: Path):
        with pytest.raises(mc.ConnectError, match="no API key"):
            mc.connect("ollama", "unused", env_path=tmp_path / ".env")

    def test_rejects_unknown_provider(self):
        with pytest.raises(mc.ConnectError, match="unknown provider"):
            mc.connect("myster", "sk-x")

    def test_rejects_empty_key(self):
        with pytest.raises(mc.ConnectError, match="required"):
            mc.connect("openai", "   ")

    def test_rejects_ssrf_base_url(self):
        with pytest.raises(mc.ConnectError, match="loopback"):
            mc.connect("openai", "sk-x", base_url="http://169.254.169.254/")

    def test_sets_process_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(mv, "verify", lambda *a, **k: _ok())
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        mc.connect("openrouter", "sk-or", env_path=tmp_path / ".env")
        assert __import__("os").environ["OPENROUTER_API_KEY"] == "sk-or"

    def test_fingerprint_is_not_the_key(self):
        fp = mc.key_fingerprint("sk-super-secret")
        assert "sk-super" not in fp
        assert len(fp) == 12


def test_cli_connect_writes_env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(mv, "verify", lambda *a, **k: _ok())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env = tmp_path / ".env"
    from hivepilot.cli import app

    result = CliRunner().invoke(
        app,
        [
            "model",
            "connect",
            "--provider",
            "openai",
            "--api-key",
            "sk-cli-secret",
            "--env-file",
            str(env),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "sk-cli-secret" not in result.output
    assert "OPENAI_API_KEY" in result.output
    assert "OPENAI_API_KEY=sk-cli-secret" in env.read_text(encoding="utf-8")
