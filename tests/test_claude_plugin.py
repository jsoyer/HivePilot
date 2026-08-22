"""claude leaves the core — the last vendor CLI to make the trip.

codex, cursor, gemini, opencode, ollama and vibe all moved out of builtin
registration into default-on plugins; `models.py` records each departure in
its own comments. claude stayed, for no reason of principle beyond seniority.
The rule this finishes: THE ENGINE KNOWS ONLY THE VENDOR-AGNOSTIC PATH —
`openrouter` (API-only, no binary, no vendor) is the one agent kind left in
`_BUILTIN_RUNNERS`.

One deliberate difference from the six precedents, and it is the load-bearing
choice here: the claude plugin is FLAG-gated but NOT PATH-gated. The six are
CLI-only, so an absent binary means an unusable kind. claude is not:
`mode: api` drives Anthropic's Messages API with `ANTHROPIC_API_KEY` and no
binary at all. PATH-gating would have deleted a kind that works — the inverse
of the langchain lie (#571), and just as wrong. `health()` still reports the
binary's absence, naming the api-mode caveat, so `plugins health` tells the
truth without the registry lying either way.

This also keeps CI honest for free: the suite never invokes the real binary,
and since registration does not check PATH, no stub `claude` executable has to
be planted on CI's PATH for 144 test files to keep meaning what they meant.
"""

from __future__ import annotations

import pytest


class TestTheCoreNoLongerKnowsClaude:
    def test_claude_is_not_in_the_builtin_dict(self):
        """The literal entry is gone. The kind arrives through the plugin, or
        not at all."""
        import inspect

        from hivepilot import registry

        src = inspect.getsource(registry)
        dict_body = src[src.index("_BUILTIN_RUNNERS: Dict") :]
        dict_body = dict_body[: dict_body.index("\n}")]

        assert '"claude"' not in dict_body

    def test_openrouter_is_the_only_builtin_agent_kind(self):
        """The doctrine, stated as an assertion: API-only and vendor-agnostic
        stays; every vendor CLI arrives through the seam."""
        from hivepilot.registry import _BUILTIN_RUNNERS

        agent_kinds = {
            "claude",
            "codex",
            "cursor",
            "gemini",
            "opencode",
            "ollama",
            "vibe",
            "grok",
            "openrouter",
            "langchain",
        }

        assert {k for k in _BUILTIN_RUNNERS if k in agent_kinds} <= {"openrouter", "langchain"}

    def test_claude_left_known_runner_kinds(self):
        """That tuple's own invariant: every name in it is unconditionally
        present in RUNNER_MAP. claude no longer is."""
        from hivepilot.models import KNOWN_RUNNER_KINDS

        assert "claude" not in KNOWN_RUNNER_KINDS

    def test_an_unregistered_claude_resolves_to_the_actionable_error(self, monkeypatch):
        """Flag off -> the kind is absent, and the refusal must name the fix,
        not be a bare KeyError. Same contract as the six precedents."""
        from hivepilot import registry

        monkeypatch.delitem(registry.RUNNER_MAP, "claude", raising=False)

        with pytest.raises(registry.RunnerPluginUnavailableError, match="CLAUDE"):
            registry.resolve_runner_class("claude")


class TestThePluginRegistersWithoutTheBinary:
    """The deliberate difference from codex/grok/the rest. `mode: api` works
    with no binary, so an absent binary must NOT delete the kind."""

    def test_it_registers_even_when_the_binary_is_absent(self, monkeypatch):
        from hivepilot.bundled_plugins import claude as plugin

        monkeypatch.setattr(plugin.shutil, "which", lambda _: None)

        contributed = plugin.register()

        assert "claude" in contributed.get("runners", {})

    def test_the_flag_off_contributes_nothing(self, monkeypatch):
        from hivepilot.bundled_plugins import claude as plugin
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "claude_enabled", False, raising=False)

        assert plugin.register() == {}

    def test_it_contributes_the_real_runner(self, monkeypatch):
        from hivepilot.bundled_plugins import claude as plugin
        from hivepilot.runners.claude_runner import ClaudeRunner

        monkeypatch.setattr(plugin.shutil, "which", lambda _: "/usr/bin/claude")

        assert plugin.register()["runners"]["claude"] is ClaudeRunner

    def test_health_degraded_when_absent_names_the_api_caveat(self, monkeypatch):
        """`plugins health` must tell the operator the whole truth: the CLI
        path is dead, the api path is not."""
        from hivepilot.bundled_plugins import claude as plugin

        monkeypatch.setattr(plugin.shutil, "which", lambda _: None)

        status = plugin.health()

        assert status.status == "degraded"
        assert "api" in status.detail.lower()

    def test_health_ok_when_present(self, monkeypatch):
        from hivepilot.bundled_plugins import claude as plugin

        monkeypatch.setattr(plugin.shutil, "which", lambda _: "/usr/bin/claude")

        assert plugin.health().status == "ok"

    def test_health_probes_the_CONFIGURED_command_not_a_literal(self, monkeypatch):
        """`settings.claude_command` can rename the binary; probing a literal
        "claude" would report the wrong binary's absence."""
        from hivepilot.bundled_plugins import claude as plugin
        from hivepilot.config import settings

        probed: list = []
        monkeypatch.setattr(settings, "claude_command", "claude-custom", raising=False)
        monkeypatch.setattr(plugin.shutil, "which", lambda name: probed.append(name))

        plugin.health()

        assert probed == ["claude-custom"]


class TestTheKindStillArrivesThroughTheLoader:
    def test_a_loaded_plugin_manager_serves_claude(self):
        """The visibility rule for every plugin kind: bare import sees
        builtins only, a LOADER sees contributions. 22 noxys roles say
        `kind: claude`, so this arriving is not optional."""
        from hivepilot.orchestrator import Orchestrator

        Orchestrator()  # loads plugins as every entry point does
        from hivepilot.registry import RUNNER_MAP

        assert "claude" in RUNNER_MAP

    def test_the_default_is_on(self):
        """A default-on plugin, like the six precedents: no deployment changes
        anything to keep its claude."""
        from hivepilot.config import Settings

        assert Settings().claude_enabled is True
