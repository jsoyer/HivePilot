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
    require_secure_forge_url,
    resolve_forge,
    resolve_forge_base_url,
    resolve_forge_token,
)
from hivepilot.models import ProjectConfig
from hivepilot.services.config_provenance import clear_secret_values
from hivepilot.services.secret_refs import SecretReferenceError


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


# ---------------------------------------------------------------------------
# Phase 2 shared helpers: require_secure_forge_url / resolve_forge_base_url /
# resolve_forge_token (used by ForgejoForge/GitLabForge).
# ---------------------------------------------------------------------------


def test_require_secure_forge_url_accepts_https() -> None:
    require_secure_forge_url("https://forge.example.com")  # must not raise


def test_require_secure_forge_url_accepts_loopback_http() -> None:
    require_secure_forge_url("http://localhost:3000")  # must not raise


def test_require_secure_forge_url_rejects_plaintext_non_loopback() -> None:
    with pytest.raises(ValueError, match="Refusing plaintext"):
        require_secure_forge_url("http://forge.example.com")


def test_require_secure_forge_url_rejects_non_http_scheme() -> None:
    with pytest.raises(ValueError, match="http"):
        require_secure_forge_url("ftp://forge.example.com")


def test_resolve_forge_base_url_uses_project_value(tmp_path) -> None:
    project = ProjectConfig(
        path=tmp_path, forge="forgejo", forge_base_url="https://forge.example.com/"
    )
    assert resolve_forge_base_url(project) == "https://forge.example.com"


def test_resolve_forge_base_url_fails_closed_when_unset_and_no_default(tmp_path) -> None:
    project = ProjectConfig(path=tmp_path)  # forge_base_url unset
    with pytest.raises(ValueError, match="forge_base_url"):
        resolve_forge_base_url(project)


def test_resolve_forge_base_url_falls_back_to_given_default(tmp_path) -> None:
    project = ProjectConfig(path=tmp_path)  # forge_base_url unset
    assert resolve_forge_base_url(project, default="https://gitlab.com") == "https://gitlab.com"


def test_resolve_forge_base_url_enforces_https_even_with_default(tmp_path) -> None:
    project = ProjectConfig(path=tmp_path, forge_base_url="http://forge.example.com")
    with pytest.raises(ValueError, match="Refusing plaintext"):
        resolve_forge_base_url(project, default="https://gitlab.com")


@pytest.fixture(autouse=True)
def _clean_secret_registry_for_token_tests():
    clear_secret_values()
    yield
    clear_secret_values()


def test_resolve_forge_token_resolves_via_secret_catalog(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MY_TOKEN_ENV", "top-secret-value")
    project = ProjectConfig(
        path=tmp_path,
        secrets={"SOME_TOKEN": {"source": "env", "key": "MY_TOKEN_ENV"}},
    )
    assert resolve_forge_token(project, "SOME_TOKEN") == "top-secret-value"


def test_resolve_forge_token_fails_closed_when_catalog_entry_missing(tmp_path) -> None:
    project = ProjectConfig(path=tmp_path, secrets={})
    with pytest.raises(SecretReferenceError, match="SOME_TOKEN"):
        resolve_forge_token(project, "SOME_TOKEN")


def test_resolve_forge_token_fails_closed_on_empty_resolved_value(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EMPTY_TOKEN_ENV", "   ")
    project = ProjectConfig(
        path=tmp_path,
        secrets={"SOME_TOKEN": {"source": "env", "key": "EMPTY_TOKEN_ENV"}},
    )
    with pytest.raises(ValueError, match="empty"):
        resolve_forge_token(project, "SOME_TOKEN")
