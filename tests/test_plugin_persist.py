"""Tests for `hivepilot.ui.plugin_persist` — the Textual-free extraction of
`persist_plugins_disabled` (previously defined in `hivepilot.ui.plugin_manager`).

This module MUST be importable without `textual` installed (it backs
`hivepilot.services.api_service`, which must not transitively require the
TUI-only optional dependency). See `tests/test_plugin_manager_tui.py` for the
re-export/monkeypatch-compatibility coverage on the `plugin_manager` side.
"""

from __future__ import annotations

import json
import sys

import pytest


def test_importable_without_textual(monkeypatch: pytest.MonkeyPatch) -> None:
    """`plugin_persist` must not import `textual` anywhere in its chain."""
    for mod_name in list(sys.modules):
        if mod_name == "textual" or mod_name.startswith("textual."):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)

    class _BlockTextual:
        def find_spec(self, name, path=None, target=None):
            if name == "textual" or name.startswith("textual."):
                raise ModuleNotFoundError(f"No module named {name!r}")
            return None

    blocker = _BlockTextual()
    sys.meta_path.insert(0, blocker)
    try:
        for mod_name in list(sys.modules):
            if mod_name == "hivepilot.ui.plugin_persist":
                monkeypatch.delitem(sys.modules, mod_name, raising=False)
        from hivepilot.ui.plugin_persist import persist_plugins_disabled

        assert callable(persist_plugins_disabled)
    finally:
        sys.meta_path.remove(blocker)


def test_persist_plugins_disabled_upserts_env_file(tmp_path) -> None:
    from hivepilot.ui.plugin_persist import persist_plugins_disabled

    env_path = tmp_path / ".env"
    env_path.write_text("SOME_OTHER_VAR=1\n", encoding="utf-8")

    persist_plugins_disabled(["rtk", "obsidian"], env_path=env_path)
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert "SOME_OTHER_VAR=1" in lines
    disabled_line = next(line for line in lines if line.startswith("HIVEPILOT_PLUGINS_DISABLED="))
    assert json.loads(disabled_line.split("=", 1)[1]) == ["obsidian", "rtk"]

    # Second call replaces (does not duplicate) the existing line.
    persist_plugins_disabled(["rtk"], env_path=env_path)
    lines = env_path.read_text(encoding="utf-8").splitlines()
    disabled_lines = [line for line in lines if line.startswith("HIVEPILOT_PLUGINS_DISABLED=")]
    assert len(disabled_lines) == 1
    assert json.loads(disabled_lines[0].split("=", 1)[1]) == ["rtk"]


def test_persist_plugins_disabled_creates_missing_env_file(tmp_path) -> None:
    from hivepilot.ui.plugin_persist import persist_plugins_disabled

    env_path = tmp_path / "nested" / ".env"
    result = persist_plugins_disabled(["rtk"], env_path=env_path)

    assert result == env_path
    assert env_path.exists()
    content = env_path.read_text(encoding="utf-8")
    assert json.loads(content.strip().split("=", 1)[1]) == ["rtk"]


def test_persist_plugins_disabled_default_env_path_uses_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from hivepilot.config import Settings
    from hivepilot.ui.plugin_persist import persist_plugins_disabled

    env_path = tmp_path / ".env"
    monkeypatch.setitem(Settings.model_config, "env_file", str(env_path))

    persist_plugins_disabled(["rtk", "obsidian"])
    assert env_path.exists()
