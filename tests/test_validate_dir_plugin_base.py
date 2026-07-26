"""Tests for the `hivepilot validate --dir` cwd-bug fix.

Bug: `validate --dir <config-repo>` loaded the CONFIG FILES from `--dir` but
resolved plugins/skills via `PluginManager()`, which reads the global
`Settings.base_dir` (`default_factory=Path.cwd`) -- so a `skills:` reference
in a config repo validated from any cwd OTHER than that repo silently
reported "unknown skill", while the identical command run with cwd inside
the repo reported OK. This module pins:

  1. `config_validation.validate_config_report()` threads its `base_dir`
     down to `PluginManager(base_dir=...)` instead of leaving it to default
     to `Path.cwd()` (mirrors `tests/test_plugin_loading_mechanisms.py::
     TestPluginManagerExplicitBaseDir` at the `PluginManager` level).
  2. The report exposes the resolved config dir + plugin dirs scanned (the
     `hivepilot validate` header) and is cwd-independent end to end.
  3. `validate_config()` (the pre-existing `list[str]` contract dozens of
     other tests rely on) is unchanged -- it's now a thin wrapper.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from hivepilot.services import config_validation

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


def _write_config(base_dir: Path, *, tasks: dict | None = None) -> None:
    (base_dir / "projects.yaml").write_text(yaml.dump({"projects": {}}))
    (base_dir / "roles.yaml").write_text(
        yaml.dump({"roles": [{"name": "planner", "prompt_file": "planner.md"}]})
    )
    (base_dir / "policies.yaml").write_text(yaml.dump({"policies": {}}))
    (base_dir / "groups.yaml").write_text(yaml.dump({"groups": {}}))
    (base_dir / "tasks.yaml").write_text(yaml.dump({"tasks": tasks or {}}))
    (base_dir / "pipelines.yaml").write_text(yaml.dump({"pipelines": {}}))
    (base_dir / "prompts" / "agents").mkdir(parents=True)
    (base_dir / "prompts" / "agents" / "planner.md").write_text("# planner")


def _write_skill_plugin(config_dir: Path, *, skill_name: str) -> None:
    plugin_dir = config_dir / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    skill = {
        "name": skill_name,
        "description": "d",
        "provider": "acme",
        "files": {"SKILL.md": "hi"},
    }
    (plugin_dir / f"{skill_name}.py").write_text(
        f"def register():\n    return {{'skills': [{skill!r}]}}\n",
        encoding="utf-8",
    )


class TestValidationReportShape:
    def test_report_exposes_config_dir_and_plugin_dirs(self, tmp_path: Path) -> None:
        _write_config(tmp_path)

        report = config_validation.validate_config_report(base_dir=tmp_path)

        assert report.problems == []
        assert report.config_dir == tmp_path.resolve()
        assert (tmp_path / "plugins").resolve() in report.plugin_dirs

    def test_validate_config_is_a_thin_wrapper_returning_problems_only(
        self, tmp_path: Path
    ) -> None:
        """The pre-existing `list[str]` contract dozens of other tests rely
        on (`validate_config(base_dir=...) == []`) must be unchanged."""
        _write_config(tmp_path)

        assert config_validation.validate_config(base_dir=tmp_path) == []
        report = config_validation.validate_config_report(base_dir=tmp_path)
        assert config_validation.validate_config(base_dir=tmp_path) == report.problems

    def test_no_dir_report_still_exposes_an_absolute_config_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Omitting base_dir must still produce an absolute `config_dir` in
        the report -- unchanged-otherwise behavior for the no-`--dir` path."""
        _write_config(tmp_path)
        monkeypatch.chdir(tmp_path)
        # Isolate from any real ~/.config/hivepilot on the machine running
        # this test -- resolve_config_path checks XDG first, so a real
        # config there would otherwise shadow the tmp_path config below.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-xdg"))
        monkeypatch.setattr(config_validation.settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(config_validation.settings, "config_repo", None, raising=False)

        report = config_validation.validate_config_report()

        assert report.problems == []
        assert report.config_dir.is_absolute()


class TestPluginManagerReceivesExplicitBaseDir:
    def test_plugin_manager_constructed_with_explicit_base_dir_not_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "config-repo"
        config_dir.mkdir()
        _write_skill_plugin(config_dir, skill_name="tracked_skill")
        _write_config(
            config_dir,
            tasks={
                "task-a": {
                    "role": "planner",
                    "steps": [{"name": "s1", "runner": "claude", "skills": ["tracked_skill"]}],
                }
            },
        )

        from hivepilot import plugins as plugins_mod

        captured: dict[str, object] = {}
        original_init = plugins_mod.PluginManager.__init__

        def _spy_init(self, *args, **kwargs):
            captured["base_dir"] = kwargs.get("base_dir")
            return original_init(self, *args, **kwargs)

        monkeypatch.setattr(plugins_mod.PluginManager, "__init__", _spy_init)
        # settings.base_dir points somewhere else entirely -- proves the
        # explicit kwarg, not the global default, is what gets used.
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path / "elsewhere", raising=False)

        problems = config_validation.validate_config(base_dir=config_dir)

        assert problems == [], f"Unexpected problems: {problems}"
        assert captured["base_dir"] == config_dir
        assert captured["base_dir"] != Path.cwd()


class TestCwdIndependence:
    def test_validate_config_report_is_cwd_independent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "config-repo"
        config_dir.mkdir()
        _write_skill_plugin(config_dir, skill_name="cwd_skill")
        _write_config(
            config_dir,
            tasks={
                "task-a": {
                    "role": "planner",
                    "steps": [{"name": "s1", "runner": "claude", "skills": ["cwd_skill"]}],
                }
            },
        )
        outside = tmp_path / "outside"
        outside.mkdir()

        monkeypatch.chdir(outside)
        report_outside = config_validation.validate_config_report(base_dir=config_dir)

        monkeypatch.chdir(config_dir)
        report_inside = config_validation.validate_config_report(base_dir=config_dir)

        assert report_outside.problems == []
        assert report_inside.problems == []
        assert report_outside.problems == report_inside.problems
        assert report_outside.config_dir == report_inside.config_dir
        assert report_outside.plugin_dirs == report_inside.plugin_dirs

    def test_unresolved_skill_message_names_searched_directories(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config-repo"
        config_dir.mkdir()
        _write_config(
            config_dir,
            tasks={
                "task-a": {
                    "role": "planner",
                    "steps": [{"name": "s1", "runner": "claude", "skills": ["ghost-skill"]}],
                }
            },
        )

        report = config_validation.validate_config_report(base_dir=config_dir)

        matching = [p for p in report.problems if "ghost-skill" in p]
        assert matching, f"Expected an unknown-skill problem, got: {report.problems}"
        assert str((config_dir / "plugins").resolve()) in matching[0]
