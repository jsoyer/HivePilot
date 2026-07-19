"""Tests for `plugins/shadcn.py` (opt-in skill plugin — Mirador web
accelerator).

Mirrors `tests/test_sample_skill.py`'s structure exactly:
- `register()` returns `{}` when `settings.shadcn_enabled` is False
  (default), and the documented `SkillSpec` shape when True.
- The plugin module never defines a local `@dataclass` (CPython 3.14
  importlib+dataclasses bug documented in plugins/rtk.py) -- `SkillSpec`
  must stay a plain dict literal.
- A REAL `PluginManager()` scan of an isolated tmp_path containing a copy of
  the actual `plugins/shadcn.py` file registers `shadcn` when the flag is
  enabled, and contributes nothing when disabled via `plugins_disabled`.
- `TestAllPluginStemsHaveEnabledFlag` conformance (settings has
  `shadcn_enabled`) is covered centrally in tests/test_gating_conformance.py;
  this file only asserts the flag's own default + per-plugin shape.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHADCN_PLUGIN = _REPO_ROOT / "plugins" / "shadcn.py"


# ---------------------------------------------------------------------------
# plugins/shadcn.py — direct unit test of register()'s return shape
# ---------------------------------------------------------------------------


class TestShadcnPluginRegisterShape:
    def _load_module(self):
        spec = importlib.util.spec_from_file_location(
            "hivepilot_plugin_shadcn_direct", _SHADCN_PLUGIN
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_register_returns_empty_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hivepilot.config import settings as _settings

        monkeypatch.setattr(_settings, "shadcn_enabled", False, raising=False)
        module = self._load_module()
        assert module.register() == {}

    def test_register_returns_minimal_skillspec_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.config import settings as _settings

        monkeypatch.setattr(_settings, "shadcn_enabled", True, raising=False)
        module = self._load_module()
        hooks = module.register()

        assert "skills" in hooks
        skills = hooks["skills"]
        assert len(skills) == 1
        skill = skills[0]

        assert skill["name"] == "shadcn"
        assert isinstance(skill["description"], str) and skill["description"]
        assert skill["provider"] == "shadcn"
        assert "SKILL.md" in skill["files"]
        assert isinstance(skill["files"]["SKILL.md"], str)
        assert len(skill["files"]["SKILL.md"]) > 0

        # SKILL.md content should be grounded in the real webui stack, not
        # invented components -- spot-check for the actual conventions
        # (shadcn/ui, Tailwind, the real web/ dir layout).
        content = skill["files"]["SKILL.md"]
        assert "shadcn" in content.lower()
        assert "tailwind" in content.lower()
        assert "web/src/components" in content

    def test_optional_system_prompt_and_min_role_if_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.config import settings as _settings
        from hivepilot.services import token_service

        monkeypatch.setattr(_settings, "shadcn_enabled", True, raising=False)
        module = self._load_module()
        skill = module.register()["skills"][0]

        if "system_prompt" in skill:
            assert isinstance(skill["system_prompt"], str) and skill["system_prompt"]
        if "min_role" in skill:
            assert skill["min_role"] in token_service.ROLE_RANKS

    def test_no_local_dataclass_defined_in_plugin_module(self) -> None:
        source = _SHADCN_PLUGIN.read_text(encoding="utf-8")
        assert "import dataclasses" not in source
        assert "from dataclasses import" not in source
        assert "\n@dataclass" not in source


# ---------------------------------------------------------------------------
# Real (isolated) PluginManager scan of a copy of the actual shadcn.py
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_plugin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from hivepilot import plugins as plugins_mod

    pdir = tmp_path / "plugins"
    pdir.mkdir()
    shutil.copy2(_SHADCN_PLUGIN, pdir / "shadcn.py")
    monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
    return tmp_path


class TestShadcnPluginRealDiscovery:
    def test_registers_shadcn_when_flag_enabled(
        self, isolated_plugin_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.plugins import PluginManager

        monkeypatch.setattr(plugins_mod.settings, "shadcn_enabled", True, raising=False)

        pm = PluginManager()
        skill = pm.get_skill("shadcn")

        assert skill is not None
        assert skill["provider"] == "shadcn"
        assert "SKILL.md" in skill["files"]
        assert "shadcn" in [s["name"] for s in pm.list_skills()]

    def test_disabled_by_default_contributes_no_skill(
        self, isolated_plugin_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.plugins import PluginManager

        monkeypatch.setattr(plugins_mod.settings, "shadcn_enabled", False, raising=False)

        pm = PluginManager()

        assert pm.get_skill("shadcn") is None
        assert pm.list_skills() == []

    def test_disabled_via_plugins_disabled_contributes_no_skill(
        self, isolated_plugin_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.plugins import PluginManager

        monkeypatch.setattr(plugins_mod.settings, "shadcn_enabled", True, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_disabled", ["shadcn"])

        pm = PluginManager()

        assert pm.get_skill("shadcn") is None
        assert pm.list_skills() == []
