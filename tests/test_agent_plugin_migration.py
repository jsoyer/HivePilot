"""Sprint 2 (runner-defaults-plugins-mode PRD): built-in reduction +
gated-plugin migration for gemini/opencode/ollama.

Covers:
- `_BUILTIN_RUNNERS` no longer registers gemini/opencode/ollama; `openrouter`
  is a new built-in agent kind alongside claude/codex/vibe.
- `KNOWN_RUNNER_KINDS` (the "built-in" doc/grouping tuple `plugins list`
  reads) is updated to match.
- Each migrated plugin (`plugins/gemini.py` / `plugins/opencode.py` /
  `plugins/ollama.py`) follows the canonical gated-agent-plugin skeleton:
  `register()` returns `{}` when EITHER its per-plugin enable flag is off OR
  its CLI binary is absent from PATH, else `{"runners": {<kind>:
  <ExistingRunnerClass>}}` — the SAME `GeminiRunner` / `OpenCodeRunner` /
  `OllamaRunner` classes that already lived in `prompt_cli_runner.py`
  (invocation logic untouched; only registration relocated here).
- Backward compat: with the flag on (default True) and the binary present,
  `kind: gemini` resolves to the exact same runner class it always has.
- Fail-closed-but-actionable: with the flag off OR the binary absent, the
  kind is not registered and resolving it raises a clear, actionable error
  (naming the enable flag + required binary) rather than a bare `KeyError`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from hivepilot.config import settings
from hivepilot.models import KNOWN_RUNNER_KINDS
from hivepilot.registry import RUNNER_MAP, RunnerPluginUnavailableError, resolve_runner_class
from hivepilot.runners.claude_runner import ClaudeRunner
from hivepilot.runners.openrouter_runner import OpenRouterRunner
from hivepilot.runners.prompt_cli_runner import (
    CodexRunner,
    GeminiRunner,
    OllamaRunner,
    OpenCodeRunner,
    VibeRunner,
)

REPO_ROOT = Path(__file__).parent.parent

# (plugin file stem, runner kind, existing runner class, per-plugin enable flag)
_PLUGIN_SPECS = [
    ("gemini", "gemini", GeminiRunner, "gemini_enabled"),
    ("opencode", "opencode", OpenCodeRunner, "opencode_enabled"),
    ("ollama", "ollama", OllamaRunner, "ollama_enabled"),
]


def _load_plugin_module(stem: str) -> ModuleType:
    """Load plugins/<stem>.py by file path — same mechanism
    `hivepilot.plugins._scan_local_plugins` uses (mirrors tests/test_rtk.py)."""
    path = REPO_ROOT / "plugins" / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(f"hivepilot_plugin_{stem}_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# _BUILTIN_RUNNERS / KNOWN_RUNNER_KINDS taxonomy
# ---------------------------------------------------------------------------


class TestBuiltinReduction:
    def test_gemini_opencode_ollama_not_in_builtin_runners(self) -> None:
        from hivepilot.registry import _BUILTIN_RUNNERS

        for kind in ("gemini", "opencode", "ollama"):
            assert kind not in _BUILTIN_RUNNERS

    def test_claude_codex_vibe_openrouter_are_builtin_runners(self) -> None:
        from hivepilot.registry import _BUILTIN_RUNNERS

        assert _BUILTIN_RUNNERS["claude"] is ClaudeRunner
        assert _BUILTIN_RUNNERS["codex"] is CodexRunner
        assert _BUILTIN_RUNNERS["vibe"] is VibeRunner
        assert _BUILTIN_RUNNERS["openrouter"] is OpenRouterRunner

    def test_openrouter_registered_in_runner_map_by_default(self) -> None:
        assert RUNNER_MAP.get("openrouter") is OpenRouterRunner

    def test_gemini_opencode_ollama_not_in_known_runner_kinds(self) -> None:
        for kind in ("gemini", "opencode", "ollama"):
            assert kind not in KNOWN_RUNNER_KINDS

    def test_openrouter_in_known_runner_kinds(self) -> None:
        assert "openrouter" in KNOWN_RUNNER_KINDS

    def test_claude_codex_vibe_still_in_known_runner_kinds(self) -> None:
        for kind in ("claude", "codex", "vibe"):
            assert kind in KNOWN_RUNNER_KINDS


# ---------------------------------------------------------------------------
# Canonical gated-agent-plugin skeleton (register() gating semantics)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stem,kind,runner_cls,flag_name", _PLUGIN_SPECS)
class TestGatedAgentPluginSkeleton:
    def test_flag_defaults_to_true(self, stem, kind, runner_cls, flag_name) -> None:
        assert getattr(settings, flag_name) is True

    def test_register_exposes_kind_when_enabled_and_binary_present(
        self, stem, kind, runner_cls, flag_name, monkeypatch
    ) -> None:
        module = _load_plugin_module(stem)
        monkeypatch.setattr(settings, flag_name, True, raising=False)
        with patch.object(module.shutil, "which", return_value=f"/usr/local/bin/{stem}"):
            hooks = module.register()
        assert hooks.get("runners") == {kind: runner_cls}

    def test_register_returns_empty_when_flag_disabled(
        self, stem, kind, runner_cls, flag_name, monkeypatch
    ) -> None:
        module = _load_plugin_module(stem)
        monkeypatch.setattr(settings, flag_name, False, raising=False)
        with patch.object(module.shutil, "which", return_value=f"/usr/local/bin/{stem}"):
            assert module.register() == {}

    def test_register_returns_empty_when_binary_absent(
        self, stem, kind, runner_cls, flag_name, monkeypatch
    ) -> None:
        module = _load_plugin_module(stem)
        monkeypatch.setattr(settings, flag_name, True, raising=False)
        with patch.object(module.shutil, "which", return_value=None):
            assert module.register() == {}

    def test_register_returns_empty_when_both_flag_off_and_binary_absent(
        self, stem, kind, runner_cls, flag_name, monkeypatch
    ) -> None:
        module = _load_plugin_module(stem)
        monkeypatch.setattr(settings, flag_name, False, raising=False)
        with patch.object(module.shutil, "which", return_value=None):
            assert module.register() == {}


# ---------------------------------------------------------------------------
# Backward-compat + actionable-error resolution via the REAL PluginManager
# ---------------------------------------------------------------------------


class TestBackwardCompatResolutionViaRealPluginManager:
    @pytest.fixture(autouse=True)
    def _restore_runner_map(self):
        """RUNNER_MAP is process-global mutable state — snapshot/restore
        around every test here (mirrors tests/test_rtk.py) so a real
        PluginManager() scan in one test never leaks a registered/unregistered
        kind into another test in the same session. (conftest.py's autouse
        `_isolate_runner_and_notifier_maps` fixture already does this
        globally after every test; this local fixture is redundant but kept
        for parity with the established test_rtk.py pattern.)"""
        snapshot = dict(RUNNER_MAP)
        yield
        RUNNER_MAP.clear()
        RUNNER_MAP.update(snapshot)

    @pytest.mark.parametrize("stem,kind,runner_cls,flag_name", _PLUGIN_SPECS)
    def test_kind_resolves_to_original_runner_class_when_binary_present(
        self, stem, kind, runner_cls, flag_name, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(settings, flag_name, True, raising=False)
        RUNNER_MAP.pop(kind, None)

        with patch(
            "shutil.which",
            side_effect=lambda name: f"/usr/local/bin/{name}" if name == stem else None,
        ):
            plugins_mod.PluginManager()

        assert resolve_runner_class(kind) is runner_cls

    @pytest.mark.parametrize("stem,kind,runner_cls,flag_name", _PLUGIN_SPECS)
    def test_kind_unregistered_and_actionable_error_when_flag_disabled(
        self, stem, kind, runner_cls, flag_name, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(settings, flag_name, False, raising=False)
        RUNNER_MAP.pop(kind, None)

        with patch(
            "shutil.which",
            side_effect=lambda name: f"/usr/local/bin/{name}" if name == stem else None,
        ):
            plugins_mod.PluginManager()

        assert kind not in RUNNER_MAP
        with pytest.raises(RunnerPluginUnavailableError) as exc_info:
            resolve_runner_class(kind)
        message = str(exc_info.value)
        assert kind in message
        assert stem in message  # names the required binary
        assert flag_name.upper() in message.upper()

    @pytest.mark.parametrize("stem,kind,runner_cls,flag_name", _PLUGIN_SPECS)
    def test_kind_unregistered_and_actionable_error_when_binary_absent(
        self, stem, kind, runner_cls, flag_name, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(settings, flag_name, True, raising=False)
        RUNNER_MAP.pop(kind, None)

        with patch("shutil.which", return_value=None):
            plugins_mod.PluginManager()

        assert kind not in RUNNER_MAP
        with pytest.raises(RunnerPluginUnavailableError):
            resolve_runner_class(kind)

    @pytest.mark.parametrize("stem,kind,runner_cls,flag_name", _PLUGIN_SPECS)
    def test_actionable_error_is_not_a_plain_keyerror(
        self, stem, kind, runner_cls, flag_name, monkeypatch
    ) -> None:
        """Acceptance: resolution yields the actionable error, not a bare
        KeyError — distinguishable so callers/operators get real guidance."""
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(settings, flag_name, False, raising=False)
        RUNNER_MAP.pop(kind, None)
        plugins_mod.PluginManager()

        with pytest.raises(Exception) as exc_info:
            resolve_runner_class(kind)
        assert not isinstance(exc_info.value, KeyError)
        assert isinstance(exc_info.value, RunnerPluginUnavailableError)


# ---------------------------------------------------------------------------
# A genuinely unknown kind is unaffected — still the plain, existing KeyError.
# ---------------------------------------------------------------------------


def test_genuinely_unknown_kind_still_raises_plain_keyerror() -> None:
    with pytest.raises(KeyError) as exc_info:
        resolve_runner_class("totally-made-up-kind")
    message = str(exc_info.value)
    assert "Unknown runner kind" in message
    assert not isinstance(exc_info.value, RunnerPluginUnavailableError)
