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
from conftest import BUNDLED_PLUGINS

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
    path = BUNDLED_PLUGINS / "cursor.py"
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


class TestCursorIsOnTheClaudeShapedPath:
    """Moved off `PromptCliRunner`, which applies neither `permission_mode`
    nor `allowed_tools` and therefore gave cursor none of the shared
    infrastructure.

    Its headless surface IS claude's — `-p/--print` with the same spelling AND
    the same shape (a boolean, prompt positional, unlike grok's `-p <PROMPT>`),
    `--model`, `--add-dir`, `--output-format`. Read from `cursor-agent --help`.
    """

    def test_it_inherits_claude_not_prompt_cli(self):
        from hivepilot.runners.claude_runner import ClaudeRunner
        from hivepilot.runners.cursor_runner import CursorRunner
        from hivepilot.runners.prompt_cli_runner import PromptCliRunner

        assert issubclass(CursorRunner, ClaudeRunner)
        assert not issubclass(CursorRunner, PromptCliRunner)

    def test_it_gains_the_shared_infrastructure(self):
        """The reason for the move. None of this was reachable from
        `PromptCliRunner`."""
        import inspect

        from hivepilot.runners.claude_runner import ClaudeRunner

        src = inspect.getsource(ClaudeRunner)

        assert "run_in_pane(" in src, "herdr surface"
        assert "run_in_container(" in src, "container workspace"
        assert "set_last_resolved_model(" in src, "resolved-model stamp"
        assert '"--add-dir"' in src, "skills scratch dir"

    def test_api_mode_still_fails_closed(self):
        """`ClaudeRunner` advertises cli+api, but its API path is Anthropic's
        Messages API. A resolved `mode: api` must be refused, never dispatched
        to the wrong vendor."""
        import pytest

        from hivepilot.runners.base import RunnerModeUnsupportedError, validate_runner_mode
        from hivepilot.runners.cursor_runner import CursorRunner

        with pytest.raises(RunnerModeUnsupportedError):
            validate_runner_mode("cursor", CursorRunner.supported_modes, "api")


class TestCursorDeclaresNothingBecauseItApplesNothing:
    """cursor-agent has NO `--allowed-tools` and NO `--permission-mode`. Its
    nearest kin — `--mode plan|ask`, `--sandbox`, `--force` ("Force allow
    commands unless explicitly DENIED", so a deny mechanism exists at config
    level) — are not drop-ins, and mapping `bypassPermissions` onto `--force`
    by eye is exactly the guess this codebase keeps paying for."""

    def test_it_honours_nothing(self):
        from hivepilot.runners.cursor_runner import CursorRunner

        assert CursorRunner.honoured_controls == frozenset()

    def test_a_restricted_role_is_still_refused_on_cursor(self):
        """Unchanged since #569, and correct: an unrestricted agent is not a
        degraded outcome, it is a different one."""
        import pytest

        from hivepilot.runners.base import RunnerControlUnsupportedError, assert_runner_honours
        from hivepilot.runners.cursor_runner import CursorRunner

        with pytest.raises(RunnerControlUnsupportedError):
            assert_runner_honours("cursor", CursorRunner, {"allowed_tools": ["Bash(rtk git:*)"]})

    def test_the_flags_are_None_not_inherited(self):
        """`None` means "no such flag", NOT "use claude's". Inheriting
        `--allowed-tools` would pass cursor a flag it rejects."""
        from hivepilot.runners.cursor_runner import CursorRunner

        assert CursorRunner.allowed_tools_flag is None
        assert CursorRunner.permission_mode_flag is None

    def test_the_declaration_and_the_flags_agree(self):
        """The invariant that keeps `assert_runner_honours` honest: a runner
        that emits no flag must not claim to apply the control. Checked as a
        rule rather than for cursor alone, so the next runner cannot break it
        quietly."""
        from hivepilot.runners.claude_runner import ClaudeRunner
        from hivepilot.runners.cursor_runner import CursorRunner
        from hivepilot.runners.grok_runner import GrokRunner

        for runner in (ClaudeRunner, GrokRunner, CursorRunner):
            honoured = runner.honoured_controls
            assert ("allowed_tools" in honoured) == (runner.allowed_tools_flag is not None), (
                f"{runner.__name__} disagrees with itself about allowed_tools"
            )
            assert ("permission_mode" in honoured) == (runner.permission_mode_flag is not None), (
                f"{runner.__name__} disagrees with itself about permission_mode"
            )


class TestTheBaseSkipsAbsentFlags:
    def test_no_allow_flag_means_no_allow_argv(self):
        """A `None` flag must emit NOTHING — passing `--allowed-tools` to a
        CLI that has never heard of it fails obscurely at dispatch."""
        import inspect

        from hivepilot.runners.claude_runner import ClaudeRunner

        src = inspect.getsource(ClaudeRunner._build_invocation)

        assert "if allowed_tools and self.allowed_tools_flag:" in src
        assert "if self.permission_mode_flag:" in src

    def test_claude_and_grok_still_emit_theirs(self):
        """The discriminating half: a base that always skipped would pass
        every skipping test."""
        from hivepilot.runners.claude_runner import ClaudeRunner
        from hivepilot.runners.grok_runner import GrokRunner

        assert ClaudeRunner.allowed_tools_flag == "--allowed-tools"
        assert GrokRunner.allowed_tools_flag == "--allow"
