"""Tests for the opened runner registry (Plugin System PRD, Sprint 1).

Covers `RunnerRegistry.register()` semantics (fresh registration, idempotent
same-class re-registration, collision detection, explicit override) plus the
`_parse_brain` fix that replaced the now-defunct `get_args(RunnerKind)` check
with a live-registry union check.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from hivepilot.orchestrator import _parse_brain
from hivepilot.registry import RUNNER_MAP, RunnerKindCollisionError, RunnerRegistry


# Plain concrete classes that structurally satisfy the BaseRunner Protocol
# (do NOT subclass it — subclassing a Protocol makes the class abstract to
# mypy, which then rejects passing it where a concrete type[BaseRunner] is
# expected). These are only ever registered, never instantiated, here.
class _DummyRunnerA:
    # supported_modes is part of the BaseRunner Protocol (Sprint 1: mode
    # cli|api), so a class that structurally satisfies it must declare it too.
    supported_modes: ClassVar[frozenset[str]] = frozenset({"cli"})

    def __init__(self, definition, settings) -> None:
        self.definition = definition
        self.settings = settings

    def run(self, payload) -> None:
        raise NotImplementedError

    def capture(self, payload) -> str:
        raise NotImplementedError


class _DummyRunnerB:
    supported_modes: ClassVar[frozenset[str]] = frozenset({"cli"})

    def __init__(self, definition, settings) -> None:
        self.definition = definition
        self.settings = settings

    def run(self, payload) -> None:
        raise NotImplementedError

    def capture(self, payload) -> str:
        raise NotImplementedError


def test_register_fresh_kind_resolves_via_runner_map() -> None:
    RunnerRegistry.register("dummy-a", _DummyRunnerA)
    try:
        assert RUNNER_MAP["dummy-a"] is _DummyRunnerA
    finally:
        del RUNNER_MAP["dummy-a"]


def test_register_same_kind_same_class_is_noop() -> None:
    RunnerRegistry.register("dummy-a", _DummyRunnerA)
    try:
        RunnerRegistry.register("dummy-a", _DummyRunnerA)  # should not raise
        assert RUNNER_MAP["dummy-a"] is _DummyRunnerA
    finally:
        del RUNNER_MAP["dummy-a"]


def test_register_collision_raises_without_override() -> None:
    RunnerRegistry.register("dummy-a", _DummyRunnerA)
    try:
        with pytest.raises(RunnerKindCollisionError):
            RunnerRegistry.register("dummy-a", _DummyRunnerB)
    finally:
        del RUNNER_MAP["dummy-a"]


def test_register_override_true_replaces_class() -> None:
    RunnerRegistry.register("dummy-a", _DummyRunnerA)
    try:
        RunnerRegistry.register("dummy-a", _DummyRunnerB, override=True)
        assert RUNNER_MAP["dummy-a"] is _DummyRunnerB
    finally:
        del RUNNER_MAP["dummy-a"]


def test_known_kinds_returns_frozenset_with_11_builtins() -> None:
    builtins = {
        "claude",
        "shell",
        "langchain",
        "internal",
        "codex",
        "gemini",
        "opencode",
        "ollama",
        "container",
        "cursor",
        "vibe",
    }
    known = RunnerRegistry.known_kinds()
    assert isinstance(known, frozenset)
    assert builtins <= known


def test_parse_brain_claude_prefix_still_resolves() -> None:
    assert _parse_brain("claude:claude-sonnet-4-6", "shell") == (
        "claude",
        "claude-sonnet-4-6",
    )


def test_parse_brain_api_prefix_no_longer_recognised() -> None:
    """Roadmap Phase 26a: `_parse_brain` now checks the live registry
    (`RUNNER_MAP`) instead of the static `KNOWN_RUNNER_KINDS` tuple, so the
    historical `"api"` orphan (no RUNNER_MAP entry) is no longer treated as
    a recognised runner prefix — it falls back to the default runner, like
    any other unrecognised prefix."""
    assert _parse_brain("api:some-model", "shell") == ("shell", "api:some-model")


def test_parse_brain_unrecognised_prefix_falls_back_to_default() -> None:
    assert _parse_brain("opencode-go/kimi", "shell") == ("shell", "opencode-go/kimi")
