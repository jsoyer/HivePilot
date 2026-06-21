"""
Minimal tests for hivepilot.plugins — PluginManager and hooks type annotation.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Stub optional deps before importing
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


class TestPluginManagerHooksAnnotation:
    """Verify PluginManager.hooks has the correct type annotation."""

    def test_plugin_manager_importable(self) -> None:
        """hivepilot.plugins imports without error."""
        import hivepilot.plugins  # noqa: F401

        assert hivepilot.plugins is not None

    def test_plugin_manager_has_hooks_attribute(self) -> None:
        """PluginManager instance has a hooks attribute that is a dict."""
        from hivepilot.plugins import PluginManager

        pm = PluginManager()
        assert hasattr(pm, "hooks")
        assert isinstance(pm.hooks, dict)

    def test_hooks_dict_has_expected_keys(self) -> None:
        """hooks dict has at minimum before_step and after_step keys."""
        from hivepilot.plugins import PluginManager

        pm = PluginManager()
        assert "before_step" in pm.hooks
        assert "after_step" in pm.hooks

    def test_hooks_values_are_lists(self) -> None:
        """hooks values are lists."""
        from hivepilot.plugins import PluginManager

        pm = PluginManager()
        for value in pm.hooks.values():
            assert isinstance(value, list)

    def test_load_plugins_returns_list(self) -> None:
        """load_plugins() returns a list."""
        from hivepilot.plugins import load_plugins

        result = load_plugins()
        assert isinstance(result, list)


class TestLoadPluginsByPath:
    """Plugins load by file path — no dependency on `plugins` being on sys.path
    (regression: the installed binary / Telegram bot crashed with
    ModuleNotFoundError: No module named 'plugins')."""

    def test_loads_plugin_without_plugins_on_syspath(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "good.py").write_text(
            "def register():\n    return {'before_step': lambda **k: None}\n", encoding="utf-8"
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        assert "plugins" not in sys.modules  # not importable as a package here
        loaded = plugins_mod.load_plugins()
        assert len(loaded) == 1
        assert callable(loaded[0])

    def test_broken_plugin_is_skipped_not_fatal(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "ok.py").write_text("def register():\n    return {}\n", encoding="utf-8")
        (pdir / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        loaded = plugins_mod.load_plugins()  # must not raise
        assert len(loaded) == 1  # ok loaded, broken skipped
