"""codex-cursor-plugins migration: `codex` agent runner plugin.

Mirrors `tests/test_antigravity.py` / `tests/test_agent_plugin_migration.py`'s
coverage for the canonical gated-agent-plugin skeleton, applied to `codex`
now that it has moved OUT of `hivepilot.registry._BUILTIN_RUNNERS` and into
`plugins/codex.py` (default-on, PATH-gated on the `codex` CLI binary) --
same pattern as gemini/opencode/ollama/pi/qwen-code/kimi-cli/antigravity.
`CodexRunner`'s invocation logic (`hivepilot.runners.prompt_cli_runner`) is
completely unchanged; only its *registration* moved.
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
from hivepilot.runners.prompt_cli_runner import CodexRunner
from hivepilot.services.agent_checks import MANDATORY_AGENTS, check_mandatory_agents

REPO_ROOT = Path(__file__).parent.parent


def _load_plugin_module():
    path = REPO_ROOT / "plugins" / "codex.py"
    spec = importlib.util.spec_from_file_location("hivepilot_plugin_codex_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Taxonomy: codex is no longer a builtin, is now a known optional plugin kind
# ---------------------------------------------------------------------------


def test_codex_not_in_builtin_runners() -> None:
    assert "codex" not in _BUILTIN_RUNNERS


def test_codex_in_optional_agent_plugin_kinds() -> None:
    assert _OPTIONAL_AGENT_PLUGIN_KINDS["codex"] == ("codex_enabled", "codex")


# ---------------------------------------------------------------------------
# Canonical gated-agent-plugin skeleton (register() gating semantics)
# ---------------------------------------------------------------------------


def test_flag_defaults_to_true() -> None:
    assert settings.codex_enabled is True


def test_register_returns_codex_runner_when_active(monkeypatch) -> None:
    module = _load_plugin_module()
    monkeypatch.setattr(settings, "codex_enabled", True, raising=False)
    with patch.object(module.shutil, "which", return_value="/usr/local/bin/codex"):
        hooks = module.register()
    assert hooks.get("runners") == {"codex": CodexRunner}
    assert "codex" in hooks.get("health", {})


def test_register_returns_empty_when_flag_disabled(monkeypatch) -> None:
    module = _load_plugin_module()
    monkeypatch.setattr(settings, "codex_enabled", False, raising=False)
    with patch.object(module.shutil, "which", return_value="/usr/local/bin/codex"):
        assert module.register() == {}


def test_register_returns_empty_when_binary_absent(monkeypatch) -> None:
    module = _load_plugin_module()
    monkeypatch.setattr(settings, "codex_enabled", True, raising=False)
    with patch.object(module.shutil, "which", return_value=None):
        assert module.register() == {}


def test_register_returns_empty_when_both_flag_off_and_binary_absent(monkeypatch) -> None:
    module = _load_plugin_module()
    monkeypatch.setattr(settings, "codex_enabled", False, raising=False)
    with patch.object(module.shutil, "which", return_value=None):
        assert module.register() == {}


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------


def test_health_ok_when_binary_present() -> None:
    module = _load_plugin_module()
    with patch.object(module.shutil, "which", return_value="/usr/local/bin/codex"):
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


def test_kind_resolves_to_codex_runner_when_binary_present(monkeypatch) -> None:
    from hivepilot import plugins as plugins_mod

    monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
    monkeypatch.setattr(settings, "codex_enabled", True, raising=False)
    RUNNER_MAP.pop("codex", None)

    with patch(
        "shutil.which",
        side_effect=lambda name: "/usr/local/bin/codex" if name == "codex" else None,
    ):
        plugins_mod.PluginManager()

    assert resolve_runner_class("codex") is CodexRunner


def test_kind_unregistered_and_actionable_error_when_flag_disabled(monkeypatch) -> None:
    from hivepilot import plugins as plugins_mod

    monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
    monkeypatch.setattr(settings, "codex_enabled", False, raising=False)
    RUNNER_MAP.pop("codex", None)

    with patch(
        "shutil.which",
        side_effect=lambda name: "/usr/local/bin/codex" if name == "codex" else None,
    ):
        plugins_mod.PluginManager()

    assert "codex" not in RUNNER_MAP
    with pytest.raises(RunnerPluginUnavailableError) as exc_info:
        resolve_runner_class("codex")
    message = str(exc_info.value)
    assert "codex" in message
    assert "CODEX_ENABLED" in message.upper()


def test_kind_unregistered_and_actionable_error_when_binary_absent(monkeypatch) -> None:
    from hivepilot import plugins as plugins_mod

    monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
    monkeypatch.setattr(settings, "codex_enabled", True, raising=False)
    RUNNER_MAP.pop("codex", None)

    with patch("shutil.which", return_value=None):
        plugins_mod.PluginManager()

    assert "codex" not in RUNNER_MAP

    with pytest.raises(RunnerPluginUnavailableError):
        resolve_runner_class("codex")


# ---------------------------------------------------------------------------
# check_mandatory_agents / MANDATORY_AGENTS -- unaffected by builtin-vs-plugin
# ---------------------------------------------------------------------------


def test_codex_still_in_mandatory_agents() -> None:
    assert "codex" in MANDATORY_AGENTS


def test_check_mandatory_agents_still_recognizes_codex_on_path(monkeypatch) -> None:
    import shutil as shutil_mod

    monkeypatch.setattr(
        shutil_mod, "which", lambda name: "/usr/local/bin/codex" if name == "codex" else None
    )
    report = check_mandatory_agents()
    assert "codex" in report.present
    assert report.any_ok is True
