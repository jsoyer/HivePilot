"""Tests for the store-backed roles layer — Agent Studio Phase 1, slice 1
(HP-25). Covers the `roles` table CRUD + first-boot seeding in
`state_service`. `load_roles()` is NOT yet flipped to store-first (slice 2),
so these tests target the persistence layer directly.

The autouse `_isolate_state_db` fixture (conftest.py) points the DB at a
per-test tmp file, so each test starts with an empty `roles` table.
"""

from __future__ import annotations

from pathlib import Path

from hivepilot import roles as roles_module
from hivepilot.services import state_service


def _ciso(**over) -> dict:
    row = {
        "name": "ciso",
        "title": "CISO",
        "display_name": "Hugo",
        "model_profile": "architecture",
        "runner": "opencode",
        "model": None,
        "models": ["opencode-go/glm-5.2"],
        "prompt_file": "ciso.md",
        "prompt_text": "You are the CISO. Review for security.",
        "inputs": ["implementation", "review_report"],
        "outputs": ["security_report", "clearance"],
        "optional_inputs": [],
        "allowed_tools": None,
        "can_block": True,
        "order": 6,
        "host": None,
        "permission_mode": None,
        "command_task": "ciso",
        "effort": None,
    }
    row.update(over)
    return row


class TestRolesCrud:
    def test_upsert_then_get_round_trips_all_fields(self) -> None:
        state_service.upsert_role(_ciso())
        row = state_service.get_role_row("ciso")
        assert row is not None
        assert row["title"] == "CISO"
        assert row["display_name"] == "Hugo"
        assert row["models"] == ["opencode-go/glm-5.2"]  # JSON round-trip
        assert row["inputs"] == ["implementation", "review_report"]
        assert row["can_block"] is True  # 0/1 -> bool
        assert row["order"] == 6  # role_order -> order
        assert row["prompt_text"].startswith("You are the CISO")

    def test_get_unknown_role_is_none(self) -> None:
        assert state_service.get_role_row("nope") is None

    def test_upsert_replaces_existing(self) -> None:
        state_service.upsert_role(_ciso())
        state_service.upsert_role(_ciso(display_name="Amélie", can_block=False))
        row = state_service.get_role_row("ciso")
        assert row is not None
        assert row["display_name"] == "Amélie"
        assert row["can_block"] is False
        assert state_service.roles_count() == 1  # replaced, not duplicated

    def test_delete_role(self) -> None:
        state_service.upsert_role(_ciso())
        assert state_service.roles_count() == 1
        state_service.delete_role("ciso")
        assert state_service.roles_count() == 0
        assert state_service.get_role_row("ciso") is None

    def test_list_ordered_by_role_order(self) -> None:
        state_service.upsert_role(_ciso(name="qa", order=7))
        state_service.upsert_role(_ciso(name="dev", order=4))
        state_service.upsert_role(_ciso(name="ceo", order=1))
        assert [r["name"] for r in state_service.list_role_rows()] == ["ceo", "dev", "qa"]


class TestRolesSeeding:
    def test_seed_when_empty_then_idempotent(self) -> None:
        rows = [_ciso(name="ceo", order=1), _ciso(name="dev", order=4)]
        assert state_service.seed_roles(rows) == 2
        assert state_service.roles_count() == 2
        # A second seed must NOT clobber live edits — returns 0, writes nothing.
        assert state_service.seed_roles([_ciso(name="other", order=9)]) == 0
        assert state_service.roles_count() == 2
        assert state_service.get_role_row("other") is None


class TestRolesTenantScoping:
    def test_roles_are_isolated_per_tenant(self) -> None:
        state_service.upsert_role(_ciso(tenant="acme"))
        state_service.upsert_role(_ciso(name="dev", tenant="beta", order=4))
        assert [r["name"] for r in state_service.list_role_rows("acme")] == ["ciso"]
        assert [r["name"] for r in state_service.list_role_rows("beta")] == ["dev"]
        assert state_service.get_role_row("ciso", tenant="beta") is None
        assert state_service.roles_count("acme") == 1
        assert state_service.roles_count("default") == 0


class TestStoreFirstRefresh:
    """Slice 2: `refresh_roles()` adopts the store when it has roles, and an
    inline `prompt_text` (no file) is materialized so the role loads."""

    def test_refresh_adopts_store_with_inline_prompt(self) -> None:
        original = dict(roles_module.ROLES)
        try:
            state_service.upsert_role(
                {
                    "name": "auditor",
                    "title": "Auditor",
                    "model_profile": "architecture",
                    "runner": "openai",
                    "inputs": [],
                    "outputs": ["report"],
                    "can_block": True,
                    "order": 2,
                    "prompt_text": "You audit security.",  # inline, no prompt_file
                }
            )
            assert roles_module.refresh_roles() is True
            assert "auditor" in roles_module.ROLES
            role = roles_module.ROLES["auditor"]
            assert role.runner == "openai"
            assert role.can_block is True
            assert role.optional_inputs == []  # NULL column -> model default
            # inline prompt materialized to a real file with the stored text
            assert role.prompt_file.exists()
            assert role.prompt_file.read_text(encoding="utf-8") == "You audit security."
        finally:
            roles_module.ROLES = original

    def test_seed_from_yaml_then_refresh_is_idempotent(self, tmp_path: Path, monkeypatch) -> None:
        import hivepilot.config as config_module

        prompt = tmp_path / "dev.md"
        prompt.write_text("You are the dev.", encoding="utf-8")
        roles_yaml = tmp_path / "roles.yaml"
        roles_yaml.write_text(
            "roles:\n"
            "  - name: developer\n"
            "    title: Developer\n"
            "    prompt_file: dev.md\n"
            "    model_profile: coding\n"
            "    inputs: [spec]\n"
            "    outputs: [impl]\n"
            "    can_block: false\n"
            "    order: 4\n",
            encoding="utf-8",
        )
        mock = type(
            "S",
            (),
            {
                "roles_file": roles_yaml,
                "resolve_config_path": lambda self, f: (
                    roles_yaml if str(f).endswith("roles.yaml") else prompt
                ),
            },
        )()
        original_settings = config_module.settings
        original = dict(roles_module.ROLES)
        try:
            config_module.settings = mock
            assert roles_module.seed_store_from_yaml() == 1
            assert state_service.roles_count() == 1
            assert state_service.get_role_row("developer")["prompt_text"] == "You are the dev."
            assert roles_module.seed_store_from_yaml() == 0  # idempotent
            assert roles_module.refresh_roles() is True
            assert set(roles_module.ROLES.keys()) == {"developer"}
        finally:
            config_module.settings = original_settings
            roles_module.ROLES = original


class TestExportStoreToYaml:
    """Slice 3: `export_store_to_yaml()` writes the store roster back to
    `roles.yaml` + `prompts/agents/*.md` for GitOps (the store is authoritative;
    inline prompt text is written to files)."""

    def test_empty_store_is_a_noop(self, tmp_path: Path) -> None:
        roles_path = tmp_path / "roles.yaml"
        assert roles_module.export_store_to_yaml(roles_path=roles_path) == 0
        assert not roles_path.exists()  # never clobber with an empty roster

    def test_export_writes_yaml_and_prompt_files(self, tmp_path: Path) -> None:
        import yaml

        state_service.upsert_role(_ciso(name="dev", order=4, prompt_file="dev.md"))
        state_service.upsert_role(
            _ciso(name="auditor", order=2, prompt_file=None, prompt_text="Audit inline.")
        )
        roles_path = tmp_path / "roles.yaml"
        prompts_dir = tmp_path / "prompts" / "agents"

        count = roles_module.export_store_to_yaml(roles_path=roles_path, prompts_dir=prompts_dir)
        assert count == 2

        parsed = yaml.safe_load(roles_path.read_text(encoding="utf-8"))
        names = [r["name"] for r in parsed["roles"]]
        assert names == ["auditor", "dev"]  # ordered by role_order

        # inline-only role gets a generated prompt file named after the role
        assert (prompts_dir / "auditor.md").read_text(encoding="utf-8") == "Audit inline."
        # file-referencing role's text is synced to its referenced file
        assert (prompts_dir / "dev.md").read_text(encoding="utf-8").startswith("You are the CISO")

    def test_roundtrip_reloads_equivalently(self, tmp_path: Path, monkeypatch) -> None:
        """Export then point settings at the exported files: reloading yields the
        same roster (files are a faithful projection of the store)."""
        import hivepilot.config as config_module

        state_service.upsert_role(_ciso(name="dev", order=4, prompt_file="dev.md"))
        roles_path = tmp_path / "roles.yaml"
        prompts_dir = tmp_path / "prompts" / "agents"
        assert (
            roles_module.export_store_to_yaml(roles_path=roles_path, prompts_dir=prompts_dir) == 1
        )

        mock = type(
            "S",
            (),
            {
                "roles_file": roles_path,
                "resolve_config_path": lambda self, f: (
                    roles_path if str(f).endswith("roles.yaml") else prompts_dir / Path(str(f)).name
                ),
            },
        )()
        original_settings = config_module.settings
        original = dict(roles_module.ROLES)
        try:
            config_module.settings = mock
            reloaded = roles_module._load_roles_strict()
            assert set(reloaded) == {"dev"}
            assert reloaded["dev"].title == "CISO"
            assert (
                reloaded["dev"]
                .prompt_file.read_text(encoding="utf-8")
                .startswith("You are the CISO")
            )
        finally:
            config_module.settings = original_settings
            roles_module.ROLES = original
