"""Tests for uniform plugin/runner gating (plugin-arch-overhaul PRD, Sprint 01).

Covers acceptance criteria 1, 3, 4, 5, 6, 7 from the sprint spec:
  1. The `_BUILTIN_RUNNERS` registration gate excludes disabled agent kinds
     while always including infra kinds (which have no `<kind>_enabled` flag).
  3. `active_agent_runner_kinds()` reflects whichever agent kinds are
     currently registered in `RUNNER_MAP`.
  4. `sample` / `sample_skill` plugins contribute nothing by default and
     contribute their payload once their flag is flipped True.
  5. Every `plugins/*.py` stem has a matching `Settings.<stem>_enabled` flag,
     and flipping that flag False makes `register()` return `{}`
     unconditionally (regardless of any additional PATH-gating a plugin also
     performs).
  6. `check_mandatory_agents()` / `MANDATORY_AGENTS` regression — still
     warn-only, never raises.
  7. `AGENT_RUNNER_KINDS` is defined exactly once (in
     `hivepilot.services.agent_checks`) and imported — not redefined — by
     `hivepilot/registry.py` and `hivepilot/orchestrator.py`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from hivepilot.config import Settings, settings
from hivepilot.registry import _BUILTIN_RUNNERS, active_agent_runner_kinds
from hivepilot.services.agent_checks import (
    AGENT_RUNNER_KINDS,
    MANDATORY_AGENTS,
    check_mandatory_agents,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / "plugins"


def _load_plugin_module(stem: str):
    """Load plugins/<stem>.py by file path (never `import plugins.<stem>` —
    that would insert a `plugins` package into `sys.modules` and break
    tests/test_plugins.py's `assert "plugins" not in sys.modules` isolation
    assumption). Mirrors tests/test_sample.py's existing loading pattern."""
    path = PLUGINS_DIR / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(f"hivepilot_test_gating_{stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plugin_stems() -> list[str]:
    return sorted(p.stem for p in PLUGINS_DIR.glob("*.py") if p.stem != "__init__")


# ---------------------------------------------------------------------------
# 1. _BUILTIN_RUNNERS gate excludes disabled agent kinds, keeps infra kinds
# ---------------------------------------------------------------------------


class TestBuiltinRunnersGate:
    def test_gate_excludes_disabled_agent_kinds_includes_infra(self) -> None:
        # codex-cursor-plugins migration: codex/cursor moved OUT of
        # _BUILTIN_RUNNERS into gated plugins (see TestAgentRunnerKindsSingleSourceOfTruth
        # / tests/test_codex.py / tests/test_cursor.py for their own gating
        # coverage) -- the built-in agent set this class exercises is now
        # exactly {claude, vibe, openrouter}.
        s = Settings(
            _env_file=None,  # type: ignore[call-arg]
            claude_enabled=False,
            vibe_enabled=False,
            openrouter_enabled=False,
        )
        active = {kind for kind in _BUILTIN_RUNNERS if getattr(s, f"{kind}_enabled", True)}
        for disabled in ("claude", "vibe", "openrouter"):
            assert disabled not in active
        # infra kinds carry no `<kind>_enabled` flag -> getattr(..., True) default wins
        for infra in ("shell", "terraform", "kubectl", "helm"):
            assert infra in active

    def test_gate_keeps_all_agents_active_by_default(self) -> None:
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        active = {kind for kind in _BUILTIN_RUNNERS if getattr(s, f"{kind}_enabled", True)}
        for agent in ("claude", "vibe", "openrouter"):
            assert agent in active

    def test_only_claude_disabled_excludes_only_claude(self) -> None:
        s = Settings(_env_file=None, claude_enabled=False)  # type: ignore[call-arg]
        active = {kind for kind in _BUILTIN_RUNNERS if getattr(s, f"{kind}_enabled", True)}
        assert "claude" not in active
        for agent in ("vibe", "openrouter"):
            assert agent in active


# ---------------------------------------------------------------------------
# 3. active_agent_runner_kinds()
# ---------------------------------------------------------------------------


class TestActiveAgentRunnerKindsHelper:
    def test_intersects_runner_map_with_agent_kinds(self) -> None:
        from hivepilot.registry import RUNNER_MAP

        RUNNER_MAP.clear()
        RUNNER_MAP["claude"] = object()  # type: ignore[assignment]  # agent kind
        RUNNER_MAP["shell"] = object()  # type: ignore[assignment]  # infra kind, must be excluded
        assert active_agent_runner_kinds() == {"claude"}
        # restored to baseline by tests/conftest.py's autouse
        # `_isolate_runner_and_notifier_maps` fixture after this test.

    def test_empty_runner_map_yields_empty_set(self) -> None:
        from hivepilot.registry import RUNNER_MAP

        RUNNER_MAP.clear()
        assert active_agent_runner_kinds() == set()


# ---------------------------------------------------------------------------
# 4. sample / sample_skill default OFF, ON when flagged
# ---------------------------------------------------------------------------


class TestSampleAndSampleSkillDefaultOff:
    def test_sample_enabled_default_is_false(self) -> None:
        assert settings.sample_enabled is False

    def test_sample_skill_enabled_default_is_false(self) -> None:
        assert settings.sample_skill_enabled is False

    def test_sample_register_returns_empty_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sample = _load_plugin_module("sample")
        monkeypatch.setattr(settings, "sample_enabled", False, raising=False)
        assert sample.register() == {}

    def test_sample_register_returns_contribution_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sample = _load_plugin_module("sample")
        monkeypatch.setattr(settings, "sample_enabled", True, raising=False)
        result = sample.register()
        assert callable(result["before_step"])
        assert callable(result["after_step"])

    def test_sample_skill_register_returns_empty_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sample_skill = _load_plugin_module("sample_skill")
        monkeypatch.setattr(settings, "sample_skill_enabled", False, raising=False)
        assert sample_skill.register() == {}

    def test_sample_skill_register_returns_contribution_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sample_skill = _load_plugin_module("sample_skill")
        monkeypatch.setattr(settings, "sample_skill_enabled", True, raising=False)
        result = sample_skill.register()
        assert result["skills"][0]["name"] == "sample-skill"


# ---------------------------------------------------------------------------
# 5. Every plugins/*.py stem has a matching Settings.<stem>_enabled flag;
#    flag False unconditionally empties register() -- EXCEPT the two plugins
#    below, which gate LAZILY inside their hook/health functions instead of
#    in register() itself (pre-existing pattern, out of this sprint's file
#    boundaries -- plugins/headroom.py / plugins/mem0.py are not in
#    files_to_modify). register() for those two always returns its full
#    hooks+health dict; each hook/health call reads
#    settings.<stem>_enabled at CALL time and no-ops when False. That
#    call-time gating is covered by their own dedicated tests
#    (tests/test_headroom.py / tests/test_mem0.py), not here.
# ---------------------------------------------------------------------------

_LAZILY_GATED_STEMS = {"headroom", "mem0"}


class TestAllPluginStemsHaveEnabledFlag:
    @pytest.mark.parametrize("stem", _plugin_stems())
    def test_settings_has_enabled_flag(self, stem: str) -> None:
        flag_name = f"{stem}_enabled"
        assert hasattr(settings, flag_name), (
            f"Settings is missing {flag_name!r} for plugins/{stem}.py"
        )

    @pytest.mark.parametrize("stem", [s for s in _plugin_stems() if s not in _LAZILY_GATED_STEMS])
    def test_flag_false_makes_register_return_empty(
        self, stem: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        flag_name = f"{stem}_enabled"
        module = _load_plugin_module(stem)
        monkeypatch.setattr(settings, flag_name, False, raising=False)
        assert module.register() == {}

    def test_headroom_register_is_non_empty_but_before_step_no_ops_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        headroom = _load_plugin_module("headroom")
        monkeypatch.setattr(settings, "headroom_enabled", False, raising=False)
        hooks = headroom.register()
        assert hooks != {}  # lazily-gated: register() itself is unconditional

        payload = type("P", (), {"metadata": {"prompt": "some long original text"}})()
        hooks["before_step"](payload=payload)
        # No-op: the still-dormant flag means metadata is left untouched.
        assert payload.metadata == {"prompt": "some long original text"}

    def test_mem0_register_is_non_empty_regardless_of_flag(self) -> None:
        mem0 = _load_plugin_module("mem0")
        assert settings.mem0_enabled is False  # default -- opt-in, dormant
        hooks = mem0.register()
        assert hooks != {}  # lazily-gated: register() itself is unconditional


# ---------------------------------------------------------------------------
# 6. init/doctor warn-only regression
# ---------------------------------------------------------------------------


class TestMandatoryAgentsRegression:
    def test_mandatory_agents_constant_unchanged(self) -> None:
        assert MANDATORY_AGENTS == ("claude", "codex", "vibe")

    def test_check_mandatory_agents_never_raises_and_reports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        report = check_mandatory_agents()
        assert report.present == []
        assert report.any_ok is False
        assert report.claude_ok is False


# ---------------------------------------------------------------------------
# 7. AGENT_RUNNER_KINDS defined exactly once, imported elsewhere
# ---------------------------------------------------------------------------


class TestAgentRunnerKindsSingleSourceOfTruth:
    def test_agent_runner_kinds_content(self) -> None:
        expected = frozenset(
            {
                "claude",
                "codex",
                "cursor",
                "vibe",
                "openrouter",
                "gemini",
                "opencode",
                "ollama",
                "pi",
                "qwen-code",
                "kimi-cli",
                "antigravity",
            }
        )
        assert AGENT_RUNNER_KINDS == expected

    def test_defined_exactly_once_in_agent_checks(self) -> None:
        definition_needle = "AGENT_RUNNER_KINDS: frozenset[str] = frozenset("
        modules = {
            "agent_checks": REPO_ROOT / "hivepilot" / "services" / "agent_checks.py",
            "registry": REPO_ROOT / "hivepilot" / "registry.py",
            "orchestrator": REPO_ROOT / "hivepilot" / "orchestrator.py",
        }
        defining = [name for name, path in modules.items() if definition_needle in path.read_text()]
        assert defining == ["agent_checks"]

    def test_registry_imports_agent_runner_kinds(self) -> None:
        src = (REPO_ROOT / "hivepilot" / "registry.py").read_text()
        assert "from hivepilot.services.agent_checks import" in src
        assert "AGENT_RUNNER_KINDS" in src

    def test_orchestrator_references_agent_runner_kinds(self) -> None:
        src = (REPO_ROOT / "hivepilot" / "orchestrator.py").read_text()
        assert "AGENT_RUNNER_KINDS" in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
