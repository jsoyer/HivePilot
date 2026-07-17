"""
Tests for hivepilot.plugins' two discovery mechanisms (local-file scan and
Python entry points), runner-kind wiring into RunnerRegistry, notifier
collection, and broken-plugin isolation.

Covers, per the Sprint 2 spec:
(a) local-file plugin runner registration
(b) entry-point plugin runner registration (real `ep.load()` against a real
    importable fixture module, via a monkeypatched `importlib.metadata.entry_points`)
(c) both mechanisms loaded together with no collision
(d) a kind collision (plugin vs. built-in) raises RunnerKindCollisionError
(e) broken-plugin isolation for both mechanisms (import/exec, `.load()`, and
    `register()` invocation failures)
(f) `settings.plugins_enabled = False` disables both mechanisms
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from hivepilot import plugins as plugins_mod
from hivepilot.models import RunnerDefinition
from hivepilot.plugins import PLUGIN_ENTRY_POINT_GROUP, PluginManager, PluginRecord
from hivepilot.registry import RUNNER_MAP, RunnerKindCollisionError, RunnerRegistry
from hivepilot.services.notification_service import NOTIFIER_MAP
from tests.fixtures import entry_point_plugin as fixture_module


@pytest.fixture(autouse=True)
def _restore_runner_map():
    """RUNNER_MAP and NOTIFIER_MAP are process-global mutable state —
    snapshot/restore around every test in this module so runner kinds and
    notifier names registered here (a plugin's declared notifiers are wired
    into NOTIFIER_MAP by PluginManager) never leak into other test modules
    sharing the pytest session."""
    runner_snapshot = dict(RUNNER_MAP)
    notifier_snapshot = dict(NOTIFIER_MAP)
    yield
    RUNNER_MAP.clear()
    RUNNER_MAP.update(runner_snapshot)
    NOTIFIER_MAP.clear()
    NOTIFIER_MAP.update(notifier_snapshot)


def _write_local_plugin(
    plugin_dir: Path, filename: str, kind: str, class_name: str = "LocalFixtureRunner"
) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / filename).write_text(
        f"""
class {class_name}:
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


@dataclass
class _FakeDist:
    name: str
    version: str


class _FakeEntryPoint:
    """Stand-in for importlib.metadata.EntryPoint — `.load()` resolves to a
    real callable (no actual package install needed for the test)."""

    def __init__(
        self,
        name: str,
        value: str,
        loader: Callable[[], Any],
        dist: _FakeDist | None = None,
    ) -> None:
        self.name = name
        self.value = value
        self.dist = dist
        self._loader = loader

    def load(self) -> Any:
        return self._loader()


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, eps: list[_FakeEntryPoint]) -> None:
    def _fake_entry_points(*, group: str | None = None) -> list[_FakeEntryPoint]:
        if group == PLUGIN_ENTRY_POINT_GROUP:
            return eps
        return []

    monkeypatch.setattr(plugins_mod.metadata, "entry_points", _fake_entry_points)


class TestLocalFilePluginRunnerRegistration:
    def test_local_file_plugin_runner_is_resolvable(self, tmp_path, monkeypatch) -> None:
        _write_local_plugin(tmp_path / "plugins", "local_fixture.py", kind="local-fixture")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = PluginManager()

        assert "local-fixture" in RUNNER_MAP
        assert any(r.source == "local-file" and r.name == "local_fixture" for r in pm.loaded)


class TestPluginRunnerResolvesAndExecutes:
    def test_plugin_runner_resolves_instantiates_and_runs(self, tmp_path, monkeypatch) -> None:
        # AC #4 (execution half): a plugin kind is not merely present in
        # RUNNER_MAP — it resolves through RunnerRegistry.get_runner to an
        # instance of the plugin's OWN class, and that instance's run() is
        # invocable, i.e. the plugin runner actually executes.
        _write_local_plugin(
            tmp_path / "plugins",
            "exec_fixture.py",
            kind="exec-fixture",
            class_name="ExecFixtureRunner",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        PluginManager()

        registry = RunnerRegistry(
            {"myrunner": RunnerDefinition(name="myrunner", kind="exec-fixture")}
        )
        runner = registry.get_runner("myrunner")
        assert type(runner).__name__ == "ExecFixtureRunner"
        runner.run(None)  # plugin runner executes without raising


class TestEntryPointPluginRunnerRegistration:
    def test_entry_point_plugin_runner_is_resolvable(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        ep = _FakeEntryPoint(
            name="fixture-ep",
            value="tests.fixtures.entry_point_plugin:register",
            loader=lambda: fixture_module.register,
        )
        _patch_entry_points(monkeypatch, [ep])

        pm = PluginManager()

        assert "fixture-kind" in RUNNER_MAP
        assert any(r.source == "entry-point" and r.name == "fixture-ep" for r in pm.loaded)


class TestBothMechanismsTogether:
    def test_both_mechanisms_load_without_collision(self, tmp_path, monkeypatch) -> None:
        _write_local_plugin(tmp_path / "plugins", "local_fixture.py", kind="local-fixture-2")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        ep = _FakeEntryPoint(
            name="fixture-ep",
            value="tests.fixtures.entry_point_plugin:register",
            loader=lambda: fixture_module.register,
        )
        _patch_entry_points(monkeypatch, [ep])

        pm = PluginManager()

        assert "local-fixture-2" in RUNNER_MAP
        assert "fixture-kind" in RUNNER_MAP
        assert len(pm.loaded) == 2
        assert all(isinstance(r, PluginRecord) for r in pm.loaded)
        assert {r.source for r in pm.loaded} == {"local-file", "entry-point"}


class TestKindCollision:
    def test_collision_with_builtin_raises(self, tmp_path, monkeypatch) -> None:
        _write_local_plugin(
            tmp_path / "plugins", "collide.py", kind="claude", class_name="CollidingRunner"
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(RunnerKindCollisionError):
            PluginManager()

    def test_collision_rolls_back_that_plugins_earlier_registrations(
        self, tmp_path, monkeypatch
    ) -> None:
        # A single plugin declaring two runners where the SECOND collides with a
        # built-in must not leave the FIRST orphaned in the process-global
        # RUNNER_MAP: registration of one plugin's runners is atomic.
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "partial.py").write_text(
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
    # 'fresh-kind' registers first, then 'claude' collides with the built-in.
    return {"runners": {"fresh-kind": FreshRunner, "claude": CollidingRunner}}
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(RunnerKindCollisionError):
            PluginManager()

        # The earlier, non-colliding kind was rolled back — no orphaned entry.
        assert "fresh-kind" not in RUNNER_MAP


class TestBrokenPluginIsolation:
    def test_broken_local_plugin_register_call_is_skipped(self, tmp_path, monkeypatch) -> None:
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "broken_register.py").write_text(
            "def register():\n    raise RuntimeError('register boom')\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = PluginManager()  # must not raise

        assert pm.loaded == []

    def test_broken_entry_point_load_is_skipped(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        def _boom() -> Any:
            raise RuntimeError("load boom")

        ep = _FakeEntryPoint(name="broken-ep", value="broken:register", loader=_boom)
        _patch_entry_points(monkeypatch, [ep])

        pm = PluginManager()  # must not raise

        assert pm.loaded == []

    def test_broken_entry_point_register_call_is_skipped(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        def _bad_register() -> dict[str, Any]:
            raise RuntimeError("register boom")

        ep = _FakeEntryPoint(
            name="broken-register-ep", value="broken:register", loader=lambda: _bad_register
        )
        _patch_entry_points(monkeypatch, [ep])

        pm = PluginManager()  # must not raise

        assert pm.loaded == []


class TestPluginsDisabled:
    def test_plugins_disabled_skips_both_mechanisms(self, tmp_path, monkeypatch) -> None:
        _write_local_plugin(tmp_path / "plugins", "local_fixture.py", kind="should-not-register")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        ep = _FakeEntryPoint(
            name="fixture-ep",
            value="tests.fixtures.entry_point_plugin:register",
            loader=lambda: fixture_module.register,
        )
        _patch_entry_points(monkeypatch, [ep])
        monkeypatch.setattr(plugins_mod.settings, "plugins_enabled", False, raising=False)

        assert plugins_mod._scan_local_plugins() == []
        assert plugins_mod.load_entry_point_plugins() == []

        pm = PluginManager()

        assert pm.loaded == []
        assert "should-not-register" not in RUNNER_MAP
        assert "fixture-kind" not in RUNNER_MAP


class TestPerPluginDisabled:
    """Sprint 5: `plugins_disabled` skips ONE named plugin (by derived
    name — local-file stem or entry-point name) in each discovery path
    while leaving other plugins unaffected, unlike the all-or-nothing
    `plugins_enabled` master switch covered by TestPluginsDisabled above."""

    def test_local_file_plugin_in_disabled_list_is_not_loaded(self, tmp_path, monkeypatch) -> None:
        _write_local_plugin(tmp_path / "plugins", "rtk.py", kind="rtk-kind")
        _write_local_plugin(tmp_path / "plugins", "other.py", kind="other-kind")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_disabled", ["rtk"], raising=False)

        pm = PluginManager()

        assert "rtk-kind" not in RUNNER_MAP
        assert "other-kind" in RUNNER_MAP
        assert not any(r.name == "rtk" for r in pm.loaded)
        assert any(r.name == "other" for r in pm.loaded)

    def test_explicit_plugins_entry_in_disabled_list_by_full_string_is_not_loaded(
        self, tmp_path, monkeypatch
    ) -> None:
        """A THIRD load path — `settings.plugins_entry` (a single pinned
        plugin loaded via `load_plugins(entry=...)` in `PluginManager.__init__`,
        distinct from the local-file scan / entry-point discovery covered
        above) must also honor `plugins_disabled`. `PluginRecord.name` for
        this path is the full `explicit_entry` string (what the TUI shows
        and would toggle) — disabling by that exact string must skip it."""
        entry = "tests.fixtures.entry_point_plugin:register"
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_entry", entry, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_disabled", [entry], raising=False)

        pm = PluginManager()

        assert "fixture-kind" not in RUNNER_MAP
        assert pm.loaded == []

    def test_explicit_plugins_entry_in_disabled_list_by_module_name_is_not_loaded(
        self, tmp_path, monkeypatch
    ) -> None:
        """An operator setting `plugins_disabled` directly via config/env
        would naturally use the short module-name portion (before the `:`
        attribute separator), matching the short names the other two paths'
        `plugins_disabled` entries use (e.g. "rtk", not a full module:attr
        string) — accept that form too."""
        entry = "tests.fixtures.entry_point_plugin:register"
        module_name = "tests.fixtures.entry_point_plugin"
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_entry", entry, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_disabled", [module_name], raising=False)

        pm = PluginManager()

        assert "fixture-kind" not in RUNNER_MAP
        assert pm.loaded == []

    def test_explicit_plugins_entry_not_in_disabled_list_still_loads(
        self, tmp_path, monkeypatch
    ) -> None:
        """Regression guard: the new gate must not accidentally skip an
        explicit `plugins_entry` plugin that is NOT disabled."""
        entry = "tests.fixtures.entry_point_plugin:register"
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_entry", entry, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_disabled", [], raising=False)

        pm = PluginManager()

        assert "fixture-kind" in RUNNER_MAP
        assert any(r.name == entry and r.source == "explicit-entry" for r in pm.loaded)

    def test_entry_point_plugin_in_disabled_list_is_not_loaded(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_disabled", ["fixture-ep"], raising=False)
        ep = _FakeEntryPoint(
            name="fixture-ep",
            value="tests.fixtures.entry_point_plugin:register",
            loader=lambda: fixture_module.register,
        )
        _patch_entry_points(monkeypatch, [ep])

        pm = PluginManager()

        assert "fixture-kind" not in RUNNER_MAP
        assert pm.loaded == []

    def test_disabled_local_plugin_module_is_never_executed(self, tmp_path, monkeypatch) -> None:
        """A disabled plugin is skipped before register() is invoked — assert
        the module body itself never runs (a side-effecting top-level
        statement would prove exec_module was never called)."""
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        marker = tmp_path / "executed.marker"
        (plugin_dir / "rtk.py").write_text(
            f"""
import pathlib
pathlib.Path({str(marker)!r}).write_text("yes")


def register():
    return {{}}
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_disabled", ["rtk"], raising=False)

        PluginManager()

        assert not marker.exists()


class TestExplicitEntrySourceValue:
    """Phase 26a: the explicit `settings.plugins_entry` pin is tagged
    `source="explicit-entry"` — a distinct 4th `PluginRecord.source` value,
    not the misleading `"local-file"` it used to share with the (unrelated)
    `plugins/*.py` directory scan. `TestPerPluginDisabled` above already
    covers the `plugins_disabled` gate on this path; this class covers only
    the `source` tag itself.
    """

    def test_explicit_entry_source_is_not_local_file(self, tmp_path, monkeypatch) -> None:
        entry = "tests.fixtures.entry_point_plugin:register"
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_entry", entry, raising=False)

        pm = PluginManager()

        record = next(r for r in pm.loaded if r.name == entry)
        assert record.source == "explicit-entry"
        assert record.source != "local-file"
        assert record.location == entry

    def test_explicit_entry_disabled_by_full_string_still_honored(
        self, tmp_path, monkeypatch
    ) -> None:
        """Regression guard for the source-value rename: the
        `plugins_disabled` gate on this load path (matched by the full
        `explicit_entry` string) must still work now that its `source` tag
        changed from `"local-file"` to `"explicit-entry"` — the skip
        happens before a `PluginRecord` is even constructed, so it must be
        source-value-agnostic."""
        entry = "tests.fixtures.entry_point_plugin:register"
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_entry", entry, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_disabled", [entry], raising=False)

        pm = PluginManager()

        assert "fixture-kind" not in RUNNER_MAP
        assert pm.loaded == []


class TestDeclaredNotifiersCollection:
    def test_notifiers_key_is_collected_not_treated_as_hook(self, tmp_path, monkeypatch) -> None:
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "notifier_fixture.py").write_text(
            "def _notify(msg):\n"
            "    return None\n\n\n"
            "def register():\n"
            "    return {'notifiers': {'fixture-notifier': _notify}}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = PluginManager()

        assert "fixture-notifier" in pm.declared_notifiers
        assert "notifiers" not in pm.hooks
