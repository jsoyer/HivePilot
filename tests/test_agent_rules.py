"""
Agent rules registry tests.

Covers:
- get_rules_for_role() returns entries for all 8 roles.
- Unknown role raises KeyError (documented design choice).
- When governance_repo is configured, the 6 governance file paths appear in the
  manifest and exist on disk; when not configured, the manifest omits file paths.
- Vault AGENT-DETECTION-FABRIC.md and AGENT-GIT-BRANCH-RULES.md are present
  in the manifest for the roles that require them when obsidian_vault is set.
- CROSS_CUTTING_RULES is non-empty and includes 'English' and 'detection-fabric'
  markers.
- All 8 role names from roles.ROLES are covered by the agent_rules manifest.
"""

from __future__ import annotations

import os

import pytest

from hivepilot.config import settings

ALL_ROLE_NAMES = {
    "ceo",
    "chief_of_staff",
    "cto",
    "developer",
    "reviewer",
    "ciso",
    "qa",
    "documentation",
}

# Derive expected paths from settings so tests are deployment-agnostic.
_GOVERNANCE_REPO = settings.governance_repo or ""
_GOVERNANCE_ROOT_RULE_FILES = (
    [
        f"{_GOVERNANCE_REPO}/CLAUDE.md",
        f"{_GOVERNANCE_REPO}/AGENTS.md",
        f"{_GOVERNANCE_REPO}/.cursorrules",
        f"{_GOVERNANCE_REPO}/.windsurfrules",
        f"{_GOVERNANCE_REPO}/GEMINI.md",
        f"{_GOVERNANCE_REPO}/AGENT-GOVERNANCE.md",
    ]
    if _GOVERNANCE_REPO
    else []
)

_OBSIDIAN_VAULT = str(settings.obsidian_vault) if settings.obsidian_vault else ""
_VAULT_SECURITY = (
    os.path.join(_OBSIDIAN_VAULT, "08 - Security")
    if _OBSIDIAN_VAULT and os.path.isabs(_OBSIDIAN_VAULT)
    else ""
)
_VAULT_RULE_FILES = (
    [
        f"{_VAULT_SECURITY}/AGENT-DETECTION-FABRIC.md",
        f"{_VAULT_SECURITY}/AGENT-GIT-BRANCH-RULES.md",
    ]
    if _VAULT_SECURITY
    else []
)


class TestGetRulesForRole:
    """get_rules_for_role() must return a non-empty list for every known role."""

    def test_returns_list_for_all_eight_roles(self):
        from hivepilot.agent_rules import get_rules_for_role

        for role_name in ALL_ROLE_NAMES:
            rules = get_rules_for_role(role_name)
            assert isinstance(rules, list), f"Expected list for role '{role_name}'"
            assert len(rules) > 0, f"Expected non-empty list for role '{role_name}'"

    def test_unknown_role_raises_key_error(self):
        """Design choice: unknown role raises KeyError (mirrors roles.get_role behaviour)."""
        from hivepilot.agent_rules import get_rules_for_role

        with pytest.raises(KeyError):
            get_rules_for_role("nonexistent_role")

    def test_returns_ordered_list_of_strings(self):
        from hivepilot.agent_rules import get_rules_for_role

        for role_name in ALL_ROLE_NAMES:
            rules = get_rules_for_role(role_name)
            for entry in rules:
                assert isinstance(entry, str), (
                    f"Rule entry for '{role_name}' must be a string path, got {type(entry)}"
                )

    def test_no_empty_string_entries_in_any_role(self):
        """Empty strings must not appear in any role manifest (filtered at build time)."""
        from hivepilot.agent_rules import get_rules_for_role

        for role_name in ALL_ROLE_NAMES:
            for entry in get_rules_for_role(role_name):
                assert entry != "", f"Role '{role_name}' manifest contains an empty-string entry"


class TestGovernanceRootRulePaths:
    """Governance rule paths must be config-derived and conditionally in the manifest."""

    @pytest.mark.skipif(
        not _GOVERNANCE_REPO,
        reason="HIVEPILOT_GOVERNANCE_REPO not configured; skipping path-existence checks",
    )
    @pytest.mark.parametrize("path", _GOVERNANCE_ROOT_RULE_FILES)
    def test_governance_root_rule_file_exists(self, path: str):
        assert os.path.exists(path), f"Governance root rule file does not exist on disk: {path}"

    @pytest.mark.skipif(
        not _GOVERNANCE_REPO,
        reason="HIVEPILOT_GOVERNANCE_REPO not configured; skipping manifest-membership checks",
    )
    def test_all_six_governance_files_are_in_manifest(self):
        """All 6 governance files must appear in at least one role's rule list."""
        from hivepilot.agent_rules import get_rules_for_role

        all_referenced: set[str] = set()
        for role_name in ALL_ROLE_NAMES:
            all_referenced.update(get_rules_for_role(role_name))

        for path in _GOVERNANCE_ROOT_RULE_FILES:
            assert path in all_referenced, (
                f"Governance root rule '{path}' is not referenced in any role manifest"
            )

    def test_governance_files_absent_from_manifest_when_repo_not_set(self):
        """When governance_repo is None, no absolute governance paths appear in any manifest."""
        if _GOVERNANCE_REPO:
            pytest.skip("governance_repo is configured; this check does not apply")

        from hivepilot.agent_rules import get_rules_for_role

        vault_sec = _VAULT_SECURITY  # may be "" if vault is not absolute

        for role_name in ALL_ROLE_NAMES:
            for entry in get_rules_for_role(role_name):
                # Absolute .md paths are only OK if they come from the vault.
                if entry.endswith(".md") and os.path.isabs(entry):
                    if vault_sec:
                        assert entry.startswith(vault_sec), (
                            f"Role '{role_name}' contains absolute .md path that is neither"
                            f" from vault nor governance_repo: {entry!r}"
                        )
                    else:
                        pytest.fail(
                            f"Role '{role_name}' contains absolute .md path without"
                            f" any configured source: {entry!r}"
                        )


class TestVaultRulePathsInManifest:
    """Vault AGENT-*.md paths must be in the manifest for the roles that need them."""

    def test_detection_fabric_marker_in_ciso_manifest(self):
        from hivepilot.agent_rules import get_rules_for_role

        rules = get_rules_for_role("ciso")
        if not _VAULT_SECURITY:
            # No vault configured — detection fabric path should not be in manifest.
            assert not any("AGENT-DETECTION-FABRIC.md" in r for r in rules), (
                "CISO manifest must not reference AGENT-DETECTION-FABRIC.md when vault is not configured"
            )
        else:
            assert any("AGENT-DETECTION-FABRIC.md" in r for r in rules), (
                "CISO manifest must reference AGENT-DETECTION-FABRIC.md"
            )

    def test_git_branch_rules_marker_in_developer_manifest(self):
        from hivepilot.agent_rules import get_rules_for_role

        rules = get_rules_for_role("developer")
        if not _VAULT_SECURITY:
            assert not any("AGENT-GIT-BRANCH-RULES.md" in r for r in rules), (
                "Developer manifest must not reference AGENT-GIT-BRANCH-RULES.md when vault is not configured"
            )
        else:
            assert any("AGENT-GIT-BRANCH-RULES.md" in r for r in rules), (
                "Developer manifest must reference AGENT-GIT-BRANCH-RULES.md"
            )

    @pytest.mark.skipif(
        not _VAULT_SECURITY,
        reason="Obsidian vault not configured as absolute path; skipping vault file existence checks",
    )
    def test_vault_files_exist_on_disk(self):
        """Both vault rule files happen to exist locally; assert their presence."""
        for path in _VAULT_RULE_FILES:
            assert os.path.exists(path), f"Vault security rule file does not exist on disk: {path}"

    @pytest.mark.skipif(
        not _VAULT_SECURITY,
        reason="Obsidian vault not configured as absolute path; skipping manifest-membership checks",
    )
    def test_vault_paths_referenced_in_manifest(self):
        """Both vault paths must appear in at least one role's manifest."""
        from hivepilot.agent_rules import get_rules_for_role

        all_referenced: set[str] = set()
        for role_name in ALL_ROLE_NAMES:
            all_referenced.update(get_rules_for_role(role_name))

        for path in _VAULT_RULE_FILES:
            assert path in all_referenced, (
                f"Vault rule '{path}' is not referenced in any role manifest"
            )


class TestCrossCuttingRules:
    """CROSS_CUTTING_RULES must be non-empty and include required markers."""

    def test_cross_cutting_rules_is_non_empty(self):
        from hivepilot.agent_rules import CROSS_CUTTING_RULES

        assert isinstance(CROSS_CUTTING_RULES, list)
        assert len(CROSS_CUTTING_RULES) > 0, "CROSS_CUTTING_RULES must not be empty"

    def test_cross_cutting_rules_contains_english_marker(self):
        from hivepilot.agent_rules import CROSS_CUTTING_RULES

        combined = " ".join(CROSS_CUTTING_RULES).lower()
        assert "english" in combined, "CROSS_CUTTING_RULES must include an 'English' artifacts rule"

    def test_cross_cutting_rules_contains_detection_fabric_marker(self):
        from hivepilot.agent_rules import CROSS_CUTTING_RULES

        combined = " ".join(CROSS_CUTTING_RULES).lower()
        assert "detection-fabric" in combined, (
            "CROSS_CUTTING_RULES must include a 'detection-fabric' rule"
        )

    def test_every_role_includes_cross_cutting_rules(self):
        """Every role's manifest must contain all CROSS_CUTTING_RULES entries."""
        from hivepilot.agent_rules import CROSS_CUTTING_RULES, get_rules_for_role

        for role_name in ALL_ROLE_NAMES:
            rules = get_rules_for_role(role_name)
            for cc_rule in CROSS_CUTTING_RULES:
                assert cc_rule in rules, (
                    f"Role '{role_name}' manifest is missing cross-cutting rule: {cc_rule!r}"
                )


class TestManifestCoversAllRoles:
    """ROLE_RULES manifest must have an entry for every role in the ROLES registry."""

    def test_manifest_keys_match_roles_registry(self):
        from hivepilot.agent_rules import ROLE_RULES
        from hivepilot.roles import ROLES

        assert set(ROLE_RULES.keys()) == set(ROLES.keys()), (
            "ROLE_RULES keys must exactly match ROLES registry keys"
        )
