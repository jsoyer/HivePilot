"""codex-cursor-plugins migration: `cursor` agent runner plugin.

Mirrors `tests/test_codex.py` / `tests/test_antigravity.py`'s coverage for
the canonical gated-agent-plugin skeleton, applied to `cursor` now that it
has moved OUT of `hivepilot.registry._BUILTIN_RUNNERS` and into
`plugins/cursor.py` (default-on, PATH-gated on the `cursor-agent` CLI
binary -- NOT `cursor`, matching `CursorRunner.command_name` in
`hivepilot.runners.cursor_runner` and `AGENT_INSTALL_SPECS["cursor"]` in
`hivepilot.services.agent_install`) -- same pattern as gemini/opencode/
ollama/pi/qwen-code/kimi-cli/antigravity/codex. `CursorRunner`'s invocation
logic is completely unchanged; only its *registration* moved.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

from hivepilot.config import settings
from hivepilot.registry import (
    _BUILTIN_RUNNERS,
    _OPTIONAL_AGENT_PLUGIN_KINDS,
    RUNNER_MAP,
    RunnerPluginUnavailableError,
    resolve_runner_class,
)
from hivepilot.runners.cursor_runner import CursorRunner

REPO_ROOT = Path(__file__).parent.parent


def _load_plugin_module():
    path = REPO_ROOT / "plugins" / "cursor.py"
    spec = importlib.util.spec_from_file_location("hivepilot_plugin_cursor_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Taxonomy: cursor is no longer a builtin, is now a known optional plugin kind
# ---------------------------------------------------------------------------


def test_cursor_not_in_builtin_runners() -> None:
    assert "cursor" not in _BUILTIN_RUNNERS


def test_cursor_in_optional_agent_plugin_kinds() -> None:
    assert _OPTIONAL_AGENT_PLUGIN_KINDS["cursor"] == ("cursor_enabled", "cursor-agent")


# ---------------------------------------------------------------------------
# Canonical gated-agent-plugin skeleton (register() gating semantics)
# ---------------------------------------------------------------------------


def test_flag_defaults_to_true() -> None:
    assert settings.cursor_enabled is True


def test_register_returns_cursor_runner_when_active(monkeypatch) -> None:
    module = _load_plugin_module()
    monkeypatch.setattr(settings, "cursor_enabled", True, raising=False)
    with patch.object(module.shutil, "which", return_value="/usr/local/bin/cursor-agent"):
        hooks = module.register()
    assert hooks.get("runners") == {"cursor": CursorRunner}
    assert "cursor" in hooks.get("health", {})


def test_register_returns_empty_when_flag_disabled(monkeypatch) -> None:
    module = _load_plugin_module()
    monkeypatch.setattr(settings, "cursor_enabled", False, raising=False)
    with patch.object(module.shutil, "which", return_value="/usr/local/bin/cursor-agent"):
        assert module.register() == {}


def test_register_returns_empty_when_binary_absent(monkeypatch) -> None:
    module = _load_plugin_module()
    monkeypatch.setattr(settings, "cursor_enabled", True, raising=False)
    with patch.object(module.shutil, "which", return_value=None):
        assert module.register() == {}


def test_register_returns_empty_when_both_flag_off_and_binary_absent(monkeypatch) -> None:
    module = _load_plugin_module()
    monkeypatch.setattr(settings, "cursor_enabled", False, raising=False)
    with patch.object(module.shutil, "which", return_value=None):
        assert module.register() == {}


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------


def test_health_ok_when_binary_present() -> None:
    module = _load_plugin_module()
    with patch.object(module.shutil, "which", return_value="/usr/local/bin/cursor-agent"):
        status = module.health()
    assert status.status == "ok"


def test_health_degraded_when_binary_absent() -> None:
    module = _load_plugin_module()
    with patch.object(module.shutil, "which", return_value=None):
        status = module.health()
    assert status.status == "degraded"


# ---------------------------------------------------------------------------
# Resolution via the REAL PluginManager -- clear error, never a bare KeyError
# ---------------------------------------------------------------------------


def test_kind_resolves_to_cursor_runner_when_binary_present(monkeypatch) -> None:
    from hivepilot import plugins as plugins_mod

    monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
    monkeypatch.setattr(settings, "cursor_enabled", True, raising=False)
    RUNNER_MAP.pop("cursor", None)

    with patch(
        "shutil.which",
        side_effect=lambda name: "/usr/local/bin/cursor-agent" if name == "cursor-agent" else None,
    ):
        plugins_mod.PluginManager()

    assert resolve_runner_class("cursor") is CursorRunner


def test_kind_unregistered_and_actionable_error_when_flag_disabled(monkeypatch) -> None:
    from hivepilot import plugins as plugins_mod

    monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
    monkeypatch.setattr(settings, "cursor_enabled", False, raising=False)
    RUNNER_MAP.pop("cursor", None)

    with patch(
        "shutil.which",
        side_effect=lambda name: "/usr/local/bin/cursor-agent" if name == "cursor-agent" else None,
    ):
        plugins_mod.PluginManager()

    assert "cursor" not in RUNNER_MAP
    with pytest.raises(RunnerPluginUnavailableError) as exc_info:
        resolve_runner_class("cursor")
    message = str(exc_info.value)
    assert "cursor" in message
    assert "cursor-agent" in message
    assert "CURSOR_ENABLED" in message.upper()


def test_kind_unregistered_and_actionable_error_when_binary_absent(monkeypatch) -> None:
    from hivepilot import plugins as plugins_mod

    monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
    monkeypatch.setattr(settings, "cursor_enabled", True, raising=False)
    RUNNER_MAP.pop("cursor", None)

    with patch("shutil.which", return_value=None):
        plugins_mod.PluginManager()

    assert "cursor" not in RUNNER_MAP

    with pytest.raises(RunnerPluginUnavailableError):
        resolve_runner_class("cursor")


def test_actionable_error_is_not_a_plain_keyerror(monkeypatch) -> None:
    from hivepilot import plugins as plugins_mod

    monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
    monkeypatch.setattr(settings, "cursor_enabled", False, raising=False)
    RUNNER_MAP.pop("cursor", None)
    plugins_mod.PluginManager()

    with pytest.raises(Exception) as exc_info:
        resolve_runner_class("cursor")
    assert not isinstance(exc_info.value, KeyError)
    assert isinstance(exc_info.value, RunnerPluginUnavailableError)
