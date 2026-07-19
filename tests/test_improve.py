"""Tests for `plugins/improve.py` (opt-in skill plugin — read-only auditor
feeding the review/lessons loop).

Mirrors `tests/test_sample_skill.py`'s structure exactly:
- `register()` returns `{}` when `settings.improve_enabled` is False
  (default), and the documented `SkillSpec` shape when True.
- The plugin module never defines a local `@dataclass` (CPython 3.14
  importlib+dataclasses bug documented in plugins/rtk.py) -- `SkillSpec`
  must stay a plain dict literal.
- A REAL `PluginManager()` scan of an isolated tmp_path containing a copy of
  the actual `plugins/improve.py` file registers `improve` when the flag is
  enabled, and contributes nothing when disabled.
- The SKILL.md emphasizes read-only behavior (no writes/edits/commits) and a
  structured findings output format (severity, file:line, why, fix).
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_IMPROVE_PLUGIN = _REPO_ROOT / "plugins" / "improve.py"


# ---------------------------------------------------------------------------
# plugins/improve.py — direct unit test of register()'s return shape
# ---------------------------------------------------------------------------


class TestImprovePluginRegisterShape:
    def _load_module(self):
        spec = importlib.util.spec_from_file_location(
            "hivepilot_plugin_improve_direct", _IMPROVE_PLUGIN
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_register_returns_empty_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hivepilot.config import settings as _settings

        monkeypatch.setattr(_settings, "improve_enabled", False, raising=False)
        module = self._load_module()
        assert module.register() == {}

    def test_register_returns_minimal_skillspec_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.config import settings as _settings

        monkeypatch.setattr(_settings, "improve_enabled", True, raising=False)
        module = self._load_module()
        hooks = module.register()

        assert "skills" in hooks
        skills = hooks["skills"]
        assert len(skills) == 1
        skill = skills[0]

        assert skill["name"] == "improve"
        assert isinstance(skill["description"], str) and skill["description"]
        assert skill["provider"] == "improve"
        assert "SKILL.md" in skill["files"]
        assert isinstance(skill["files"]["SKILL.md"], str)
        assert len(skill["files"]["SKILL.md"]) > 0

    def test_skill_md_emphasizes_read_only_and_findings_format(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.config import settings as _settings

        monkeypatch.setattr(_settings, "improve_enabled", True, raising=False)
        module = self._load_module()
        content = module.register()["skills"][0]["files"]["SKILL.md"]
        lowered = content.lower()

        # Read-only discipline must be explicit.
        assert "read-only" in lowered or "read only" in lowered
        assert "no writes" in lowered or "never write" in lowered or "never edit" in lowered
        assert "no commit" in lowered or "never commit" in lowered

        # Findings output format must be explicit (severity, location, why, fix).
        assert "severity" in lowered
        assert "file:line" in lowered or "file: line" in lowered
        assert "suggested fix" in lowered or "fix" in lowered

    def test_optional_system_prompt_enforces_read_only_and_min_role_if_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.config import settings as _settings
        from hivepilot.services import token_service

        monkeypatch.setattr(_settings, "improve_enabled", True, raising=False)
        module = self._load_module()
        skill = module.register()["skills"][0]

        if "system_prompt" in skill:
            assert isinstance(skill["system_prompt"], str) and skill["system_prompt"]
            assert "read-only" in skill["system_prompt"].lower() or (
                "read only" in skill["system_prompt"].lower()
            )
        if "min_role" in skill:
            assert skill["min_role"] in token_service.ROLE_RANKS

    def test_no_local_dataclass_defined_in_plugin_module(self) -> None:
        source = _IMPROVE_PLUGIN.read_text(encoding="utf-8")
        assert "import dataclasses" not in source
        assert "from dataclasses import" not in source
        assert "\n@dataclass" not in source


# ---------------------------------------------------------------------------
# Real (isolated) PluginManager scan of a copy of the actual improve.py
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_plugin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from hivepilot import plugins as plugins_mod

    pdir = tmp_path / "plugins"
    pdir.mkdir()
    shutil.copy2(_IMPROVE_PLUGIN, pdir / "improve.py")
    monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
    return tmp_path


class TestImprovePluginRealDiscovery:
    def test_registers_improve_when_flag_enabled(
        self, isolated_plugin_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.plugins import PluginManager

        monkeypatch.setattr(plugins_mod.settings, "improve_enabled", True, raising=False)

        pm = PluginManager()
        skill = pm.get_skill("improve")

        assert skill is not None
        assert skill["provider"] == "improve"
        assert "SKILL.md" in skill["files"]
        assert "improve" in [s["name"] for s in pm.list_skills()]

    def test_disabled_by_default_contributes_no_skill(
        self, isolated_plugin_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.plugins import PluginManager

        monkeypatch.setattr(plugins_mod.settings, "improve_enabled", False, raising=False)

        pm = PluginManager()

        assert pm.get_skill("improve") is None
        assert pm.list_skills() == []

    def test_disabled_via_plugins_disabled_contributes_no_skill(
        self, isolated_plugin_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.plugins import PluginManager

        monkeypatch.setattr(plugins_mod.settings, "improve_enabled", True, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_disabled", ["improve"])

        pm = PluginManager()

        assert pm.get_skill("improve") is None
        assert pm.list_skills() == []
