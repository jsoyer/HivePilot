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

from pathlib import Path

import pytest

from hivepilot.models import TaskConfig, TaskStep
from hivepilot.registry import RUNNER_MAP
from hivepilot.services.lint_service import _lint_task, lint_configuration


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


def test_lint_configuration_accepts_real_on_disk_plugin_runner_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behavior-true regression test: `lint_configuration()` must discover
    plugins itself (via a real `PluginManager()` construction) BEFORE
    validating runner kinds — a kind contributed by a genuine, on-disk
    local-file plugin (discovered through the real `plugins/` directory
    scan, not a manual `RunnerRegistry.register()` shortcut) must lint
    clean, exactly as a fresh `hivepilot lint` CLI invocation would see it."""
    from hivepilot.config import settings

    xdg_empty = tmp_path / "xdg-empty"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_empty))
    monkeypatch.setattr(settings, "base_dir", tmp_path, raising=False)
    monkeypatch.setattr(settings, "config_repo", None, raising=False)

    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "lint_fixture.py").write_text(
        """
class LintFixtureRunner:
    def __init__(self, definition, settings):
        pass

    def run(self, payload):
        return None


def register():
    return {"runners": {"lint-fixture-kind": LintFixtureRunner}}
""",
        encoding="utf-8",
    )

    (tmp_path / "projects.yaml").write_text("projects: {}\n", encoding="utf-8")
    (tmp_path / "pipelines.yaml").write_text("pipelines: {}\n", encoding="utf-8")
    (tmp_path / "tasks.yaml").write_text(
        "runners: {}\n"
        "tasks:\n"
        "  plugin-task:\n"
        "    description: uses a plugin runner\n"
        "    steps:\n"
        "      - name: s1\n"
        "        runner: lint-fixture-kind\n",
        encoding="utf-8",
    )

    errors = lint_configuration()

    assert errors == [], f"plugin-contributed runner kind should lint clean, got: {errors}"
    # Sanity: the plugin was genuinely discovered (not pre-registered by the test).
    assert "lint-fixture-kind" in RUNNER_MAP


def test_lint_task_still_allows_runner_ref_without_direct_kind_match() -> None:
    """A step whose `runner` isn't a registered kind is still NOT flagged when
    `runner_ref` is set (points at a named runner definition instead of a
    kind directly) — unchanged pre-existing behavior."""
    task = TaskConfig(
        description="d",
        steps=[TaskStep(name="s1", runner="totally-made-up", runner_ref="custom-runner-def")],
    )
    assert _lint_task("t", task) == []
