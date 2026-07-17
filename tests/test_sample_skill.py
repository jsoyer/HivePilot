"""Tests for `plugins/sample_skill.py` (skill-plugin-type PRD, Sprint 5).

NOTE ON SCOPE: this file is required by the repo's TDD pre-write hook
(`check-test-exists.sh`), which expects an exact stem-matched test file
(`tests/test_sample_skill.py`) to exist before `plugins/sample_skill.py` may
be written/edited. The sprint 5 spec's declared `files_to_create` only lists
`tests/test_cli_skills_list.py`; this file was created in addition, purely to
satisfy that hook -- it contains ONLY tests for the sample_skill.py plugin
itself (register() shape + real PluginManager discovery + plugins_disabled
gating). CLI-command tests for `hivepilot skills list` remain in
tests/test_cli_skills_list.py as declared. See Sprint 5 Agent Notes.

Covers:
- `register()` returns the documented minimal `SkillSpec` shape
  (name/description/provider/files, non-empty `SKILL.md` content).
- The plugin module never defines a local `@dataclass` (CPython 3.14
  importlib+dataclasses bug documented in plugins/rtk.py).
- A REAL `PluginManager()` scan of an isolated tmp_path containing a copy of
  the actual `plugins/sample_skill.py` file registers `sample-skill` -- and,
  when `sample_skill` is listed in `settings.plugins_disabled`, contributes
  no skill at all (central plugins_enabled/plugins_disabled gating -- this
  plugin declares no per-plugin settings flag of its own).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SAMPLE_SKILL_PLUGIN = _REPO_ROOT / "plugins" / "sample_skill.py"


# ---------------------------------------------------------------------------
# plugins/sample_skill.py — direct unit test of register()'s return shape
# ---------------------------------------------------------------------------


class TestSampleSkillPluginRegisterShape:
    def _load_module(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "hivepilot_plugin_sample_skill_direct", _SAMPLE_SKILL_PLUGIN
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_register_returns_minimal_skillspec_shape(self) -> None:
        module = self._load_module()
        hooks = module.register()

        assert "skills" in hooks
        skills = hooks["skills"]
        assert len(skills) == 1
        skill = skills[0]

        assert skill["name"] == "sample-skill"
        assert isinstance(skill["description"], str) and skill["description"]
        assert skill["provider"] == "sample_skill"
        assert "SKILL.md" in skill["files"]
        assert isinstance(skill["files"]["SKILL.md"], str) and skill["files"]["SKILL.md"]

    def test_no_local_dataclass_defined_in_plugin_module(self) -> None:
        """The SkillSpec must be built as a plain dict literal, never a local
        `@dataclass` -- that would trip the CPython 3.14 importlib+dataclasses
        bug documented in plugins/rtk.py (local-file plugins are exec'd via
        `spec_from_file_location`, never registered in `sys.modules`). Checks
        for an actual `dataclasses` import/decorator usage, not just the
        substring "@dataclass" (which also appears in this module's own
        explanatory docstring prose)."""
        source = _SAMPLE_SKILL_PLUGIN.read_text(encoding="utf-8")
        assert "import dataclasses" not in source
        assert "from dataclasses import" not in source
        assert "\n@dataclass" not in source


# ---------------------------------------------------------------------------
# Real (isolated) PluginManager scan of a copy of the actual sample_skill.py
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_plugin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copy the REAL plugins/sample_skill.py into an isolated tmp_path/plugins
    dir and point hivepilot.plugins.settings.base_dir at it -- genuine
    on-disk discovery of the actual production file, isolated from every
    other plugin in the real plugins/ directory (mirrors
    tests/test_cli_config_commands.py::test_runner_plugin_registered_kind_accepted).
    """
    from hivepilot import plugins as plugins_mod

    pdir = tmp_path / "plugins"
    pdir.mkdir()
    shutil.copy2(_SAMPLE_SKILL_PLUGIN, pdir / "sample_skill.py")
    monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
    return tmp_path


class TestSampleSkillPluginRealDiscovery:
    def test_enabled_by_default_registers_sample_skill(self, isolated_plugin_dir: Path) -> None:
        from hivepilot.plugins import PluginManager

        pm = PluginManager()
        skill = pm.get_skill("sample-skill")

        assert skill is not None
        assert skill["provider"] == "sample_skill"
        assert "SKILL.md" in skill["files"]
        assert "sample-skill" in [s["name"] for s in pm.list_skills()]

    def test_disabled_via_plugins_disabled_contributes_no_skill(
        self, isolated_plugin_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.plugins import PluginManager

        monkeypatch.setattr(plugins_mod.settings, "plugins_disabled", ["sample_skill"])

        pm = PluginManager()

        assert pm.get_skill("sample-skill") is None
        assert pm.list_skills() == []
