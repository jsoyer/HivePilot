"""
Tests for config-derived functions in agent_rules.

Covers:
- governance_file_paths() returns correct paths when governance_repo is set.
- governance_file_paths() returns empty list when governance_repo is None.
- vault_security_path() returns correct path when obsidian_vault is absolute.
- vault_security_path() returns None when obsidian_vault is relative.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from hivepilot import agent_rules
from hivepilot.config import settings


class TestGovernanceFilePaths:
    """governance_file_paths() must derive paths entirely from settings."""

    def test_returns_paths_with_configured_repo(self):
        with patch.object(settings, "governance_repo", "/some/repo"):
            with patch.object(settings, "governance_files", ["CLAUDE.md", "AGENTS.md"]):
                paths = agent_rules.governance_file_paths()
        assert paths == ["/some/repo/CLAUDE.md", "/some/repo/AGENTS.md"]

    def test_returns_empty_when_governance_repo_is_none(self):
        with patch.object(settings, "governance_repo", None):
            paths = agent_rules.governance_file_paths()
        assert paths == []

    def test_returns_empty_when_governance_repo_is_empty_string(self):
        with patch.object(settings, "governance_repo", ""):
            paths = agent_rules.governance_file_paths()
        assert paths == []

    def test_uses_default_governance_files_list(self):
        with patch.object(settings, "governance_repo", "/gov"):
            paths = agent_rules.governance_file_paths()
        # Default list has 6 entries; all should be joined under /gov.
        assert len(paths) == 6
        assert all(p.startswith("/gov/") for p in paths)

    def test_path_components_are_strings(self):
        with patch.object(settings, "governance_repo", "/gov"):
            paths = agent_rules.governance_file_paths()
        for p in paths:
            assert isinstance(p, str)

    def test_filename_preserved_in_path(self):
        with patch.object(settings, "governance_repo", "/org/repo"):
            with patch.object(settings, "governance_files", ["AGENT-GOVERNANCE.md"]):
                paths = agent_rules.governance_file_paths()
        assert paths == ["/org/repo/AGENT-GOVERNANCE.md"]


class TestVaultSecurityPath:
    """vault_security_path() must derive path from settings.obsidian_vault."""

    def test_returns_path_when_vault_is_absolute(self):
        # obsidian_vault is the product vault root; vault_security_path() appends
        # "08 - Security" directly (no extra product-name subdirectory — the vault
        # path is already scoped to the product, e.g. .../obsidian-vault/MyProduct).
        with patch.object(settings, "obsidian_vault", Path("/abs/vault")):
            result = agent_rules.vault_security_path()
        assert result == "/abs/vault/08 - Security"

    def test_returns_none_when_vault_is_relative(self):
        with patch.object(settings, "obsidian_vault", Path("relative/vault")):
            result = agent_rules.vault_security_path()
        assert result is None

    def test_returns_none_when_vault_is_none(self):
        with patch.object(settings, "obsidian_vault", None):
            result = agent_rules.vault_security_path()
        assert result is None

    def test_returns_string_not_path_object(self):
        with patch.object(settings, "obsidian_vault", Path("/abs/vault")):
            result = agent_rules.vault_security_path()
        assert isinstance(result, str)


class TestGovernanceRootBackwardCompat:
    """_GOVERNANCE_ROOT module-level variable must be a string (backward-compat shim)."""

    def test_governance_root_is_a_string(self):
        assert isinstance(agent_rules._GOVERNANCE_ROOT, str)

    def test_governance_root_matches_settings_governance_repo(self):
        expected = settings.governance_repo or ""
        assert agent_rules._GOVERNANCE_ROOT == expected
