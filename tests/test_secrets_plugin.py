"""
Tests for Phase 19 Sprint 2 — `secrets` as the THIRD plugin provider type
(alongside `runners`/`notifiers`) in `hivepilot.plugins.PluginManager`.

Covers:
(a) a local-file plugin declaring `{"secrets": {"dummy": DummyBackend()}}`
    loads `dummy` into `SECRETS_MAP` and the plugin's `PluginRecord` reflects it
(b) a `secrets` name collision with a builtin (`env`) aborts the load with
    `SecretsBackendCollisionError` AND rolls back the SAME plugin's other
    contributions (a runner it also declared) — proving atomicity across all
    three provider types, not just within `secrets` itself
(c) an entry-point plugin can also contribute a `secrets` backend

Rely on the conftest autouse `_isolate_runner_and_notifier_maps` fixture,
which now also snapshots/restores `SECRETS_MAP` around every test.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from hivepilot import plugins as plugins_mod
from hivepilot.plugins import PLUGIN_ENTRY_POINT_GROUP, PluginManager
from hivepilot.registry import RUNNER_MAP, SECRETS_MAP, SecretsBackendCollisionError
from hivepilot.services.notification_service import NOTIFIER_MAP


def _write_secrets_plugin(plugin_dir: Path, filename: str, backend_name: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / filename).write_text(
        f"""
class DummyBackend:
    def resolve(self, ref, settings):
        return "dummy-value"


def register():
    return {{"secrets": {{"{backend_name}": DummyBackend()}}}}
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


class TestLocalFilePluginSecretsRegistration:
    def test_local_file_plugin_secrets_backend_is_registered(self, tmp_path, monkeypatch) -> None:
        _write_secrets_plugin(tmp_path / "plugins", "secrets_fixture.py", backend_name="dummy")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = PluginManager()

        assert "dummy" in SECRETS_MAP
        assert any(r.source == "local-file" and r.name == "secrets_fixture" for r in pm.loaded)


class TestSecretsBackendCollision:
    def test_collision_with_builtin_raises(self, tmp_path, monkeypatch) -> None:
        _write_secrets_plugin(tmp_path / "plugins", "collide_secrets.py", backend_name="env")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(SecretsBackendCollisionError):
            PluginManager()

        # The builtin must be left untouched.
        from hivepilot.services.secrets_service import EnvSecretsBackend

        assert isinstance(SECRETS_MAP["env"], EnvSecretsBackend)

    def test_collision_rolls_back_plugins_other_contributions(self, tmp_path, monkeypatch) -> None:
        """A single plugin declaring a runner AND a notifier AND a colliding
        `secrets` backend (name `env`, a builtin) must not leave the runner or
        notifier orphaned in the process-global maps — registration of one
        plugin's runners+notifiers+secrets is atomic across ALL three types."""
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "partial_secrets.py").write_text(
            """
class FreshRunner:
    def __init__(self, definition, settings):
        pass

    def run(self, payload):
        return None


def _fresh_notifier(msg):
    return None


class CollidingBackend:
    def resolve(self, ref, settings):
        return "colliding-value"


def register():
    # 'fresh-secrets-kind' runner and 'fresh-secrets-notifier' notifier
    # register first, then 'env' collides with the builtin secrets backend.
    return {
        "runners": {"fresh-secrets-kind": FreshRunner},
        "notifiers": {"fresh-secrets-notifier": _fresh_notifier},
        "secrets": {"env": CollidingBackend()},
    }
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(SecretsBackendCollisionError):
            PluginManager()

        # Neither the runner nor the notifier this same plugin declared were
        # left behind — the whole plugin's contribution was rolled back.
        assert "fresh-secrets-kind" not in RUNNER_MAP
        assert "fresh-secrets-notifier" not in NOTIFIER_MAP
        # And the builtin secrets backend was never overwritten either.
        from hivepilot.services.secrets_service import EnvSecretsBackend

        assert isinstance(SECRETS_MAP["env"], EnvSecretsBackend)


class TestEntryPointPluginSecretsRegistration:
    def test_entry_point_plugin_secrets_backend_is_registered(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        class _EpDummyBackend:
            def resolve(self, ref, settings):  # noqa: ANN001
                return "ep-dummy-value"

        def _register() -> dict[str, Any]:
            return {"secrets": {"ep-dummy": _EpDummyBackend()}}

        ep = _FakeEntryPoint(
            name="secrets-fixture-ep",
            value="tests.fixtures.secrets_entry_point_plugin:register",
            loader=lambda: _register,
        )
        _patch_entry_points(monkeypatch, [ep])

        pm = PluginManager()

        assert "ep-dummy" in SECRETS_MAP
        assert any(r.source == "entry-point" and r.name == "secrets-fixture-ep" for r in pm.loaded)
