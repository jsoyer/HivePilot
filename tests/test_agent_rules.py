"""
Agent rules registry tests.

Covers:
- get_rules_for_role() returns entries for all 8 roles.
- Unknown role raises KeyError (documented design choice).
- When governance_repo is configured, the 6 governance file paths appear in the
  manifest and exist on disk; when not configured, the manifest omits file paths.
- The deployment's configured vault rule documents are present in the manifest
  for the roles that require them when obsidian_vault is set. The FILENAMES are
  config-owned (`vault_rule_documents:` in roles.yaml), so these tests derive
  them from the resolved config instead of hardcoding one customer's document
  names -- see tests/test_agent_rules_config.py for the resolver semantics.
- CROSS_CUTTING_RULES is inherited by every role, and is whatever the
  deployment's config resolved to (see tests/test_agent_rules_config.py for
  the resolution + absent-vs-empty semantics).
- All 8 role names from roles.ROLES are covered by the agent_rules manifest.
"""

from __future__ import annotations

import os

import pytest

from hivepilot import agent_rules
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
_VAULT_RULE_FILES = [
    path
    for path in (
        agent_rules.vault_rule_document_path(slot) for slot in agent_rules.VAULT_RULE_DOCUMENT_SLOTS
    )
    if path
]


def _deployment_configures_cross_cutting_rules() -> bool:
    """True if the resolved roles.yaml actually declares `cross_cutting_rules:`.

    Lets the "unconfigured deployment gets the engine default" check skip
    itself on a deployment that legitimately overrides the rules, the same
    way the governance/vault checks above skip when their settings are unset.
    """
    import yaml

    path = settings.resolve_config_path(settings.roles_file)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — absent/broken config means "not configured"
        return False
    from hivepilot.agent_rules import CROSS_CUTTING_RULES_KEY

    return isinstance(raw, dict) and raw.get(CROSS_CUTTING_RULES_KEY) is not None


_CROSS_CUTTING_RULES_CONFIGURED = _deployment_configures_cross_cutting_rules()


class TestGetRulesForRole:
    """get_rules_for_role() must return a non-empty list for every known role."""

    def test_returns_list_for_all_eight_roles(self):
        from hivepilot.agent_rules import get_rules_for_role

        for role_name in ALL_ROLE_NAMES:
            rules = get_rules_for_role(role_name)
            assert isinstance(rules, list), f"Expected list for role '{role_name}'"
            assert len(rules) > 0, f"Expected non-empty list for role '{role_name}'"

    def test_unknown_role_returns_cross_cutting_floor(self):
        """Sprint 2 (roles-model-effort-config-owned PRD) changed this from
        KeyError to a safe return: a role absent from ROLE_RULES (e.g. a
        business role not loaded under the reduced generic-only code
        defaults) must never crash a caller that only wants its rule
        manifest. A security review of the first Sprint 2 cut correctly
        flagged returning `[]` as fail-OPEN: it silently dropped the
        CROSS_CUTTING_RULES enforced policy floor that every known role
        already inherits. Corrected expectation:
        `get_rules_for_role(...) == list(CROSS_CUTTING_RULES)` -- fail-safe
        means inheriting the enforced minimum, not returning a policy-free
        empty list. `roles.get_role` (a different function) still raises
        KeyError for a genuinely unknown role.

        The floor is now the DEPLOYMENT's resolved rules rather than a
        hardcoded list. This assertion is unchanged because it always
        compared against CROSS_CUTTING_RULES itself, never against literal
        statements; only the enumeration of that customer's policy names was
        removed from this docstring. See tests/test_agent_rules_config.py::
        TestUnknownRoleFloorFollowsConfig for the config-driven cases."""
        from hivepilot.agent_rules import CROSS_CUTTING_RULES, get_rules_for_role

        assert get_rules_for_role("nonexistent_role") == list(CROSS_CUTTING_RULES)

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

    def test_security_rules_document_in_ciso_manifest(self):
        """CISO inherits the security-rules slot -- whatever THIS deployment
        named it. Asserting the configured path rather than a literal filename
        is the point: the engine no longer knows any document's name."""
        from hivepilot.agent_rules import SLOT_SECURITY_RULES, get_rules_for_role

        self._assert_slot_membership("ciso", SLOT_SECURITY_RULES, get_rules_for_role("ciso"))

    def test_git_branch_rules_document_in_developer_manifest(self):
        from hivepilot.agent_rules import SLOT_GIT_BRANCH_RULES, get_rules_for_role

        self._assert_slot_membership(
            "developer", SLOT_GIT_BRANCH_RULES, get_rules_for_role("developer")
        )

    @staticmethod
    def _assert_slot_membership(role: str, slot: str, rules: list[str]) -> None:
        expected = agent_rules.vault_rule_document_path(slot)
        if not expected:
            # No vault, or the slot is not configured -> nothing from the vault
            # security directory may appear for this slot. Critically, no
            # directory-only or "None.md" path may be constructed either.
            for entry in rules:
                assert not entry.endswith("/"), (
                    f"{role} manifest contains a directory-only path: {entry!r}"
                )
                assert "None.md" not in entry, (
                    f"{role} manifest contains a path built from an absent filename: {entry!r}"
                )
        else:
            assert expected in rules, f"{role} manifest must reference the {slot} document"

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
    """CROSS_CUTTING_RULES is config-owned; assert the MECHANISM, not content.

    These previously asserted the literal statements the engine shipped
    ('English', 'detection-fabric'). That coupled the generic engine to one
    customer's policy: the assertions could only pass while HivePilot
    hardcoded an English-only mandate and a detection-fabric mandate for
    every deployment on earth. They are converted, not deleted — each one's
    real intent (the rules exist, are strings, and reach every role) is
    preserved without pinning WHICH rules a deployment must hold.
    """

    def test_cross_cutting_rules_is_a_list_of_strings(self):
        """Was: 'must not be empty'. A deployment may legitimately declare
        `cross_cutting_rules: []`, so emptiness is no longer a defect of the
        module — what must hold is the shape. That the ENGINE DEFAULT is
        non-empty (so silence never empties the floor) is asserted in
        tests/test_agent_rules_config.py::TestEngineDefaultIsTenantFree."""
        from hivepilot.agent_rules import CROSS_CUTTING_RULES

        assert isinstance(CROSS_CUTTING_RULES, list)
        assert all(isinstance(rule, str) for rule in CROSS_CUTTING_RULES)

    def test_cross_cutting_rules_come_from_config(self):
        """Was: two tests pinning the literal 'English' and 'detection-fabric'
        statements. Replaced by the property that actually matters — the
        statements are resolved from configuration, exactly like the
        config-derived source roots in the same module."""
        from hivepilot.agent_rules import CROSS_CUTTING_RULES, load_cross_cutting_rules

        assert CROSS_CUTTING_RULES == load_cross_cutting_rules()

    @pytest.mark.skipif(
        _CROSS_CUTTING_RULES_CONFIGURED,
        reason="deployment configures cross_cutting_rules; the engine-default check does not apply",
    )
    def test_cross_cutting_rules_are_the_engine_default_when_unconfigured(self):
        """The other half of the two converted content tests: with nothing
        configured (this repo ships no `cross_cutting_rules:` key), the rules
        must be the tenant-free engine default rather than one customer's
        policy. The default's tenant-freedom itself is asserted in
        tests/test_agent_rules_config.py::TestEngineDefaultIsTenantFree."""
        from hivepilot.agent_rules import (
            CROSS_CUTTING_RULES,
            ENGINE_DEFAULT_CROSS_CUTTING_RULES,
        )

        assert CROSS_CUTTING_RULES == list(ENGINE_DEFAULT_CROSS_CUTTING_RULES)

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
