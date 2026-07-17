"""Orchestrator step-execution wiring for plugin skills (Sprint 4).

Sprints 1-3 built the skill mechanism (SkillSpec registry, TaskStep.skills /
PipelineStage.skills fields, ClaudeRunner.apply_skill, the
apply_skill_if_supported dispatch helper, and `hivepilot validate` skill-ref
checks) but nothing CALLED apply_skill_if_supported from the orchestrator, so a
declared `skills:` list validated yet had zero runtime effect.

These tests prove Sprint 4: the per-step execution path in
`Orchestrator._execute_task` now resolves a step's declared skill names (its own
`TaskStep.skills` plus the enclosing `PipelineStage.skills` threaded in via
`stage_skills`) to registered `SkillSpec`s and enriches the runner payload
through the existing `apply_skill_if_supported` choke point BEFORE the runner
runs — while a step declaring NO skills stays byte-identical to before.

The orchestrator is built with the same mocked-loader pattern as
tests/test_worktree_isolation.py; `_capture_or_execute` is patched with a
recorder so we can inspect the exact payload the runner would have received.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskConfig, TaskStep
from hivepilot.plugins import SkillSpec
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import (
    _SKILL_SCRATCH_DIR_KEY,
    ClaudeRunner,
)

ORCH_MOD = "hivepilot.orchestrator"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skill(name: str = "demo") -> SkillSpec:
    return {
        "name": name,
        "description": "demo skill",
        "provider": "sample",
        "files": {"SKILL.md": "# Demo\nDo the thing."},
        "system_prompt": "Follow the demo skill.",
    }


def _project(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(path=tmp_path)


def _task(tmp_path: Path, *, skills: list[str] | None = None) -> TaskConfig:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("do the thing", encoding="utf-8")
    return TaskConfig(
        description="skill wiring task",
        steps=[
            TaskStep(
                name="s1",
                runner="claude",
                prompt_file=str(prompt_file),
                skills=skills,
            )
        ],
    )


def _make_orchestrator(tmp_path: Path, *, skill_registry: dict[str, SkillSpec]) -> Any:
    """Minimal Orchestrator with mocked loaders.

    `plugins.get_skill` is wired to *skill_registry* (returning None for an
    unknown name, exactly like the real `PluginManager.get_skill`), and
    `registry._definition_for` yields a real claude `RunnerDefinition` so the
    skill block resolves the genuine `ClaudeRunner` class via the real
    `resolve_runner_class` (RUNNER_MAP) — the mocked registry only stands in
    for definition lookup, not the class resolution the skill block performs.
    """
    from hivepilot.orchestrator import Orchestrator

    with (
        patch(f"{ORCH_MOD}.load_projects", return_value=MagicMock(projects={})),
        patch(f"{ORCH_MOD}.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch(f"{ORCH_MOD}.load_pipelines", return_value=MagicMock(pipelines={})),
        patch(f"{ORCH_MOD}.RunnerRegistry", return_value=MagicMock()),
        patch(f"{ORCH_MOD}.PluginManager", return_value=MagicMock()),
    ):
        orch = Orchestrator()

    orch.plugins.get_skill.side_effect = lambda n: skill_registry.get(n)  # type: ignore[attr-defined]
    orch.registry._definition_for.side_effect = lambda key: RunnerDefinition(  # type: ignore[attr-defined]
        name=key, kind="claude", command="claude"
    )
    return orch


def _run_single_step(orch: Any, project: ProjectConfig, task: TaskConfig) -> RunnerPayload:
    """Execute one step and return the exact payload the runner received.

    `_capture_or_execute` is replaced by a recorder so the enriched payload is
    captured without spawning a real subprocess. Because the recorder replaces
    the real runner call, ClaudeRunner.run()/capture()'s own scratch-dir
    `finally` cleanup never fires — the caller is responsible for removing any
    materialised scratch dir it asserts on.
    """
    seen: dict[str, RunnerPayload] = {}

    def _recorder(_self: Any, runner_key: str, payload: RunnerPayload) -> str:
        seen["payload"] = payload
        return "ok"

    with (
        patch(f"{ORCH_MOD}.settings") as mock_settings,
        patch.object(
            __import__(ORCH_MOD, fromlist=["Orchestrator"]).Orchestrator,
            "_capture_or_execute",
            _recorder,
        ),
    ):
        mock_settings.worktree_isolation = False
        mock_settings.stage_cache_enabled = False
        mock_settings.dev_batch_size = 0
        orch._execute_task(
            project=project,
            task_name="skill-task",
            task=task,
            extra_prompt=None,
            auto_git=False,
            simulate=False,
            dry_run=True,
        )

    assert "payload" in seen, "runner was never invoked for the step"
    return seen["payload"]


# ---------------------------------------------------------------------------
# Wiring: a step declaring skills reaches the runner enriched
# ---------------------------------------------------------------------------


def test_step_skill_reaches_claude_runner(tmp_path: Path) -> None:
    """A step with `skills: [demo]` (registered) enriches the payload: the
    scratch dir is materialised with the skill's files and the built argv
    carries `--add-dir` — proving apply_skill actually ran BEFORE the runner."""
    orch = _make_orchestrator(tmp_path, skill_registry={"demo": _skill()})
    project = _project(tmp_path)
    task = _task(tmp_path, skills=["demo"])

    payload = _run_single_step(orch, project, task)

    scratch = payload.metadata.get(_SKILL_SCRATCH_DIR_KEY)
    assert scratch is not None, "skill scratch dir must be stashed on the payload"
    skill_file = Path(scratch) / ".claude" / "skills" / "demo" / "SKILL.md"
    assert skill_file.read_text(encoding="utf-8") == "# Demo\nDo the thing."

    # The runner's own invocation now reflects the skill (--add-dir <scratch>).
    runner = ClaudeRunner(
        RunnerDefinition(name="claude", kind="claude", command="claude"), settings
    )
    args, _ = runner._build_invocation(payload)
    assert "--add-dir" in args
    assert args[args.index("--add-dir") + 1] == scratch
    assert "--append-system-prompt" in args

    shutil.rmtree(scratch, ignore_errors=True)


def test_stage_level_skill_is_applied(tmp_path: Path) -> None:
    """A skill declared on the enclosing PipelineStage (threaded in as
    `stage_skills`) is applied even when the step itself declares none —
    proving the stage-level union path, not only the step's own list."""
    orch = _make_orchestrator(tmp_path, skill_registry={"demo": _skill()})
    project = _project(tmp_path)
    task = _task(tmp_path, skills=None)  # step declares no skills of its own

    seen: dict[str, RunnerPayload] = {}

    def _recorder(_self: Any, runner_key: str, payload: RunnerPayload) -> str:
        seen["payload"] = payload
        return "ok"

    with (
        patch(f"{ORCH_MOD}.settings") as mock_settings,
        patch.object(
            __import__(ORCH_MOD, fromlist=["Orchestrator"]).Orchestrator,
            "_capture_or_execute",
            _recorder,
        ),
    ):
        mock_settings.worktree_isolation = False
        mock_settings.stage_cache_enabled = False
        mock_settings.dev_batch_size = 0
        orch._execute_task(
            project=project,
            task_name="skill-task",
            task=task,
            extra_prompt=None,
            auto_git=False,
            simulate=False,
            dry_run=True,
            stage_skills=["demo"],  # stage-level skill
        )

    scratch = seen["payload"].metadata.get(_SKILL_SCRATCH_DIR_KEY)
    assert scratch is not None, "stage-level skill must enrich the payload"
    assert (Path(scratch) / ".claude" / "skills" / "demo" / "SKILL.md").exists()

    shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# Regression: a step with NO skills is byte-identical to before this wiring
# ---------------------------------------------------------------------------


def test_step_without_skills_is_unchanged(tmp_path: Path) -> None:
    """A step declaring no skills never materialises a scratch dir and never
    needs apply_skill — the default path is byte-identical to before Sprint 4.
    `get_skill` is not even consulted."""
    orch = _make_orchestrator(tmp_path, skill_registry={"demo": _skill()})
    project = _project(tmp_path)
    task = _task(tmp_path, skills=None)

    payload = _run_single_step(orch, project, task)

    assert _SKILL_SCRATCH_DIR_KEY not in payload.metadata
    orch.plugins.get_skill.assert_not_called()

    # And the built argv carries no skill flags.
    runner = ClaudeRunner(
        RunnerDefinition(name="claude", kind="claude", command="claude"), settings
    )
    args, _ = runner._build_invocation(payload)
    assert "--add-dir" not in args
    assert "--append-system-prompt" not in args


def test_unregistered_skill_at_runtime_warns_and_does_not_crash(tmp_path: Path) -> None:
    """Defensive: if `get_skill` returns None at runtime (config validation
    should have caught it, but a plugin-manager mismatch is possible), the run
    logs a warning, skips that name, and completes — never crashes. With no
    resolvable skill, no scratch dir is materialised."""
    orch = _make_orchestrator(tmp_path, skill_registry={})  # nothing registered
    project = _project(tmp_path)
    task = _task(tmp_path, skills=["ghost"])

    with patch(f"{ORCH_MOD}.logger.warning") as mock_warn:
        payload = _run_single_step(orch, project, task)

    assert _SKILL_SCRATCH_DIR_KEY not in payload.metadata
    # get_skill was consulted for the declared (but unregistered) name.
    orch.plugins.get_skill.assert_called_with("ghost")
    # A warning was emitted for the missing skill.
    assert any(
        call.args and call.args[0] == "step.skill_not_found" for call in mock_warn.call_args_list
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
