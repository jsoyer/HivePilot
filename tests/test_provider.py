"""Tests for hivepilot.forges.provider — the ForgeProvider registry (forge
plugin type, Phase 1). Mirrors hivepilot.registry's RunnerRegistry/
SecretsRegistry pattern: a process-global FORGE_MAP + a fail-closed
resolve_forge(project) lookup."""

from __future__ import annotations

import pytest

from hivepilot.forges.provider import (
    FORGE_MAP,
    ForgeCollisionError,
    ForgeRegistry,
    UnknownForgeError,
    resolve_forge,
)
from hivepilot.models import ProjectConfig


def test_github_is_registered_by_default() -> None:
    """Importing hivepilot.forges registers the built-in GitHub provider under
    'github' -- Phase 1's only concrete forge, and the default for every
    project."""
    assert "github" in FORGE_MAP


def test_resolve_forge_returns_github_by_default(tmp_path) -> None:
    project = ProjectConfig(path=tmp_path)  # forge defaults to "github"
    forge = resolve_forge(project)
    assert forge is FORGE_MAP["github"]
    assert forge.name == "github"


def test_project_config_forge_defaults_to_github(tmp_path) -> None:
    project = ProjectConfig(path=tmp_path)
    assert project.forge == "github"
    assert project.forge_base_url is None


def test_resolve_forge_fails_closed_on_unregistered_forge(tmp_path, monkeypatch) -> None:
    """FAIL-CLOSED: if a project's forge isn't in FORGE_MAP at resolve time
    (e.g. it was unregistered after config load), resolve_forge must raise --
    NEVER silently fall back to GitHub."""
    project = ProjectConfig(path=tmp_path)
    monkeypatch.delitem(FORGE_MAP, "github")
    with pytest.raises(UnknownForgeError, match="github"):
        resolve_forge(project)


def test_forge_registry_rejects_silent_collision() -> None:
    """A second, different provider registered under an existing name without
    override=True must raise -- never silently replace the live provider
    (mirrors RunnerRegistry.register / SecretsRegistry.register)."""

    class _FakeForge:
        name = "github"

    with pytest.raises(ForgeCollisionError):
        ForgeRegistry.register("github", _FakeForge())  # type: ignore[arg-type]


def test_forge_registry_allows_explicit_override() -> None:
    original = FORGE_MAP["github"]

    class _FakeForge:
        name = "github"

    fake = _FakeForge()
    try:
        ForgeRegistry.register("github", fake, override=True)  # type: ignore[arg-type]
        assert FORGE_MAP["github"] is fake
    finally:
        ForgeRegistry.register("github", original, override=True)


def test_known_kinds_includes_github() -> None:
    assert "github" in ForgeRegistry.known_kinds()
