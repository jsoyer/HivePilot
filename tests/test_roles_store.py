"""Tests for the store-backed roles layer — Agent Studio Phase 1, slice 1
(HP-25). Covers the `roles` table CRUD + first-boot seeding in
`state_service`. `load_roles()` is NOT yet flipped to store-first (slice 2),
so these tests target the persistence layer directly.

The autouse `_isolate_state_db` fixture (conftest.py) points the DB at a
per-test tmp file, so each test starts with an empty `roles` table.
"""

from __future__ import annotations

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
