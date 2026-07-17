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
from hivepilot.runners.base import RunnerModeUnsupportedError, RunnerPayload
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


# ---------------------------------------------------------------------------
# Regression: skill re-applied to a quota-substituted fallback runner
# ---------------------------------------------------------------------------


def test_skill_reapplied_to_quota_fallback_runner(tmp_path: Path, monkeypatch) -> None:
    """LOW correctness bug regression: a developer-role step with an attached
    skill whose PRIMARY runner (claude) raises a quota error must have the
    skill RE-APPLIED to the FALLBACK runner (a different kind, e.g. codex)
    before it runs -- not silently dropped because `payload` was only ever
    materialised via `apply_skill_if_supported` for the original runner's
    kind.

    A fake runner class (spy) stands in for BOTH `claude` and `codex` via a
    patched `resolve_runner_class`, so `apply_skill` is observable per-kind
    without depending on ClaudeRunner's real scratch-dir mechanics. Each spy
    invocation stamps `payload.metadata["skill_applied_by_kind"]` with its
    own runner kind -- proving `apply_skill` ran a SECOND time, for `codex`,
    from a clean pre-skill payload (not the claude-materialised one).
    """
    from dataclasses import replace

    from hivepilot import config
    from hivepilot.models import GitActions

    monkeypatch.setattr(config.settings, "dev_fallback_runners", ["codex"])
    monkeypatch.setattr(config.settings, "stage_cache_enabled", False)
    monkeypatch.setattr(config.settings, "worktree_isolation", False)

    apply_calls: list[str] = []

    class _SpyRunner:
        def __init__(self, definition: RunnerDefinition, settings: Any) -> None:
            self.definition = definition
            self.settings = settings

        def apply_skill(self, payload: RunnerPayload, skills: list[SkillSpec]) -> RunnerPayload:
            apply_calls.append(self.definition.kind)
            new_meta = dict(payload.metadata)
            new_meta["skill_applied_by_kind"] = self.definition.kind
            return replace(payload, metadata=new_meta)

    monkeypatch.setattr("hivepilot.registry.resolve_runner_class", lambda kind: _SpyRunner)

    orch = _make_orchestrator(tmp_path, skill_registry={"demo": _skill()})
    project = _project(tmp_path)
    task = TaskConfig(
        description="skill wiring task",
        role="developer",
        steps=[TaskStep(name="s1", runner="claude", skills=["demo"])],
        git=GitActions(),
    )

    captured: dict[str, Any] = {}

    def capture_definition_side_effect(runner_def: RunnerDefinition, payload: RunnerPayload) -> str:
        if runner_def.kind == "claude":
            # The original runner's own apply_skill must have already run --
            # unaffected by this bug/fix, but a useful sanity check.
            assert payload.metadata.get("skill_applied_by_kind") == "claude"
            raise RuntimeError(
                "claude exited 1: You've hit your session limit · resets 9:40pm (Europe/Paris)"
            )
        if runner_def.kind == "codex":
            captured["fallback_payload"] = payload
            return "codex output"
        raise RuntimeError(f"unexpected runner kind: {runner_def.kind}")

    orch.registry.capture_definition.side_effect = capture_definition_side_effect

    with (
        patch("hivepilot.roles.get_role") as mock_get_role,
        patch("hivepilot.roles.resolve_runner", return_value=("claude", "claude-sonnet-4-6", None)),
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
    assert apply_calls == ["claude", "codex"], (
        f"apply_skill must be invoked once per runner kind actually attempted "
        f"(claude, then the codex fallback) -- got {apply_calls}"
    )
    assert "fallback_payload" in captured, "fallback runner (codex) was never invoked"
    assert captured["fallback_payload"].metadata.get("skill_applied_by_kind") == "codex", (
        "skill was not re-applied to the fallback runner -- it is inert on "
        f"the quota-substituted runner (payload metadata: "
        f"{captured['fallback_payload'].metadata})"
    )


# ---------------------------------------------------------------------------
# Regression: mode is re-resolved/validated/injected per runner attempt too,
# not just skills (the gap left by the skill-only fix above)
# ---------------------------------------------------------------------------


def _mode_aware_spy_factory(
    apply_calls: list[str], supported_modes_by_kind: dict[str, frozenset[str]]
) -> Any:
    """Build a `resolve_runner_class` stand-in whose returned spy class's
    `supported_modes` depends on *kind* (mirroring the real per-kind
    difference between e.g. claude/codex (`{"cli", "api"}`) and a
    cli-only runner (`{"cli"}`)), and whose `apply_skill` stamps
    `skill_applied_by_kind` -- same spy contract as
    `test_skill_reapplied_to_quota_fallback_runner` above, extended with a
    realistic `supported_modes` per kind so mode-validation is exercised."""
    from dataclasses import replace

    def _resolve_runner_class(kind: str) -> Any:
        _modes = supported_modes_by_kind.get(kind, frozenset({"cli"}))

        class _Spy:
            supported_modes = _modes

            def __init__(self, definition: RunnerDefinition, settings: Any) -> None:
                self.definition = definition
                self.settings = settings

            def apply_skill(self, payload: RunnerPayload, skills: list[SkillSpec]) -> RunnerPayload:
                apply_calls.append(self.definition.kind)
                new_meta = dict(payload.metadata)
                new_meta["skill_applied_by_kind"] = self.definition.kind
                return replace(payload, metadata=new_meta)

        return _Spy

    return _resolve_runner_class


def test_mode_and_skill_reapplied_to_quota_fallback_runner(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A developer-role, mode:api step with an attached skill whose PRIMARY
    runner (claude) hits a quota error must have BOTH the mode metadata AND
    the skill RE-APPLIED to the FALLBACK runner (codex, which also supports
    api mode) -- not just the skill (the 914e35a fix) while the mode
    injection silently gets dropped because it was only ever computed
    against the ORIGINAL runner and snapshotted away before the fallback
    re-apply.

    Confirmed to FAIL against the 914e35a code: `_pre_skill_payload` there is
    snapshotted BEFORE the mode-injection block runs, so the fallback re-apply
    (`apply_skill_if_supported(_fb_runner, _pre_skill_payload, ...)`) starts
    from a payload whose `step.metadata` never got `"mode": "api"` written --
    the fallback payload's `step.metadata.get("mode")` is `None`, not `"api"`.
    """
    from hivepilot import config
    from hivepilot.models import GitActions

    monkeypatch.setattr(config.settings, "dev_fallback_runners", ["codex"])
    monkeypatch.setattr(config.settings, "stage_cache_enabled", False)
    monkeypatch.setattr(config.settings, "worktree_isolation", False)

    apply_calls: list[str] = []
    _resolve_runner_class = _mode_aware_spy_factory(
        apply_calls,
        {
            "claude": frozenset({"cli", "api"}),
            "codex": frozenset({"cli", "api"}),
        },
    )
    monkeypatch.setattr("hivepilot.registry.resolve_runner_class", _resolve_runner_class)

    orch = _make_orchestrator(tmp_path, skill_registry={"demo": _skill()})
    project = _project(tmp_path)
    # Mode is deliberately NOT baked into the step's own metadata -- it comes
    # from the pipeline/stage-resolved `mode` param (`resolve_mode`'s sibling
    # threading), exactly like a real stage-level `mode: api` would reach
    # `_resolve_effective_mode`. This is what forces the orchestrator to
    # actually INJECT "mode" into the step copy (rather than finding it
    # already present), which is the code path the pre-fix bug drops on the
    # quota-fallback re-apply.
    task = TaskConfig(
        description="mode+skill wiring task",
        role="developer",
        steps=[
            TaskStep(name="s1", runner="claude", skills=["demo"]),
        ],
        git=GitActions(),
    )

    captured: dict[str, Any] = {}

    def capture_definition_side_effect(runner_def: RunnerDefinition, payload: RunnerPayload) -> str:
        if runner_def.kind == "claude":
            assert payload.step.metadata.get("mode") == "api", (
                "original runner's payload must carry mode:api before dispatch"
            )
            assert payload.metadata.get("skill_applied_by_kind") == "claude"
            raise RuntimeError(
                "claude exited 1: You've hit your session limit · resets 9:40pm (Europe/Paris)"
            )
        if runner_def.kind == "codex":
            captured["fallback_payload"] = payload
            return "codex output"
        raise RuntimeError(f"unexpected runner kind: {runner_def.kind}")

    orch.registry.capture_definition.side_effect = capture_definition_side_effect

    with (
        patch("hivepilot.roles.get_role") as mock_get_role,
        patch("hivepilot.roles.resolve_runner", return_value=("claude", "claude-sonnet-4-6", None)),
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
            mode="api",
        )

    assert result == "codex output"
    assert apply_calls == ["claude", "codex"], (
        f"apply_skill must be invoked once per runner kind actually attempted -- got {apply_calls}"
    )
    assert "fallback_payload" in captured, "fallback runner (codex) was never invoked"
    _fb_payload = captured["fallback_payload"]
    assert _fb_payload.step.metadata.get("mode") == "api", (
        "mode metadata was dropped on the quota-fallback runner -- the "
        f"fallback payload's step.metadata is {_fb_payload.step.metadata!r}"
    )
    assert _fb_payload.metadata.get("skill_applied_by_kind") == "codex", (
        "skill was not re-applied to the fallback runner alongside the mode "
        f"(payload metadata: {_fb_payload.metadata})"
    )


def test_mode_api_step_fails_closed_when_fallback_is_cli_only(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A developer-role, mode:api step whose PRIMARY runner (claude) hits a
    quota error, and whose quota-fallback runner is CLI-ONLY, must fail
    closed with `RunnerModeUnsupportedError` when the fallback is prepared --
    NEVER silently dispatch the fallback in cli mode, and never be swallowed
    as "just another reason to try the next fallback" (the mode-validation
    failure is not a quota error)."""
    from hivepilot import config
    from hivepilot.models import GitActions

    monkeypatch.setattr(config.settings, "dev_fallback_runners", ["cursor"])
    monkeypatch.setattr(config.settings, "stage_cache_enabled", False)
    monkeypatch.setattr(config.settings, "worktree_isolation", False)

    apply_calls: list[str] = []
    _resolve_runner_class = _mode_aware_spy_factory(
        apply_calls,
        {
            "claude": frozenset({"cli", "api"}),
            "cursor": frozenset({"cli"}),  # cli-only -- no api support
        },
    )
    monkeypatch.setattr("hivepilot.registry.resolve_runner_class", _resolve_runner_class)

    orch = _make_orchestrator(tmp_path, skill_registry={})
    project = _project(tmp_path)
    task = TaskConfig(
        description="mode fail-closed task",
        role="developer",
        steps=[TaskStep(name="s1", runner="claude", metadata={"mode": "api"})],
        git=GitActions(),
    )

    def capture_definition_side_effect(runner_def: RunnerDefinition, payload: RunnerPayload) -> str:
        if runner_def.kind == "claude":
            raise RuntimeError(
                "claude exited 1: You've hit your session limit · resets 9:40pm (Europe/Paris)"
            )
        raise AssertionError(
            f"cli-only fallback runner {runner_def.kind!r} must never be dispatched "
            "for a mode:api step -- mode validation must fail closed before "
            "capture_definition is ever called for it"
        )

    orch.registry.capture_definition.side_effect = capture_definition_side_effect

    with (
        patch("hivepilot.roles.get_role") as mock_get_role,
        patch("hivepilot.roles.resolve_runner", return_value=("claude", "claude-sonnet-4-6", None)),
        patch("hivepilot.roles.resolve_host", return_value=None),
        patch("hivepilot.services.state_service.record_step"),
    ):
        mock_role = MagicMock()
        mock_role.models = []
        mock_role.permission_mode = None
        mock_get_role.return_value = mock_role

        with pytest.raises(RunnerModeUnsupportedError, match="cursor"):
            orch._execute_task(
                project=project,
                task_name="dev-task",
                task=task,
                extra_prompt=None,
                auto_git=False,
                simulate=False,
                dry_run=True,
            )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
