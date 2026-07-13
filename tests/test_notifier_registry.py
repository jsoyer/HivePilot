"""
Tests for hivepilot.services.notification_service's NOTIFIER_MAP /
NotifierRegistry pluggable-notifier dispatch, and its wiring into
hivepilot.plugins.PluginManager.

Covers, per the Sprint 3 spec:
(a) built-in non-regression — NOTIFIER_MAP contains slack/discord/telegram
    mapped to the original functions immediately at import time
(b) registering a new notifier name and dispatching to it via
    send_notification()
(c) a name collision (no override) raises NotifierKindCollisionError
(d) an unregistered channel name logs a warning and does not raise
(e) a plugin notifier registered via PluginManager is present in
    NOTIFIER_MAP and is invoked by send_notification()
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot import plugins as plugins_mod
from hivepilot.plugins import PluginManager
from hivepilot.services import notification_service as notif_mod
from hivepilot.services.notification_service import (
    NOTIFIER_MAP,
    NotifierKindCollisionError,
    NotifierRegistry,
    _send_discord,
    _send_slack,
    _send_telegram,
    send_notification,
)


@pytest.fixture(autouse=True)
def _restore_notifier_map():
    """NOTIFIER_MAP is process-global mutable state — snapshot/restore around
    every test in this module so notifier names registered here never leak
    into other test modules sharing the pytest session (mirrors
    `_restore_runner_map` in test_plugin_loading_mechanisms.py)."""
    snapshot = dict(NOTIFIER_MAP)
    yield
    NOTIFIER_MAP.clear()
    NOTIFIER_MAP.update(snapshot)


class TestBuiltinNonRegression:
    def test_builtin_channels_registered_at_import(self) -> None:
        assert NOTIFIER_MAP["slack"] is _send_slack
        assert NOTIFIER_MAP["discord"] is _send_discord
        assert NOTIFIER_MAP["telegram"] is _send_telegram


class TestRegisterAndDispatch:
    def test_new_notifier_is_invoked_by_send_notification(self) -> None:
        calls: list[str] = []

        def _custom(message: str) -> None:
            calls.append(message)

        NotifierRegistry.register("my-channel", _custom)

        send_notification("hi", channels=["my-channel"])

        assert calls == ["hi"]


class TestKindCollision:
    def test_collision_without_override_raises(self) -> None:
        def _first(message: str) -> None:
            pass

        def _second(message: str) -> None:
            pass

        NotifierRegistry.register("collide-channel", _first)

        with pytest.raises(NotifierKindCollisionError):
            NotifierRegistry.register("collide-channel", _second)

    def test_plugin_notifier_collision_with_builtin_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "colliding_notifier.py").write_text(
            "def _notify(msg):\n"
            "    return None\n\n\n"
            "def register():\n"
            "    return {'notifiers': {'slack': _notify}}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(NotifierKindCollisionError):
            PluginManager()


class TestUnknownChannel:
    def test_unregistered_channel_logs_warning_and_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            notif_mod.logger,
            "warning",
            lambda event, **kwargs: warnings.append((event, kwargs)),
        )

        send_notification("hi", channels=["no-such-channel"])  # must not raise

        assert warnings == [("notification.unknown_channel", {"channel": "no-such-channel"})]


class TestPluginNotifierWiring:
    def test_plugin_notifier_registered_via_plugin_manager_is_invoked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "custom_notifier.py").write_text(
            "calls = []\n\n\n"
            "def _notify(msg):\n"
            "    calls.append(msg)\n\n\n"
            "def register():\n"
            "    return {'notifiers': {'custom': _notify}}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = PluginManager()

        assert "custom" in pm.declared_notifiers
        assert NOTIFIER_MAP["custom"] is pm.declared_notifiers["custom"]

        send_notification("hi", channels=["custom"])

        fn = NOTIFIER_MAP["custom"]
        # The fixture plugin module's own `calls` list, reached via the
        # registered function's __globals__ — proves the plugin's function
        # (not a stand-in) actually ran.
        assert fn.__globals__["calls"] == ["hi"]
