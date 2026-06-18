"""
Sprint 1.5 — Agent rules tests.

Covers:
- get_rules_for_role() returns entries for all 8 roles.
- Unknown role raises KeyError (documented design choice).
- Every Noxys monorepo root rule path referenced EXISTS on disk (6 files).
- Vault AGENT-DETECTION-FABRIC.md and AGENT-GIT-BRANCH-RULES.md are present
  in the manifest for the roles that require them; existence check is optional
  (vault is external) but both DO exist on disk so we assert existence too.
- CROSS_CUTTING_RULES is non-empty and includes 'English' and 'detection-fabric'
  markers.
- All 8 role names from roles.ROLES are covered by the agent_rules manifest.
"""

from __future__ import annotations

import os

import pytest

NOXYS_ROOT = "/home/jeromesoyer/Documents/Github/noxys"
VAULT_SECURITY = "/home/jeromesoyer/Documents/Github/jsoyer/obsidian-vault/Noxys/08 - Security"

NOXYS_ROOT_RULE_FILES = [
    f"{NOXYS_ROOT}/CLAUDE.md",
    f"{NOXYS_ROOT}/AGENTS.md",
    f"{NOXYS_ROOT}/.cursorrules",
    f"{NOXYS_ROOT}/.windsurfrules",
    f"{NOXYS_ROOT}/GEMINI.md",
    f"{NOXYS_ROOT}/AGENT-GOVERNANCE.md",
]

VAULT_RULE_FILES = [
    f"{VAULT_SECURITY}/AGENT-DETECTION-FABRIC.md",
    f"{VAULT_SECURITY}/AGENT-GIT-BRANCH-RULES.md",
]

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


class TestNoxysRootRulePathsExist:
    """Every Noxys monorepo root rule path referenced must exist on disk."""

    @pytest.mark.parametrize("path", NOXYS_ROOT_RULE_FILES)
    def test_noxys_root_rule_file_exists(self, path: str):
        assert os.path.exists(path), f"Noxys root rule file does not exist on disk: {path}"

    def test_all_six_noxys_root_files_are_in_manifest(self):
        """All 6 Noxys root files must appear in at least one role's rule list."""
        from hivepilot.agent_rules import get_rules_for_role

        all_referenced: set[str] = set()
        for role_name in ALL_ROLE_NAMES:
            all_referenced.update(get_rules_for_role(role_name))

        for path in NOXYS_ROOT_RULE_FILES:
            assert path in all_referenced, (
                f"Noxys root rule '{path}' is not referenced in any role manifest"
            )


class TestVaultRulePathsInManifest:
    """Vault AGENT-*.md paths must be in the manifest for the roles that need them."""

    def test_detection_fabric_in_ciso_manifest(self):
        from hivepilot.agent_rules import get_rules_for_role

        rules = get_rules_for_role("ciso")
        assert any("AGENT-DETECTION-FABRIC.md" in r for r in rules), (
            "CISO manifest must reference AGENT-DETECTION-FABRIC.md"
        )

    def test_git_branch_rules_in_developer_manifest(self):
        from hivepilot.agent_rules import get_rules_for_role

        rules = get_rules_for_role("developer")
        assert any("AGENT-GIT-BRANCH-RULES.md" in r for r in rules), (
            "Developer manifest must reference AGENT-GIT-BRANCH-RULES.md"
        )

    def test_vault_files_exist_on_disk(self):
        """Both vault rule files happen to exist locally; assert their presence."""
        for path in VAULT_RULE_FILES:
            assert os.path.exists(path), f"Vault security rule file does not exist on disk: {path}"

    def test_vault_paths_referenced_in_manifest(self):
        """Both vault paths must appear in at least one role's manifest."""
        from hivepilot.agent_rules import get_rules_for_role

        all_referenced: set[str] = set()
        for role_name in ALL_ROLE_NAMES:
            all_referenced.update(get_rules_for_role(role_name))

        for path in VAULT_RULE_FILES:
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
