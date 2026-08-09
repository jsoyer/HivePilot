"""Tests for `plugins/shadcn.py` (opt-in skill plugin — Pollen web
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
import re
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


class TestTheCheatSheetMatchesTheActualTree:
    """A stale cheat sheet is worse than none: it gives confidence in the
    wrong direction.

    Audited 2026-08-09 against `web/`. 36 of 37 factual claims held; the
    misses were all in the "what to reuse" layer, which is exactly what the
    skill is for and exactly what moves fastest:

    - it named `RunsView`, superseded by `RunBoardView`;
    - it told an agent to hand-roll the loading/error scaffold inline, while
      13 of 17 views already compose `<AsyncSection>`;
    - it listed 6 of the 9 `ui/` primitives;
    - it never mentioned i18n, though 16 of 17 views use `useT()`.

    These tests read the REAL tree, so the next drift fails here instead of
    reaching an agent.
    """

    WEB = _REPO_ROOT / "web"

    def _skill_md(self, monkeypatch: pytest.MonkeyPatch) -> str:
        from hivepilot.config import settings as _settings

        monkeypatch.setattr(_settings, "shadcn_enabled", True, raising=False)
        spec = importlib.util.spec_from_file_location(
            "hivepilot_plugin_shadcn_audit", _SHADCN_PLUGIN
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        skills = module.register()["skills"]
        return str(skills[0]["files"]["SKILL.md"])

    def test_every_file_it_names_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The `RunsView` case: a named file that is gone sends an agent
        looking for it, or modelling new work on a superseded pattern."""
        md = self._skill_md(monkeypatch)
        named = set(re.findall(r"`([A-Za-z][A-Za-z0-9_]*\.tsx)`", md))
        missing = sorted(
            n
            for n in named
            if not list(self.WEB.rglob(n))  # anywhere under web/
        )

        assert not missing, f"the skill names files that no longer exist: {missing}"

    def test_it_lists_every_ui_primitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unlisted primitive is one an agent re-implements by hand."""
        md = self._skill_md(monkeypatch)
        real = {
            p.stem for p in (self.WEB / "src/components/ui").glob("*.tsx") if ".test" not in p.name
        }
        missing = sorted(n for n in real if f"{n}.tsx" not in md)

        assert not missing, f"ui/ primitives absent from the cheat sheet: {missing}"

    def test_it_points_at_the_scaffold_that_views_actually_use(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`AsyncSection` is the loading/error/empty scaffold. Telling an
        agent to build one inline is telling it to duplicate a component."""
        assert "AsyncSection" in self._skill_md(monkeypatch)

    def test_it_mentions_i18n(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """16 of 17 views use `useT()`, and a visible string must exist in
        BOTH `en.ts` and `fr.ts`. A skill that omits this produces hardcoded
        text that builds and lints cleanly."""
        md = self._skill_md(monkeypatch)

        assert "useT" in md
        assert "fr.ts" in md
