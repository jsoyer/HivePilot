"""Tests for `hivepilot.plugins.PluginManager.reload()` — Phase 26b hot-reload.

Covers:
- reload picks up a NEW plugin file (added)
- reload re-registers a CHANGED plugin (updated)
- reload drops a REMOVED plugin's kind (removed)
- ATOMICITY (security-critical): a colliding candidate set, or one whose
  explicit-entry pin raises on import, leaves LIVE state COMPLETELY
  untouched — the previously-working plugin still resolves
- ownership tracking: reload never clobbers a builtin runner kind nor a kind
  this manager never staged (simulating another manager's registration)
- `plugins_changed_on_disk()` mtime-based change detection
- regression: plain `PluginManager()` construction still populates
  `PluginRecord.contributions` (Phase 26a attribution) the same way
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hivepilot import plugins as plugins_mod
from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.plugins import PluginManager, ReloadResult
from hivepilot.registry import RUNNER_MAP, RunnerRegistry, resolve_runner_class
from hivepilot.runners.base import BaseRunner, RunnerPayload


def _write_runner_plugin(
    plugin_dir: Path,
    filename: str,
    kind: str,
    class_name: str = "FixtureRunner",
    marker: str = "v1",
) -> None:
    """Write a minimal local-file plugin contributing a single runner kind.

    `marker` is a class-level attribute so a test can prove a reload actually
    re-executed the file (a NEW class object with the NEW marker value) --
    local-file plugins are always re-exec'd via `spec_from_file_location`,
    never cached in `sys.modules` (see `hivepilot.plugins._scan_local_plugins`).
    """
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / filename).write_text(
        f"""
class {class_name}:
    MARKER = {marker!r}

    def __init__(self, definition, settings):
        self.definition = definition
        self.settings = settings

    def run(self, payload):
        return None


def register():
    return {{"runners": {{"{kind}": {class_name}}}}}
""",
        encoding="utf-8",
    )


class TestReloadPicksUpChanges:
    def test_reload_adds_new_plugin(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        assert "fixture-a" not in RUNNER_MAP

        _write_runner_plugin(pdir, "a.py", kind="fixture-a")
        result = pm.reload()

        assert result.ok is True
        assert result.added == ["a"]
        assert result.removed == []
        assert "fixture-a" in RUNNER_MAP
        assert any(r.name == "a" for r in pm.loaded)

    def test_reload_reregisters_changed_plugin(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "a.py", kind="fixture-a", marker="v1")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        first_cls = RUNNER_MAP["fixture-a"]
        assert first_cls.MARKER == "v1"  # type: ignore[attr-defined]

        _write_runner_plugin(pdir, "a.py", kind="fixture-a", marker="v2")
        result = pm.reload()

        assert result.ok is True
        assert result.updated == ["a"]
        second_cls = RUNNER_MAP["fixture-a"]
        assert second_cls is not first_cls
        assert second_cls.MARKER == "v2"  # type: ignore[attr-defined]

    def test_reload_removes_deleted_plugin(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "a.py", kind="fixture-a")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        assert "fixture-a" in RUNNER_MAP

        (pdir / "a.py").unlink()
        result = pm.reload()

        assert result.ok is True
        assert result.removed == ["a"]
        assert "fixture-a" not in RUNNER_MAP
        assert not any(r.name == "a" for r in pm.loaded)


class TestReloadAtomicity:
    """Security-critical: a broken/colliding candidate set must never
    replace a working one."""

    def test_colliding_reload_leaves_live_state_untouched(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "keep.py", kind="fixture-keep")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        keep_cls_before = RUNNER_MAP["fixture-keep"]
        assert resolve_runner_class("fixture-keep") is keep_cls_before

        # New candidate set: "keep" unchanged, PLUS a new plugin that
        # collides with a BUILTIN runner kind ("shell").
        (pdir / "bad.py").write_text(
            "class BadRunner:\n"
            "    def __init__(self, definition, settings):\n        pass\n"
            "    def run(self, payload):\n        return None\n"
            "def register():\n"
            "    return {'runners': {'shell': BadRunner}}\n",
            encoding="utf-8",
        )

        result = pm.reload()

        assert result.ok is False
        assert result.error is not None
        assert result.added == []
        assert result.removed == []
        assert result.updated == []

        # Live state completely untouched: same class object as before the
        # failed reload attempt, and the builtin "shell" kind is intact too.
        from hivepilot.runners.shell_runner import ShellRunner

        assert RUNNER_MAP["fixture-keep"] is keep_cls_before
        assert resolve_runner_class("fixture-keep") is keep_cls_before
        assert resolve_runner_class("shell") is ShellRunner
        assert [r.name for r in pm.loaded] == ["keep"]

    def test_reload_raising_on_explicit_entry_import_leaves_live_state_untouched(
        self, tmp_path, monkeypatch
    ) -> None:
        """The `settings.plugins_entry` pin is the one load path that does
        NOT fail-isolate an import error (see `hivepilot.plugins.load_plugins`
        -- `import_module()` is unwrapped there, unlike the local-file scan
        and entry-point discovery paths, which both catch and skip). A
        misconfigured pin during reload must still leave live state intact.
        """
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "keep.py", kind="fixture-keep2")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        keep_cls_before = RUNNER_MAP["fixture-keep2"]

        monkeypatch.setattr(
            plugins_mod.settings,
            "plugins_entry",
            "hivepilot_does_not_exist_xyz:register",
            raising=False,
        )

        result = pm.reload()

        assert result.ok is False
        assert result.error is not None
        assert RUNNER_MAP["fixture-keep2"] is keep_cls_before
        assert resolve_runner_class("fixture-keep2") is keep_cls_before
        assert [r.name for r in pm.loaded] == ["keep"]


class TestReloadOwnershipTracking:
    def test_reload_never_clobbers_builtin_kind(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        from hivepilot.runners.shell_runner import ShellRunner

        assert RUNNER_MAP["shell"] is ShellRunner
        result = pm.reload()
        assert result.ok is True
        assert RUNNER_MAP["shell"] is ShellRunner

    def test_reload_never_clobbers_kind_this_manager_does_not_own(
        self, tmp_path, monkeypatch
    ) -> None:
        """A runner kind present in RUNNER_MAP that this manager never staged
        (simulating another manager's registration) must survive an
        unrelated reload untouched -- reload only removes kinds it itself
        previously added.
        """
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()

        class OtherManagerRunner(BaseRunner):
            def __init__(self, definition: RunnerDefinition, settings: Settings) -> None:
                self.definition = definition
                self.settings = settings

            def run(self, payload: RunnerPayload) -> None:
                return None

        RunnerRegistry.register("owned-elsewhere", OtherManagerRunner)

        result = pm.reload()

        assert result.ok is True
        assert RUNNER_MAP["owned-elsewhere"] is OtherManagerRunner


class TestPluginsChangedOnDisk:
    def test_false_when_no_change(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "a.py", kind="fixture-a")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        assert pm.plugins_changed_on_disk() is False

    def test_true_on_added_file(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        assert pm.plugins_changed_on_disk() is False

        _write_runner_plugin(pdir, "new.py", kind="fixture-new")
        assert pm.plugins_changed_on_disk() is True

    def test_true_on_modified_file(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "a.py", kind="fixture-a", marker="v1")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        assert pm.plugins_changed_on_disk() is False

        f = pdir / "a.py"
        f.write_text(f.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        bumped = f.stat().st_mtime + 10
        os.utime(f, (bumped, bumped))
        assert pm.plugins_changed_on_disk() is True

    def test_true_on_removed_file(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "a.py", kind="fixture-a")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        assert pm.plugins_changed_on_disk() is False

        (pdir / "a.py").unlink()
        assert pm.plugins_changed_on_disk() is True


class TestRegressionAfterRefactor:
    """The `_load_into`/`_commit` refactor must not change plain
    `PluginManager()` construction behavior (Phase 26a attribution etc.)."""

    def test_plain_construction_populates_contributions(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "a.py", kind="fixture-regress")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        record = next(r for r in pm.loaded if r.name == "a")
        assert record.contributions == {"runners": ["fixture-regress"]}

    def test_reload_result_is_importable_dataclass(self) -> None:
        result = ReloadResult(ok=True)
        assert result.added == []
        assert result.removed == []
        assert result.updated == []
        assert result.error is None


@pytest.mark.parametrize("bad_kind", ["shell", "claude"])
def test_reload_collision_with_multiple_builtins_still_untouched(
    tmp_path, monkeypatch, bad_kind
) -> None:
    monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
    pm = PluginManager()
    before = dict(RUNNER_MAP)

    pdir = tmp_path / "plugins"
    (pdir).mkdir(parents=True, exist_ok=True)
    (pdir / "bad.py").write_text(
        "class BadRunner:\n"
        "    def __init__(self, definition, settings):\n        pass\n"
        "    def run(self, payload):\n        return None\n"
        f"def register():\n    return {{'runners': {{'{bad_kind}': BadRunner}}}}\n",
        encoding="utf-8",
    )

    result = pm.reload()

    assert result.ok is False
    assert dict(RUNNER_MAP) == before
