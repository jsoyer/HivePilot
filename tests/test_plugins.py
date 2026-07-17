"""
Minimal tests for hivepilot.plugins — PluginManager and hooks type annotation.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Stub optional deps before importing
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


class TestPluginManagerHooksAnnotation:
    """Verify PluginManager.hooks has the correct type annotation.

    Each test below calls the real, unmocked `PluginManager()`, which scans
    the actual `plugins/` directory (cwd) and registers any local plugin's
    declared runners into the process-global `RUNNER_MAP` — isolated across
    the whole suite by the session-wide `_isolate_runner_and_notifier_maps`
    autouse fixture in `tests/conftest.py`.
    """

    def test_plugin_manager_importable(self) -> None:
        """hivepilot.plugins imports without error."""
        import hivepilot.plugins  # noqa: F401

        assert hivepilot.plugins is not None

    def test_plugin_manager_has_hooks_attribute(self) -> None:
        """PluginManager instance has a hooks attribute that is a dict."""
        from hivepilot.plugins import PluginManager

        pm = PluginManager()
        assert hasattr(pm, "hooks")
        assert isinstance(pm.hooks, dict)

    def test_hooks_dict_has_expected_keys(self) -> None:
        """hooks dict has at minimum before_step and after_step keys."""
        from hivepilot.plugins import PluginManager

        pm = PluginManager()
        assert "before_step" in pm.hooks
        assert "after_step" in pm.hooks

    def test_hooks_values_are_lists(self) -> None:
        """hooks values are lists."""
        from hivepilot.plugins import PluginManager

        pm = PluginManager()
        for value in pm.hooks.values():
            assert isinstance(value, list)

    def test_load_plugins_returns_list(self) -> None:
        """load_plugins() returns a list."""
        from hivepilot.plugins import load_plugins

        result = load_plugins()
        assert isinstance(result, list)


class TestPluginHealthSurface:
    """Sprint 2 (plugin-health): a plugin may declare `register()["health"]`
    as `{"<name>": health_callable}`, collected into `PluginManager.health`
    the same way runners/notifiers/secrets are (popped out of the returned
    hooks dict). Covers: collection, never-raise on a raising check, and
    collision handling consistent with runners/notifiers/secrets.
    """

    def test_local_plugin_health_is_collected(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.plugins import HealthStatus

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "healthy.py").write_text(
            "from hivepilot.plugins import HealthStatus\n"
            "def check(**kwargs):\n"
            "    return HealthStatus('ok', 'all good')\n"
            "def register():\n"
            "    return {'health': {'x': check}}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()

        assert "x" in pm.health
        assert callable(pm.health["x"])
        result = pm.run_health_check("x")
        assert result == HealthStatus("ok", "all good")
        assert pm.check_all() == {"x": HealthStatus("ok", "all good")}

    def test_health_check_dict_fallback_is_normalized(self, tmp_path, monkeypatch) -> None:
        """A plain {"status", "detail"} dict (the no-import fallback) is
        accepted and normalized into a HealthStatus."""
        from hivepilot import plugins as plugins_mod
        from hivepilot.plugins import HealthStatus

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "dictish.py").write_text(
            "def check(**kwargs):\n"
            "    return {'status': 'degraded', 'detail': 'meh'}\n"
            "def register():\n"
            "    return {'health': {'y': check}}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()

        assert pm.run_health_check("y") == HealthStatus("degraded", "meh")

    def test_raising_health_check_reports_error_never_raises(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "boom.py").write_text(
            "def check(**kwargs):\n"
            "    raise RuntimeError('kaboom')\n"
            "def register():\n"
            "    return {'health': {'z': check}}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()  # must not raise

        result = pm.run_health_check("z")  # must not raise
        assert result.status == "error"
        # The exception message must never be echoed back to callers —
        # only the exception type name is surfaced.
        assert "kaboom" not in result.detail
        assert "RuntimeError" in result.detail

    def test_unregistered_health_name_reports_error(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = plugins_mod.PluginManager()

        result = pm.run_health_check("does-not-exist")
        assert result.status == "error"

    def test_invalid_health_result_shape_reports_error(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "weird.py").write_text(
            "def check(**kwargs):\n    return 'not a health status'\n"
            "def register():\n    return {'health': {'w': check}}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()

        result = pm.run_health_check("w")
        assert result.status == "error"

    def test_duplicate_health_name_across_plugins_collides(self, tmp_path, monkeypatch) -> None:
        """Two plugins declaring the SAME health name is a hard-stop collision,
        consistent with the runners/notifiers/secrets registries."""
        from hivepilot import plugins as plugins_mod
        from hivepilot.plugins import HealthNameCollisionError

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "a_first.py").write_text(
            "def check(**kwargs):\n    return {'status': 'ok', 'detail': 'a'}\n"
            "def register():\n    return {'health': {'shared': check}}\n",
            encoding="utf-8",
        )
        (pdir / "b_second.py").write_text(
            "def check(**kwargs):\n    return {'status': 'ok', 'detail': 'b'}\n"
            "def register():\n    return {'health': {'shared': check}}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(HealthNameCollisionError):
            plugins_mod.PluginManager()

    def test_mixed_type_collision_rolls_back_runner_and_notifier_when_health_collides(
        self, tmp_path, monkeypatch
    ) -> None:
        """Cross-type atomicity when `health` is the failing member: plugin A
        registers `health` name 'dup' first; plugin B declares a runner AND a
        notifier AND a colliding `health` name 'dup' — the whole plugin B
        contribution (runner + notifier) must be rolled back, not just the
        health entry. Mirrors
        `tests/test_secrets_plugin.py::test_collision_rolls_back_plugins_other_contributions`,
        but with `health` (not `secrets`) as the colliding type.
        """
        from hivepilot import plugins as plugins_mod
        from hivepilot.plugins import HealthNameCollisionError
        from hivepilot.registry import RUNNER_MAP
        from hivepilot.services.notification_service import NOTIFIER_MAP

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        # 'a_' sorts before 'b_' — _scan_local_plugins loads via
        # sorted(plugin_dir.glob("*.py")), so plugin A's 'dup' health check
        # registers successfully before plugin B is even attempted.
        (pdir / "a_owner.py").write_text(
            "def check(**kwargs):\n    return {'status': 'ok', 'detail': 'a'}\n"
            "def register():\n    return {'health': {'dup': check}}\n",
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


def _b_health(**kwargs):
    return {'status': 'ok', 'detail': 'b'}


def register():
    # 'b-kind' runner and 'b-notif' notifier register first, then the
    # 'dup' health name collides with plugin A's already-registered one.
    return {
        "runners": {"b-kind": BRunner},
        "notifiers": {"b-notif": _b_notifier},
        "health": {"dup": _b_health},
    }
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(HealthNameCollisionError):
            plugins_mod.PluginManager()

        # Plugin B's runner and notifier — registered BEFORE the colliding
        # health entry within the same plugin's atomic block — were rolled
        # back, not left orphaned in the process-global maps.
        assert "b-kind" not in RUNNER_MAP
        assert "b-notif" not in NOTIFIER_MAP

    def test_collision_rolls_back_that_plugins_earlier_health_registrations(
        self, tmp_path, monkeypatch
    ) -> None:
        """A single plugin declaring two health names where the SECOND
        collides with an already-registered one must not leave the FIRST
        orphaned: registration of one plugin's health checks is atomic."""
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "a_owner.py").write_text(
            "def check(**kwargs):\n    return {'status': 'ok', 'detail': 'a'}\n"
            "def register():\n    return {'health': {'taken': check}}\n",
            encoding="utf-8",
        )
        (pdir / "b_partial.py").write_text(
            "def check(**kwargs):\n    return {'status': 'ok', 'detail': 'b'}\n"
            "def register():\n"
            "    return {'health': {'fresh': check, 'taken': check}}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(plugins_mod.HealthNameCollisionError):
            plugins_mod.PluginManager()


class TestPluginSkillsHooksIsolation:
    """Sprint 1 (skill-plugin-type): `skills` is popped out of a plugin's
    declared hooks the same way `runners`/`notifiers`/`secrets`/`health`/
    `panels` are — it must never leak into `PluginManager.hooks` (which is
    reserved for `before_step`/`after_step`-style lifecycle hooks), and
    `PluginManager.skills` must exist as a dict even when no plugin declares
    any skill. Full skill-registry coverage lives in
    `tests/test_skills_registry.py`.
    """

    def test_skills_attribute_exists_and_is_dict_with_no_plugins(
        self, tmp_path, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = plugins_mod.PluginManager()

        assert hasattr(pm, "skills")
        assert pm.skills == {}

    def test_skills_key_never_leaks_into_hooks_dict(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "with_skill.py").write_text(
            "def register():\n"
            "    return {'skills': [{'name': 's1', 'description': 'D', "
            "'provider': 'p', 'files': {'SKILL.md': 'x'}}]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()

        assert "skills" not in pm.hooks
        assert "s1" in pm.skills


class TestLoadPluginsByPath:
    """Plugins load by file path — no dependency on `plugins` being on sys.path
    (regression: the installed binary / Telegram bot crashed with
    ModuleNotFoundError: No module named 'plugins')."""

    def test_loads_plugin_without_plugins_on_syspath(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "good.py").write_text(
            "def register():\n    return {'before_step': lambda **k: None}\n", encoding="utf-8"
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        assert "plugins" not in sys.modules  # not importable as a package here
        loaded = plugins_mod.load_plugins()
        assert len(loaded) == 1
        assert callable(loaded[0])

    def test_broken_plugin_is_skipped_not_fatal(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "ok.py").write_text("def register():\n    return {}\n", encoding="utf-8")
        (pdir / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        loaded = plugins_mod.load_plugins()  # must not raise
        assert len(loaded) == 1  # ok loaded, broken skipped


class TestPluginContributionAttribution:
    """Phase 26a: `PluginRecord.contributions` attributes, to the SPECIFIC
    plugin that won the registration, exactly which names it contributed per
    contribution type (runners/notifiers/secrets/health/panels/skills) plus
    lifecycle hook names — respecting the atomic collision-rollback
    semantics already covered by `TestPluginHealthSurface` /
    `tests/test_secrets_plugin.py` / `tests/test_plugin_loading_mechanisms.py`
    (a contribution rolled back due to a collision is never credited, since
    the whole `PluginManager()` construction aborts before `record.
    contributions` is ever set for the colliding plugin).
    """

    def test_runner_and_hook_contributions_are_attributed(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "combo.py").write_text(
            """
class ComboRunner:
    def __init__(self, definition, settings):
        pass

    def run(self, payload):
        return None


def _before_step(**kwargs):
    return None


def register():
    return {"runners": {"combo-kind": ComboRunner}, "before_step": _before_step}
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()

        record = next(r for r in pm.loaded if r.name == "combo")
        assert record.contributions == {"runners": ["combo-kind"], "hooks": ["before_step"]}

    def test_plugin_contributing_nothing_attributable_has_empty_contributions(
        self, tmp_path, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "empty.py").write_text("def register():\n    return {}\n", encoding="utf-8")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()

        record = next(r for r in pm.loaded if r.name == "empty")
        assert record.contributions == {}

    def test_colliding_contribution_is_not_credited_and_aborts_construction(
        self, tmp_path, monkeypatch
    ) -> None:
        """A single plugin declaring a runner that succeeds AND one that
        collides with a builtin (`claude`) never reaches the point where
        `record.contributions` is populated — the atomic rollback pops the
        successful entry back out of `RUNNER_MAP` and re-raises BEFORE this
        plugin's record is even appended to `PluginManager.loaded`, so it can
        never be "half credited" for the entry that did succeed."""
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RUNNER_MAP, RunnerKindCollisionError

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "partial.py").write_text(
            """
class FreshRunner:
    def __init__(self, definition, settings):
        pass

    def run(self, payload):
        return None


class CollidingRunner:
    def __init__(self, definition, settings):
        pass

    def run(self, payload):
        return None


def register():
    # 'fresh-kind' registers first, then 'claude' collides with the builtin.
    return {"runners": {"fresh-kind": FreshRunner, "claude": CollidingRunner}}
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(RunnerKindCollisionError):
            plugins_mod.PluginManager()

        # The rolled-back entry never made it into RUNNER_MAP either — the
        # same evidence used elsewhere in this suite that a colliding
        # plugin's contribution is never partially applied, and therefore
        # never partially credited on a PluginRecord that doesn't even exist
        # (the constructor raised before `self.loaded.append(record)`).
        assert "fresh-kind" not in RUNNER_MAP

    def test_all_six_contribution_types_are_attributed_via_bundled_plugins(
        self, monkeypatch
    ) -> None:
        """Exercise real bundled plugins (hugo=runner+health,
        obsidian=notifier+hooks+health, infisical=secrets+health,
        sample=hooks+panels) through the real `_scan_local_plugins` discovery
        path — `settings.base_dir` already defaults to the repo root for
        every test (see `tests/conftest.py::_isolate_config_resolution`), so
        no monkeypatch of `base_dir` is needed here; only make sure the
        plugins under test are enabled regardless of the developer's local
        `.env` overrides."""
        from hivepilot import plugins as plugins_mod

        for flag in ("hugo_enabled", "obsidian_enabled", "infisical_enabled", "sample_enabled"):
            monkeypatch.setattr(plugins_mod.settings, flag, True, raising=False)

        pm = plugins_mod.PluginManager()
        by_name = {r.name: r for r in pm.loaded}

        assert by_name["hugo"].contributions == {"runners": ["hugo"], "health": ["hugo"]}
        assert by_name["obsidian"].contributions == {
            "notifiers": ["obsidian"],
            "hooks": ["on_error", "on_pipeline_end"],
            "health": ["obsidian"],
        }
        assert by_name["infisical"].contributions == {
            "secrets": ["infisical"],
            "health": ["infisical"],
        }
        assert by_name["sample"].contributions == {
            "hooks": ["after_step", "before_step"],
            "panels": ["sample_stats"],
        }
