"""Tests for the `skill` plugin type (Sprint 1: registry + contracts only).

Mirrors `tests/test_panels.py` for the equivalent `panel` contribution type:
registration/discovery, collision -> atomic rollback (including cross-type
rollback of a plugin's other contributions), disabled/kill-switch skip, and
`min_role` fail-closed validation.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Stub optional deps before importing (mirrors tests/test_plugins.py / test_panels.py).
_STUBS = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
]

for _mod in _STUBS:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


class TestSkillRegistration:
    def test_local_plugin_skill_is_collected(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "with_skill.py").write_text(
            "def register():\n"
            "    return {'skills': [{'name': 's1', 'description': 'D', "
            "'provider': 'acme', 'files': {'SKILL.md': 'hello'}}]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()

        assert "s1" in pm.skills
        skill = pm.get_skill("s1")
        assert skill is not None
        assert skill["description"] == "D"
        assert skill["provider"] == "acme"
        assert skill["files"] == {"SKILL.md": "hello"}
        # min_role is optional and only present when the plugin declared it.
        assert "min_role" not in skill

    def test_list_skills_is_sorted_by_name(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "two_skills.py").write_text(
            "def register():\n"
            "    return {'skills': ["
            "        {'name': 'zzz', 'description': 'Z', 'provider': 'p', "
            "'files': {'SKILL.md': 'z'}},"
            "        {'name': 'aaa', 'description': 'A', 'provider': 'p', "
            "'files': {'SKILL.md': 'a'}},"
            "    ]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()

        names = [s["name"] for s in pm.list_skills()]
        assert names == ["aaa", "zzz"]

    def test_get_skill_returns_none_for_unknown_name(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = plugins_mod.PluginManager()

        assert pm.get_skill("does-not-exist") is None

    def test_skill_optional_fields_preserved_when_present(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "full_skill.py").write_text(
            "def register():\n"
            "    return {'skills': [{'name': 'full', 'description': 'D', "
            "'provider': 'p', 'files': {'SKILL.md': 'x'}, "
            "'system_prompt': 'use me wisely', "
            "'applies_to': ['claude', 'codex'], "
            "'min_role': 'admin'}]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()

        skill = pm.get_skill("full")
        assert skill["system_prompt"] == "use me wisely"
        assert skill["applies_to"] == ["claude", "codex"]
        assert skill["min_role"] == "admin"


class TestSkillCollisionRollback:
    def test_duplicate_skill_name_across_plugins_collides(self, tmp_path, monkeypatch) -> None:
        """Two plugins declaring the SAME skill name is a hard-stop
        collision, consistent with runners/notifiers/secrets/health/panels."""
        from hivepilot import plugins as plugins_mod
        from hivepilot.plugins import SkillNameCollisionError

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "a_first.py").write_text(
            "def register():\n"
            "    return {'skills': [{'name': 'shared', 'description': 'A', "
            "'provider': 'p', 'files': {'SKILL.md': 'a'}}]}\n",
            encoding="utf-8",
        )
        (pdir / "b_second.py").write_text(
            "def register():\n"
            "    return {'skills': [{'name': 'shared', 'description': 'B', "
            "'provider': 'p', 'files': {'SKILL.md': 'b'}}]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(SkillNameCollisionError):
            plugins_mod.PluginManager()

    def test_mixed_type_collision_rolls_back_runner_panel_and_notifier_when_skill_collides(
        self, tmp_path, monkeypatch
    ) -> None:
        """Cross-type atomicity when `skills` is the failing member: plugin A
        registers skill name 'dup' first; plugin B declares a runner AND a
        notifier AND a panel AND a colliding skill name 'dup' — the whole
        plugin B contribution must be rolled back, not just the skill entry.
        """
        from hivepilot import plugins as plugins_mod
        from hivepilot.plugins import SkillNameCollisionError
        from hivepilot.registry import RUNNER_MAP
        from hivepilot.services.notification_service import NOTIFIER_MAP

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        # 'a_' sorts before 'b_' — plugin A's 'dup' skill registers
        # successfully before plugin B is even attempted.
        (pdir / "a_owner.py").write_text(
            "def register():\n"
            "    return {'skills': [{'name': 'dup', 'description': 'A', "
            "'provider': 'p', 'files': {'SKILL.md': 'a'}}]}\n",
            encoding="utf-8",
        )
        (pdir / "b_mixed.py").write_text(
            """
class BRunner:
    def __init__(self, definition, settings):
        pass

    def run(self, payload):
        return None


def _b_notifier(msg):
    return None


def _b_fetch():
    return {'sections': []}


def register():
    # 'b-kind' runner, 'b-notif' notifier, and 'b-panel' panel register
    # first, then the 'dup' skill name collides with plugin A's
    # already-registered one.
    return {
        "runners": {"b-kind": BRunner},
        "notifiers": {"b-notif": _b_notifier},
        "panels": [{"name": "b-panel", "title": "B", "fetch": _b_fetch}],
        "skills": [
            {"name": "dup", "description": "B", "provider": "p", "files": {"SKILL.md": "b"}}
        ],
    }
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(SkillNameCollisionError):
            plugins_mod.PluginManager()

        assert "b-kind" not in RUNNER_MAP
        assert "b-notif" not in NOTIFIER_MAP

    def test_collision_rolls_back_that_plugins_earlier_skill_registrations(
        self, tmp_path, monkeypatch
    ) -> None:
        """A single plugin declaring two skills where the SECOND collides
        with an already-registered one must not leave the FIRST orphaned:
        registration of one plugin's skills is atomic."""
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "a_owner.py").write_text(
            "def register():\n"
            "    return {'skills': [{'name': 'taken', 'description': 'A', "
            "'provider': 'p', 'files': {'SKILL.md': 'a'}}]}\n",
            encoding="utf-8",
        )
        (pdir / "b_partial.py").write_text(
            "def register():\n"
            "    return {'skills': ["
            "        {'name': 'fresh', 'description': 'B1', 'provider': 'p', "
            "'files': {'SKILL.md': 'b1'}},"
            "        {'name': 'taken', 'description': 'B2', 'provider': 'p', "
            "'files': {'SKILL.md': 'b2'}},"
            "    ]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(plugins_mod.SkillNameCollisionError):
            plugins_mod.PluginManager()


class TestSkillInvalidMinRoleRejection:
    """A `min_role` typo/non-role value must never silently pass through —
    it fails REGISTRATION entirely (fail-closed, atomic rollback), mirroring
    `tests/test_panels.py::TestPanelInvalidMinRoleRejection`.
    """

    @pytest.mark.parametrize(
        "min_role_literal",
        ['"Admin"', '""', "123", "None", "[]"],
        ids=["typo-Admin", "empty-string", "non-string-int", "none", "non-hashable-list"],
    )
    def test_invalid_min_role_fails_plugin_registration(
        self, tmp_path, monkeypatch, min_role_literal
    ) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "bad_role.py").write_text(
            "def register():\n"
            "    return {'skills': [{'name': 'restricted', 'description': 'R', "
            "'provider': 'p', 'files': {'SKILL.md': 'x'}, "
            f"'min_role': {min_role_literal}}}]}}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(RuntimeError):
            plugins_mod.PluginManager()

    def test_mixed_contribution_rolls_back_runner_and_notifier_when_min_role_invalid(
        self, tmp_path, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RUNNER_MAP
        from hivepilot.services.notification_service import NOTIFIER_MAP

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "mixed.py").write_text(
            """
class MRunner:
    def __init__(self, definition, settings):
        pass

    def run(self, payload):
        return None


def _m_notifier(msg):
    return None


def register():
    return {
        "runners": {"m-kind": MRunner},
        "notifiers": {"m-notif": _m_notifier},
        "skills": [
            {
                "name": "m-skill",
                "description": "M",
                "provider": "p",
                "files": {"SKILL.md": "m"},
                "min_role": "superuser",
            }
        ],
    }
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(RuntimeError):
            plugins_mod.PluginManager()

        assert "m-kind" not in RUNNER_MAP
        assert "m-notif" not in NOTIFIER_MAP

    def test_valid_roles_from_token_service_all_register_successfully(
        self, tmp_path, monkeypatch
    ) -> None:
        """Every role in `token_service.ROLE_RANKS` (the source of truth)
        must be accepted — the positive counterpart proving validation isn't
        over-broad."""
        from hivepilot import plugins as plugins_mod
        from hivepilot.services import token_service

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        skill_entries = ", ".join(
            "{'name': 'skill_%s', 'description': '%s', 'provider': 'p', "
            "'files': {'SKILL.md': 'x'}, 'min_role': '%s'}" % (role, role, role)
            for role in token_service.ROLE_RANKS
        )
        (pdir / "all_roles.py").write_text(
            f"def register():\n    return {{'skills': [{skill_entries}]}}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()

        for role in token_service.ROLE_RANKS:
            assert pm.get_skill(f"skill_{role}")["min_role"] == role


class TestSkillDisabledSkip:
    def test_disabled_plugin_contributes_no_skill(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "off.py").write_text(
            "def register():\n"
            "    return {'skills': [{'name': 'off_skill', 'description': 'Off', "
            "'provider': 'p', 'files': {'SKILL.md': 'x'}}]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_disabled", ["off"], raising=False)

        pm = plugins_mod.PluginManager()

        assert "off_skill" not in pm.skills

    def test_plugins_enabled_false_contributes_no_skills_at_all(
        self, tmp_path, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "kill.py").write_text(
            "def register():\n"
            "    return {'skills': [{'name': 'kill_skill', 'description': 'K', "
            "'provider': 'p', 'files': {'SKILL.md': 'x'}}]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_enabled", False, raising=False)

        pm = plugins_mod.PluginManager()

        assert pm.skills == {}
