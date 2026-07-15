"""Tests for hivepilot.services.lint_service — runner-kind linting.

Covers the fix for the "api" runner-kind orphan (roadmap Phase 26a):
`_lint_task` used to check `step.runner` against a hardcoded, drifted
`KNOWN_RUNNERS` set (which advertised `"api"` as valid even though it has
no `RUNNER_MAP` entry, and was missing real builtins like `cursor`/`vibe`).
It now checks against the live registry (`RunnerRegistry.known_kinds()`),
so it flags orphan/unregistered kinds and accepts every actually-registered
kind, including plugin-contributed ones.
"""

from __future__ import annotations

from hivepilot.models import TaskConfig, TaskStep
from hivepilot.registry import RUNNER_MAP, RunnerRegistry
from hivepilot.services.lint_service import _lint_task


def test_lint_task_flags_unregistered_api_runner() -> None:
    """`runner: api` has no RUNNER_MAP entry, so lint must flag it as unknown —
    not silently accept it (the pre-fix behavior)."""
    task = TaskConfig(
        description="d",
        steps=[TaskStep(name="s1", runner="api")],
    )
    errors = _lint_task("my-task", task)
    assert len(errors) == 1
    assert "api" in errors[0]
    assert "unknown runner" in errors[0].lower()


def test_lint_task_accepts_every_registered_runner_kind() -> None:
    """Every kind actually present in RUNNER_MAP (the real, live registry)
    lints clean — no false positives for builtins."""
    for kind in sorted(RUNNER_MAP):
        task = TaskConfig(description="d", steps=[TaskStep(name="s1", runner=kind)])
        assert _lint_task("t", task) == [], f"builtin runner kind {kind!r} should lint clean"


def test_lint_task_accepts_plugin_registered_kind() -> None:
    """A kind registered at runtime (e.g. by a plugin) is treated as valid,
    since lint now checks the live registry instead of a static set."""

    class _DummyRunner:
        def __init__(self, definition, settings) -> None:
            pass

        def run(self, payload) -> None:
            raise NotImplementedError

    RunnerRegistry.register("dummy-lint-kind", _DummyRunner)
    try:
        task = TaskConfig(description="d", steps=[TaskStep(name="s1", runner="dummy-lint-kind")])
        assert _lint_task("t", task) == []
    finally:
        RUNNER_MAP.pop("dummy-lint-kind", None)


def test_lint_task_still_allows_runner_ref_without_direct_kind_match() -> None:
    """A step whose `runner` isn't a registered kind is still NOT flagged when
    `runner_ref` is set (points at a named runner definition instead of a
    kind directly) — unchanged pre-existing behavior."""
    task = TaskConfig(
        description="d",
        steps=[TaskStep(name="s1", runner="totally-made-up", runner_ref="custom-runner-def")],
    )
    assert _lint_task("t", task) == []
