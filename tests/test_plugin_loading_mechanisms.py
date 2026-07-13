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
from hivepilot.plugins import PLUGIN_ENTRY_POINT_GROUP, PluginManager, PluginRecord
from hivepilot.registry import RUNNER_MAP, RunnerKindCollisionError
from tests.fixtures import entry_point_plugin as fixture_module


@pytest.fixture(autouse=True)
def _restore_runner_map():
    """RUNNER_MAP is process-global mutable state — snapshot/restore around
    every test in this module so runner kinds registered here never leak into
    other test modules sharing the pytest session."""
    snapshot = dict(RUNNER_MAP)
    yield
    RUNNER_MAP.clear()
    RUNNER_MAP.update(snapshot)


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
