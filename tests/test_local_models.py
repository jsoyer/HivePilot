"""HP-78 — discover local Ollama/LM Studio + CLI sessions already on the box."""

from __future__ import annotations

from hivepilot.services import local_models
from hivepilot.services.ollama_probe import OllamaProbe


def test_loopback_accepted():
    assert local_models.is_loopback_url("http://127.0.0.1:11434/v1")
    assert local_models.is_loopback_url("http://localhost:1234/v1")
    assert not local_models.is_loopback_url("http://10.0.0.8:11434/v1")
    assert not local_models.is_loopback_url("https://evil.example/v1")


def test_verify_target_allows_known_cloud_and_loopback():
    assert local_models.verify_target_allowed("https://api.openai.com/v1")
    assert local_models.verify_target_allowed("http://127.0.0.1:1234/v1")
    assert not local_models.verify_target_allowed("http://169.254.169.254/latest")


def test_discover_refuses_non_loopback_env(monkeypatch):
    monkeypatch.setenv("HIVEPILOT_OLLAMA_BASE_URL", "http://10.0.0.8:11434/v1")
    monkeypatch.setenv("HIVEPILOT_LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")

    def _fake_probe(base_url, timeout=1.5):  # noqa: ANN001
        return OllamaProbe(base_url=base_url, reachable=True, models=["m"])

    monkeypatch.setattr(local_models, "probe_ollama", _fake_probe)
    backends = {b.kind: b for b in local_models.discover()}
    assert backends["ollama"].reachable is False
    assert "loopback" in (backends["ollama"].error or "")
    assert backends["lmstudio"].reachable is True
    assert backends["lmstudio"].models == ["m"]


def test_discover_unreachable_is_a_row(monkeypatch):
    monkeypatch.delenv("HIVEPILOT_OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("HIVEPILOT_LMSTUDIO_BASE_URL", raising=False)

    def _dead(base_url, timeout=1.5):  # noqa: ANN001
        return OllamaProbe(base_url=base_url, reachable=False, error="connection refused")

    monkeypatch.setattr(local_models, "probe_ollama", _dead)
    backends = local_models.discover()
    assert [b.kind for b in backends] == ["ollama", "lmstudio"]
    assert all(b.reachable is False for b in backends)


def test_cli_sessions_include_known_kinds(monkeypatch):
    monkeypatch.setattr(
        "hivepilot.services.agent_auth.auth_state",
        lambda kind: "present" if kind == "claude" else "absent",
    )
    rows = {r["kind"]: r for r in local_models.cli_sessions()}
    assert rows["claude"]["state"] == "present"
    assert rows["codex"]["state"] == "absent"
    assert rows["claude"]["login_available"] is True
