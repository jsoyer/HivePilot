"""Tests for hivepilot/forges/__init__.py -- package-level re-exports + the
import-time GitHub-provider registration side effect (forge plugin type,
Phase 1)."""

from __future__ import annotations

from hivepilot.forges import FORGE_MAP, GitHubForge, resolve_forge
from hivepilot.models import ProjectConfig


def test_package_import_registers_github_forge() -> None:
    assert "github" in FORGE_MAP
    assert isinstance(FORGE_MAP["github"], GitHubForge)


def test_package_reexports_resolve_forge(tmp_path) -> None:
    project = ProjectConfig(path=tmp_path)
    assert resolve_forge(project) is FORGE_MAP["github"]
