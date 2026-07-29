"""
Tests for config-derived functions in agent_rules.

Covers:
- governance_file_paths() returns correct paths when governance_repo is set.
- governance_file_paths() returns empty list when governance_repo is None.
- vault_security_path() returns correct path when obsidian_vault is absolute.
- vault_security_path() returns None when obsidian_vault is relative.
- load_cross_cutting_rules() resolves the enforced policy statements from the
  `cross_cutting_rules:` key of roles.yaml, with an explicit absent-vs-empty
  distinction, and ships an engine default free of any tenant-specific content.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

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


# ---------------------------------------------------------------------------
# Cross-cutting rules: config-owned, engine default tenant-free
# ---------------------------------------------------------------------------
# HivePilot is a generic orchestrator for ANY project and ANY organisation.
# These tests assert the MECHANISM (rules resolve from config; documented
# absent-vs-empty semantics; the floor reaches unknown roles) plus one
# content assertion that only ever gets STRICTER: that the engine default
# contains no tenant-specific policy.
# ---------------------------------------------------------------------------


@pytest.fixture
def roles_yaml(tmp_path: Path):
    """Point ``settings.roles_file`` at a throwaway roles.yaml.

    Yields a writer taking raw YAML text. Not calling the writer leaves the
    file absent, which is the "nothing configured at all" case. ``config_repo``
    is pinned to None so the resolution chain cannot pick up a real config repo
    on the machine running the tests.
    """
    path = tmp_path / "roles.yaml"

    def write(content: str) -> Path:
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        return path

    with patch.object(settings, "roles_file", path), patch.object(settings, "config_repo", None):
        yield write


class TestLoadCrossCuttingRules:
    """The enforced policy statements must come from config, not from engine code."""

    def test_configured_rules_are_used(self, roles_yaml):
        roles_yaml("""
            cross_cutting_rules:
              - "Ship nothing on a Friday."
              - "Every artifact names a human owner."
            roles: []
        """)
        assert agent_rules.load_cross_cutting_rules() == [
            "Ship nothing on a Friday.",
            "Every artifact names a human owner.",
        ]

    def test_configured_rules_replace_the_engine_default(self, roles_yaml):
        """Config REPLACES the default — it is not merged on top of it, so a
        deployment can genuinely retire a statement the engine shipped."""
        roles_yaml("""
            cross_cutting_rules:
              - "Only this rule."
        """)
        rules = agent_rules.load_cross_cutting_rules()
        assert rules == ["Only this rule."]
        for engine_rule in agent_rules.ENGINE_DEFAULT_CROSS_CUTTING_RULES:
            assert engine_rule not in rules

    def test_returns_a_fresh_list_callers_cannot_mutate_shared_state(self, roles_yaml):
        roles_yaml("cross_cutting_rules:\n  - 'A rule.'\n")
        first = agent_rules.load_cross_cutting_rules()
        first.append("injected by a caller")
        assert agent_rules.load_cross_cutting_rules() == ["A rule."]

    def test_blank_and_whitespace_only_entries_are_filtered(self, roles_yaml):
        roles_yaml("""
            cross_cutting_rules:
              - "  A real rule.  "
              - ""
              - "   "
        """)
        assert agent_rules.load_cross_cutting_rules() == ["A real rule."]


class TestCrossCuttingRulesAbsentVsEmpty:
    """Absent config and an explicitly empty list must NOT mean the same thing.

    This repository's most-repeated bug is a gate reading an absent/empty value
    as "no constraint" and failing OPEN. The floor is config-owned now, so the
    two cases are separated deliberately: silence keeps the engine floor, a
    written-down `[]` is an organisation legitimately declaring it has no
    cross-cutting rules.
    """

    def test_missing_roles_file_yields_the_engine_default(self, roles_yaml):
        # Fixture applied but never written -> the file does not exist.
        assert agent_rules.load_cross_cutting_rules() == list(
            agent_rules.ENGINE_DEFAULT_CROSS_CUTTING_RULES
        )

    def test_roles_file_without_the_key_yields_the_engine_default(self, roles_yaml):
        roles_yaml("""
            roles:
              - name: developer
        """)
        assert agent_rules.load_cross_cutting_rules() == list(
            agent_rules.ENGINE_DEFAULT_CROSS_CUTTING_RULES
        )

    def test_explicit_empty_list_means_genuinely_no_rules(self, roles_yaml):
        """An organisation with no cross-cutting rules is legitimate, and it
        said so explicitly in a reviewable, diffable config file."""
        roles_yaml("""
            cross_cutting_rules: []
            roles: []
        """)
        assert agent_rules.load_cross_cutting_rules() == []

    def test_bare_null_key_is_treated_as_absent_not_as_empty(self, roles_yaml, caplog):
        """`cross_cutting_rules:` with nothing under it parses as None. That is
        far more likely a half-finished edit than a decision to drop every
        rule, so it keeps the engine floor and warns. "No rules at all" has an
        unambiguous spelling: `[]`."""
        roles_yaml("""
            cross_cutting_rules:
            roles: []
        """)
        with caplog.at_level("WARNING"):
            rules = agent_rules.load_cross_cutting_rules()
        assert rules == list(agent_rules.ENGINE_DEFAULT_CROSS_CUTTING_RULES)
        assert rules != []
        assert "cross_cutting_rules_null" in caplog.text

    @pytest.mark.parametrize(
        "body",
        [
            "cross_cutting_rules: 'a single string, not a list'\n",
            "cross_cutting_rules:\n  key: value\n",
            "cross_cutting_rules:\n  - 42\n",
            "cross_cutting_rules:\n  - true\n",
            "cross_cutting_rules:\n  - 'ok'\n  - ['nested']\n",
        ],
    )
    def test_malformed_value_falls_back_to_the_engine_default(self, roles_yaml, body, caplog):
        """A typo must never leave a deployment with FEWER rules than the
        engine ships — the fallback direction is toward the floor, never
        toward []. Mirrors hivepilot.roles.load_roles degrading to its own
        code-owned defaults on any load failure."""
        roles_yaml(body)
        with caplog.at_level("WARNING"):
            rules = agent_rules.load_cross_cutting_rules()
        assert rules == list(agent_rules.ENGINE_DEFAULT_CROSS_CUTTING_RULES)
        assert "cross_cutting_rules_malformed" in caplog.text

    def test_unparseable_yaml_falls_back_to_the_engine_default(self, roles_yaml, caplog):
        roles_yaml("cross_cutting_rules: [unclosed\nroles: :::\n")
        with caplog.at_level("WARNING"):
            rules = agent_rules.load_cross_cutting_rules()
        assert rules == list(agent_rules.ENGINE_DEFAULT_CROSS_CUTTING_RULES)
        assert "cross_cutting_rules_unreadable" in caplog.text


class TestEngineDefaultIsTenantFree:
    """Regression guard: this is what the whole change exists to protect.

    The engine default previously carried one customer's policy — an
    English-only artifact mandate, a `code-review-graph` MCP instruction, a
    detection-fabric mandate and an EU-sovereignty stance — injected into
    every agent prompt of every deployment. If any of it comes back, this
    fails.
    """

    # Substrings that identify org-, jurisdiction-, language- or
    # tooling-specific policy. Several of these are already listed in
    # scripts/public-denylist.txt as org-specific content.
    FORBIDDEN = [
        "detection-fabric",
        "detection fabric",
        "sovereign",
        "eu-hosted",
        "eu-governed",
        "european",
        "code-review-graph",
        "mcp",
        "english",
    ]

    def test_engine_default_contains_no_tenant_specific_content(self):
        combined = " ".join(agent_rules.ENGINE_DEFAULT_CROSS_CUTTING_RULES).lower()
        for marker in self.FORBIDDEN:
            assert marker not in combined, (
                f"Engine default cross-cutting rules must not contain tenant-specific "
                f"content, found {marker!r} in: {combined!r}. HivePilot is a generic "
                f"orchestrator — organisation policy belongs in that organisation's "
                f"roles.yaml, not in engine code."
            )

    def test_engine_default_names_no_tool_the_engine_cannot_provision(self):
        """The dropped `code-review-graph MCP` rule instructed every agent to
        use an MCP server HivePilot has no mechanism to install, detect or
        configure — an instruction agents could not follow."""
        combined = " ".join(agent_rules.ENGINE_DEFAULT_CROSS_CUTTING_RULES).lower()
        assert "mcp" not in combined
        assert "code-review-graph" not in combined

    def test_engine_default_is_an_immutable_tuple(self):
        assert isinstance(agent_rules.ENGINE_DEFAULT_CROSS_CUTTING_RULES, tuple)
        assert all(isinstance(r, str) for r in agent_rules.ENGINE_DEFAULT_CROSS_CUTTING_RULES)

    def test_engine_default_is_non_empty(self):
        """A zero-config deployment still gets a floor. Not because 'empty is
        forbidden' (an explicit `[]` is honoured), but because silence must
        never be the thing that empties it."""
        assert len(agent_rules.ENGINE_DEFAULT_CROSS_CUTTING_RULES) > 0


class TestUnknownRoleFloorFollowsConfig:
    """The unknown-role floor must be the DEPLOYMENT's rules, not hardcoded ones."""

    def test_unknown_role_receives_the_configured_floor(self, monkeypatch):
        configured = ["Deployment-specific rule one.", "Deployment-specific rule two."]
        monkeypatch.setattr(agent_rules, "CROSS_CUTTING_RULES", configured)
        assert agent_rules.get_rules_for_role("no_such_role") == configured

    def test_unknown_role_floor_is_a_copy_not_the_live_list(self, monkeypatch):
        configured = ["A rule."]
        monkeypatch.setattr(agent_rules, "CROSS_CUTTING_RULES", configured)
        returned = agent_rules.get_rules_for_role("no_such_role")
        returned.append("mutated")
        assert agent_rules.CROSS_CUTTING_RULES == ["A rule."]

    def test_unknown_role_floor_is_empty_when_deployment_declared_no_rules(self, monkeypatch):
        """Honouring an explicit `[]` all the way through: the floor's job is to
        stop an unknown role escaping the rules THIS deployment set, not to
        invent rules it chose not to have."""
        monkeypatch.setattr(agent_rules, "CROSS_CUTTING_RULES", [])
        assert agent_rules.get_rules_for_role("no_such_role") == []

    def test_module_level_rules_match_the_loader(self):
        """The import-time constant is exactly what the loader resolves — no
        second, drifting source of truth."""
        assert agent_rules.CROSS_CUTTING_RULES == agent_rules.load_cross_cutting_rules()
