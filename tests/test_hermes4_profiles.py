"""HP-71: Hermes-4 is a model (OpenRouter / Nous), not the Hermes Agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.models import RunnerDefinition
from hivepilot.orchestrator import _quota_fallback_definition
from hivepilot.services import profile_service

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def profiles():
    profile_service._cache.clear()
    try:
        yield profile_service.load_model_profiles(path=REPO_ROOT / "model_profiles.yaml")
    finally:
        profile_service._cache.clear()


def test_coding_profile_maps_openrouter_and_nous_to_hermes4(profiles):
    coding = profiles["coding"]
    assert coding["openrouter"] == "nousresearch/hermes-4-70b"
    assert coding["nous"] == "Hermes-4-70B"
    assert coding["model"] == "sonnet"


def test_architecture_profile_uses_405b(profiles):
    arch = profiles["architecture"]
    assert arch["openrouter"] == "nousresearch/hermes-4-405b"
    assert arch["nous"] == "Hermes-4-405B"


def test_dedicated_hermes4_profiles_exist(profiles):
    assert profiles["hermes-4"]["openrouter"] == "nousresearch/hermes-4-70b"
    assert profiles["hermes-4-405b"]["nous"] == "Hermes-4-405B"


def test_resolve_profile_model_openrouter_coding(monkeypatch):
    from hivepilot.config import Settings

    profile_service._cache.clear()

    def fake_resolve(self, filename):  # noqa: ANN001
        return REPO_ROOT / "model_profiles.yaml"

    monkeypatch.setattr(Settings, "resolve_config_path", fake_resolve)
    assert (
        profile_service.resolve_profile_model("coding", "openrouter") == "nousresearch/hermes-4-70b"
    )
    assert profile_service.resolve_profile_model("coding", "nous") == "Hermes-4-70B"
    assert profile_service.resolve_profile_model("hermes-4", "openrouter") == (
        "nousresearch/hermes-4-70b"
    )


def test_quota_fallback_to_openrouter_injects_hermes_and_api_mode(monkeypatch):
    """A CLI developer step falling over to openrouter must not send `sonnet`."""
    monkeypatch.setattr(
        "hivepilot.roles.get_role",
        lambda name: type("R", (), {"model_profile": "coding"})(),
    )
    from hivepilot.config import Settings

    profile_service._cache.clear()

    def fake_resolve(self, filename):  # noqa: ANN001
        return REPO_ROOT / "model_profiles.yaml"

    monkeypatch.setattr(Settings, "resolve_config_path", fake_resolve)

    source = RunnerDefinition(name="role:developer", kind="claude", model="sonnet")
    nxt, meta = _quota_fallback_definition(source, role_name="developer", next_kind="openrouter")
    assert nxt.kind == "openrouter"
    assert nxt.model == "nousresearch/hermes-4-70b"
    assert nxt.options["api_model"] == "nousresearch/hermes-4-70b"
    assert nxt.options["mode"] == "api"
    assert nxt.options["api_provider"] == "openrouter"
    assert nxt.command is None
    assert meta == {"model": "nousresearch/hermes-4-70b", "mode": "api"}


def test_default_dev_fallback_ends_with_openrouter():
    from hivepilot.config import Settings

    assert Settings.model_fields["dev_fallback_runners"].default_factory() == [
        "codex",
        "cursor",
        "openrouter",
    ]


def test_quota_fallback_to_cli_kind_does_not_force_api(monkeypatch):
    monkeypatch.setattr(
        "hivepilot.roles.get_role",
        lambda name: type("R", (), {"model_profile": None})(),
    )
    source = RunnerDefinition(name="role:developer", kind="claude", model="sonnet")
    nxt, meta = _quota_fallback_definition(source, role_name="developer", next_kind="codex")
    assert nxt.kind == "codex"
    assert nxt.options.get("mode") != "api"
    assert meta == {}
