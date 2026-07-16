from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

QUOTA_MSG = "claude exited 1: You've hit your session limit · resets 9:40pm (Europe/Paris)"
CODE_FAIL_MSG = "claude exited 1: Syntax error in generated code"


def _make_orchestrator_with_mocked_registry():
    """Build a minimal Orchestrator instance with a mock registry."""
    from hivepilot.orchestrator import Orchestrator

    orch = object.__new__(Orchestrator)
    orch.registry = MagicMock()
    orch.plugins = MagicMock()
    orch.plugins.run_hook = MagicMock()
    orch.tasks = MagicMock()
    orch.projects = MagicMock()
    orch.pipelines = MagicMock()
    return orch


def _make_task_config(role="developer", allow_failure=False):
    from hivepilot.models import GitActions, TaskConfig, TaskStep

    step = TaskStep(name="dev-step", runner="claude", allow_failure=allow_failure)
    task = TaskConfig(
        description="dev task",
        steps=[step],
        role=role,
        git=GitActions(),
    )
    return task, step


def _make_project_config():
    from pathlib import Path

    from hivepilot.models import ProjectConfig

    return ProjectConfig(path=Path("/tmp/fake-project"))


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    """Ensure fallback runners are configured."""
    from hivepilot import config

    monkeypatch.setattr(config.settings, "dev_fallback_runners", ["codex", "opencode"])
    monkeypatch.setattr(config.settings, "stage_cache_enabled", False)
    monkeypatch.setattr(config.settings, "worktree_isolation", False)


def test_developer_quota_error_falls_back_to_codex():
    """Developer step whose primary (claude) raises quota → fallback runner (codex) used."""
    orch = _make_orchestrator_with_mocked_registry()
    task, step = _make_task_config(role="developer")
    project = _make_project_config()

    call_count = {"n": 0}

    def capture_definition_side_effect(runner_def, payload):
        call_count["n"] += 1
        if runner_def.kind == "claude":
            raise RuntimeError(QUOTA_MSG)
        if runner_def.kind == "codex":
            return "codex output"
        raise RuntimeError("unexpected runner")

    orch.registry.capture_definition.side_effect = capture_definition_side_effect

    with (
        patch("hivepilot.roles.get_role") as mock_get_role,
        patch("hivepilot.roles.resolve_runner", return_value=("claude", "claude-sonnet-4-6")),
        patch("hivepilot.roles.resolve_host", return_value=None),
        patch("hivepilot.services.state_service.record_step"),
    ):
        mock_role = MagicMock()
        mock_role.models = []
        mock_role.permission_mode = None
        mock_get_role.return_value = mock_role

        result = orch._execute_task(
            project=project,
            task_name="dev-task",
            task=task,
            extra_prompt=None,
            auto_git=False,
            simulate=False,
            dry_run=True,
        )

    assert result == "codex output"
    assert call_count["n"] == 2  # claude tried, codex succeeded


def test_non_quota_error_does_not_fallback():
    """A non-quota code failure surfaces immediately — no fallback attempted."""
    orch = _make_orchestrator_with_mocked_registry()
    task, step = _make_task_config(role="developer")
    project = _make_project_config()

    orch.registry.capture_definition.side_effect = RuntimeError(CODE_FAIL_MSG)

    with (
        patch("hivepilot.roles.get_role") as mock_get_role,
        patch("hivepilot.roles.resolve_runner", return_value=("claude", "claude-sonnet-4-6")),
        patch("hivepilot.roles.resolve_host", return_value=None),
        patch("hivepilot.services.state_service.record_step"),
    ):
        mock_role = MagicMock()
        mock_role.models = []
        mock_role.permission_mode = None
        mock_get_role.return_value = mock_role

        with pytest.raises(RuntimeError, match="Syntax error"):
            orch._execute_task(
                project=project,
                task_name="dev-task",
                task=task,
                extra_prompt=None,
                auto_git=False,
                simulate=False,
                dry_run=True,
            )

    # Only called once — no fallback
    assert orch.registry.capture_definition.call_count == 1


def test_quota_deferred_step_with_allow_failure_propagates_not_swallowed(monkeypatch):
    """CATASTROPHIC-RISK regression guard: a developer step with
    `allow_failure=True` whose runner exhausts every fallback must still
    raise `QuotaDeferredError` out of `_execute_task` — it must NOT be
    caught by the generic `except Exception` / `record_step(..., "failed",
    ...)` / `if step.allow_failure: continue` path and silently swallowed.
    A quota deferral is an interrupt for the pipeline-level deferred
    handler (`_run_task_body`'s `except QuotaDeferredError`, reached via
    `run_task`), not a step failure — losing it here means the whole task
    is silently dropped instead of retried later.
    """
    from hivepilot import config
    from hivepilot.services.quota import QuotaDeferredError

    # No fallback runners configured — the developer-role fallback loop
    # exhausts immediately and must raise QuotaDeferredError.
    monkeypatch.setattr(config.settings, "dev_fallback_runners", [])

    orch = _make_orchestrator_with_mocked_registry()
    task, step = _make_task_config(role="developer", allow_failure=True)
    project = _make_project_config()

    orch.registry.capture_definition.side_effect = RuntimeError(QUOTA_MSG)

    with (
        patch("hivepilot.roles.get_role") as mock_get_role,
        patch("hivepilot.roles.resolve_runner", return_value=("claude", "claude-sonnet-4-6")),
        patch("hivepilot.roles.resolve_host", return_value=None),
        patch("hivepilot.services.state_service.record_step") as mock_record_step,
    ):
        mock_role = MagicMock()
        mock_role.models = []
        mock_role.permission_mode = None
        mock_get_role.return_value = mock_role

        with pytest.raises(QuotaDeferredError):
            orch._execute_task(
                project=project,
                task_name="dev-task",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                simulate=False,
                dry_run=True,
            )

    # The quota deferral must never be recorded as a "failed" step — that
    # would be the `allow_failure=True` generic-exception path silently
    # swallowing a defer-and-retry-later signal.
    for _call in mock_record_step.call_args_list:
        assert _call.args[2] != "failed", f"quota-defer mis-recorded as failed: {_call}"


def test_non_developer_role_does_not_fallback():
    """A quota error on a non-developer role raises immediately (no fallback)."""
    orch = _make_orchestrator_with_mocked_registry()
    task, step = _make_task_config(role="architect")
    project = _make_project_config()

    orch.registry.capture_definition.side_effect = RuntimeError(QUOTA_MSG)

    with (
        patch("hivepilot.roles.get_role") as mock_get_role,
        patch("hivepilot.roles.resolve_runner", return_value=("claude", "claude-sonnet-4-6")),
        patch("hivepilot.roles.resolve_host", return_value=None),
        patch("hivepilot.services.state_service.record_step"),
    ):
        mock_role = MagicMock()
        mock_role.models = []
        mock_role.permission_mode = None
        mock_get_role.return_value = mock_role

        with pytest.raises(RuntimeError, match="session limit"):
            orch._execute_task(
                project=project,
                task_name="dev-task",
                task=task,
                extra_prompt=None,
                auto_git=False,
                simulate=False,
                dry_run=True,
            )

    assert orch.registry.capture_definition.call_count == 1
