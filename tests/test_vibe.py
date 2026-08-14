"""`vibe` must be gated like every other third-party agent CLI.

It was the last one still hardcoded in `_BUILTIN_RUNNERS`, beside `claude` and
`openrouter`. Those two earn their place: `claude` is the reference agent, and
`openrouter` is API-only with no binary to gate on. `vibe` is neither -- it is
a CLI exactly like `codex`, `cursor`, `gemini`, `opencode` and `ollama`, all of
which were migrated out of the builtins so a deployment that does not use them
does not carry them.

The migration must be behaviour-preserving where the binary IS present: a box
with `vibe` installed and enabled must resolve the kind exactly as before.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def plugin():
    import importlib

    return importlib.import_module("plugins.vibe")


class TestItRegistersOnlyWhenUsable:
    def test_enabled_and_on_path_registers_the_runner(self, plugin, monkeypatch):
        from hivepilot.config import settings
        from hivepilot.runners.prompt_cli_runner import VibeRunner

        monkeypatch.setattr(settings, "vibe_enabled", True, raising=False)
        monkeypatch.setattr(plugin.shutil, "which", lambda _b: "/usr/local/bin/vibe")

        contributions = plugin.register()

        assert contributions["runners"]["vibe"] is VibeRunner

    def test_the_flag_off_registers_nothing(self, plugin, monkeypatch):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "vibe_enabled", False, raising=False)
        monkeypatch.setattr(plugin.shutil, "which", lambda _b: "/usr/local/bin/vibe")

        assert plugin.register() == {}

    def test_the_binary_absent_registers_nothing(self, plugin, monkeypatch):
        """A kind that cannot run must not be advertised as available."""
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "vibe_enabled", True, raising=False)
        monkeypatch.setattr(plugin.shutil, "which", lambda _b: None)

        assert plugin.register() == {}


class TestHealthSaysWhichAndWhy:
    def test_on_path_is_ok(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin.shutil, "which", lambda _b: "/usr/local/bin/vibe")

        assert plugin.health().status == "ok"

    def test_absent_is_degraded_and_names_the_kind(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin.shutil, "which", lambda _b: None)

        status = plugin.health()

        assert status.status == "degraded"
        assert "vibe" in status.detail


class TestItIsNoLongerABuiltin:
    def test_the_builtin_map_does_not_carry_it(self):
        """The whole point of the migration. Built-in agent kinds are now
        exactly {claude, openrouter}."""
        from hivepilot.registry import _BUILTIN_RUNNERS

        assert "vibe" not in _BUILTIN_RUNNERS

    def test_an_unavailable_vibe_names_the_flag_and_the_binary(self):
        """Not a bare KeyError: the operator must be told what to switch on or
        install, which is why `_OPTIONAL_AGENT_PLUGIN_KINDS` exists."""
        from hivepilot.registry import _OPTIONAL_AGENT_PLUGIN_KINDS

        assert _OPTIONAL_AGENT_PLUGIN_KINDS["vibe"] == ("vibe_enabled", "vibe")

    def test_it_is_still_a_recognised_agent_kind(self):
        """Gating changes where it comes from, never what it is."""
        from hivepilot.services.agent_checks import AGENT_RUNNER_KINDS

        assert "vibe" in AGENT_RUNNER_KINDS
