"""Sprint 1 (runner-defaults-plugins-mode PRD): pipeline `mode: cli|api`.

Covers:
- PipelineConfig/PipelineStage accept the new `mode` field; invalid values are
  rejected by Pydantic.
- `resolve_mode(pipeline, stage)` precedence: stage > pipeline > "cli" default.
- `supported_modes` capability contract: BaseRunner default {"cli"}, agent
  runners (claude / prompt-cli) advertise {"cli","api"}, non-agent runners
  advertise {"cli"} only, and EVERY class in RUNNER_MAP exposes the attribute.
- `validate_runner_mode` fails closed with a clear message on an unsupported
  (kind, mode) combination.
- Orchestrator propagation: the resolved mode is written into the step-metadata
  channel runners read, and a `mode:api` step on a non-agent runner fails
  validation BEFORE any subprocess is spawned.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.models import (
    PipelineConfig,
    PipelineStage,
    ProjectConfig,
    RunnerDefinition,
    TaskConfig,
    TaskStep,
    resolve_mode,
)
from hivepilot.runners.base import (
    BaseRunner,
    RunnerModeUnsupportedError,
    RunnerPayload,
    validate_runner_mode,
)

# ── models: mode field + validation ──────────────────────────────────────────


def test_pipeline_config_mode_defaults_to_cli() -> None:
    pipeline = PipelineConfig(description="d")
    assert pipeline.mode == "cli"


def test_pipeline_stage_mode_defaults_to_none() -> None:
    stage = PipelineStage(name="s", task="t")
    assert stage.mode is None


def test_pipeline_config_accepts_api_mode() -> None:
    pipeline = PipelineConfig(description="d", mode="api")
    assert pipeline.mode == "api"


def test_pipeline_stage_accepts_explicit_modes() -> None:
    assert PipelineStage(name="s", task="t", mode="api").mode == "api"
    assert PipelineStage(name="s", task="t", mode="cli").mode == "cli"


def test_pipeline_config_rejects_invalid_mode() -> None:
    with pytest.raises(ValueError):
        PipelineConfig(description="d", mode="grpc")  # type: ignore[arg-type]


def test_pipeline_stage_rejects_invalid_mode() -> None:
    with pytest.raises(ValueError):
        PipelineStage(name="s", task="t", mode="grpc")  # type: ignore[arg-type]


# ── resolve_mode precedence ───────────────────────────────────────────────────


def test_resolve_mode_defaults_to_cli() -> None:
    pipeline = PipelineConfig(description="d")
    stage = PipelineStage(name="s", task="t")
    assert resolve_mode(pipeline, stage) == "cli"


def test_resolve_mode_pipeline_over_default() -> None:
    pipeline = PipelineConfig(description="d", mode="api")
    stage = PipelineStage(name="s", task="t")
    assert resolve_mode(pipeline, stage) == "api"


def test_resolve_mode_stage_over_pipeline() -> None:
    pipeline = PipelineConfig(description="d", mode="api")
    stage = PipelineStage(name="s", task="t", mode="cli")
    assert resolve_mode(pipeline, stage) == "cli"

    pipeline2 = PipelineConfig(description="d", mode="cli")
    stage2 = PipelineStage(name="s", task="t", mode="api")
    assert resolve_mode(pipeline2, stage2) == "api"


# ── supported_modes capability contract ───────────────────────────────────────


def test_base_runner_default_supported_modes_is_cli_only() -> None:
    assert BaseRunner.supported_modes == frozenset({"cli"})


def test_agent_runners_support_cli_and_api() -> None:
    from hivepilot.runners.claude_runner import ClaudeRunner
    from hivepilot.runners.prompt_cli_runner import (
        CodexRunner,
        GeminiRunner,
        OllamaRunner,
        OpenCodeRunner,
        PromptCliRunner,
        VibeRunner,
    )

    for cls in (
        ClaudeRunner,
        PromptCliRunner,
        CodexRunner,
        GeminiRunner,
        OpenCodeRunner,
        VibeRunner,
        OllamaRunner,
    ):
        assert cls.supported_modes == frozenset({"cli", "api"}), cls.__name__


def test_non_agent_runners_are_cli_only() -> None:
    from hivepilot.runners.ansible_runner import AnsibleRunner
    from hivepilot.runners.container_runner import ContainerRunner
    from hivepilot.runners.helm_runner import HelmRunner
    from hivepilot.runners.iac_runner import (
        OpenTofuRunner,
        PulumiRunner,
        TerraformRunner,
    )
    from hivepilot.runners.internal_runner import InternalRunner
    from hivepilot.runners.kubectl_runner import KubectlRunner
    from hivepilot.runners.kustomize_runner import KustomizeRunner
    from hivepilot.runners.langchain_runner import LangChainRunner
    from hivepilot.runners.packer_runner import PackerRunner
    from hivepilot.runners.shell_runner import ShellRunner

    for cls in (
        ShellRunner,
        LangChainRunner,
        InternalRunner,
        ContainerRunner,
        TerraformRunner,
        OpenTofuRunner,
        PulumiRunner,
        KubectlRunner,
        AnsibleRunner,
        HelmRunner,
        KustomizeRunner,
        PackerRunner,
    ):
        assert cls.supported_modes == frozenset({"cli"}), cls.__name__


def test_every_registered_runner_exposes_supported_modes() -> None:
    """INVARIANT: every class registered in RUNNER_MAP must expose a
    `supported_modes` frozenset so the orchestrator can fail closed on an
    unsupported (kind, mode) combination for ANY runner it might dispatch.

    Sprint 2 (runner-defaults-plugins-mode PRD) carves out one deliberate
    exception: `openrouter` has no CLI binary at all, so its
    `supported_modes` is strictly `{"api"}` — every OTHER registered kind
    must still support (at least) `cli`.
    """
    from hivepilot.registry import RUNNER_MAP

    api_only_kinds = {"openrouter"}
    for kind, cls in RUNNER_MAP.items():
        modes = getattr(cls, "supported_modes", None)
        assert isinstance(modes, frozenset), f"{kind} ({cls.__name__}) lacks supported_modes"
        assert modes, f"{kind} ({cls.__name__}) has an empty supported_modes"
        if kind in api_only_kinds:
            assert modes == frozenset({"api"}), f"{kind} ({cls.__name__}) must be api-only"
        else:
            assert "cli" in modes, f"{kind} ({cls.__name__}) must at least support cli"


# ── validate_runner_mode fail-closed ──────────────────────────────────────────


def test_validate_runner_mode_passes_supported() -> None:
    validate_runner_mode("claude", frozenset({"cli", "api"}), "api")  # no raise
    validate_runner_mode("shell", frozenset({"cli"}), "cli")  # no raise


def test_validate_runner_mode_rejects_unsupported_with_clear_message() -> None:
    with pytest.raises(RunnerModeUnsupportedError) as exc_info:
        validate_runner_mode("shell", frozenset({"cli"}), "api")
    message = str(exc_info.value)
    assert "shell" in message
    assert "api" in message
    assert "supported" in message.lower()
    assert "cli" in message


# ── orchestrator propagation + fail-closed dispatch ──────────────────────────


def _bare_orchestrator():
    """Construct an Orchestrator with a real (empty) RunnerRegistry and stubbed
    plugins, so `_execute_task_body` resolves runner classes via RUNNER_MAP but
    performs no plugin/state side effects."""
    from hivepilot.orchestrator import Orchestrator
    from hivepilot.registry import RunnerRegistry

    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch(
            "hivepilot.orchestrator.load_pipelines",
            return_value=MagicMock(pipelines={}),
        ),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=RunnerRegistry({})),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
    ):
        orch = Orchestrator()
    orch.plugins = MagicMock()
    return orch


def test_mode_api_on_non_agent_runner_fails_before_subprocess(tmp_path: Path) -> None:
    """A resolved mode of `api` on a cli-only runner (shell) must raise
    RunnerModeUnsupportedError BEFORE any subprocess is spawned."""
    orch = _bare_orchestrator()
    task = TaskConfig(description="d", steps=[TaskStep(name="s", runner="shell")])
    project = ProjectConfig(path=tmp_path)

    with (
        patch.object(orch, "_resolve_secrets", return_value={}),
        patch("hivepilot.runners.shell_runner.subprocess.run") as mock_run,
    ):
        with pytest.raises(RunnerModeUnsupportedError, match="shell"):
            orch._execute_task_body(
                project=project,
                task_name="t",
                task=task,
                extra_prompt=None,
                auto_git=True,  # avoids stage-cache + worktree branches (git.commit=False)
                run_id=None,
                policy=None,
                simulate=False,
                dry_run=True,
                mode="api",
            )
    mock_run.assert_not_called()


def test_resolved_mode_is_propagated_into_step_metadata(tmp_path: Path) -> None:
    """The orchestrator must write the resolved mode into the step-metadata
    channel runners consult, so an api-capable runner actually sees `api`."""
    from hivepilot.registry import RUNNER_MAP, RunnerRegistry

    seen: list[str | None] = []

    class _RecordingRunner(BaseRunner):
        supported_modes = frozenset({"cli", "api"})

        def __init__(self, definition: RunnerDefinition, settings) -> None:  # noqa: ANN001
            self.definition = definition
            self.settings = settings

        def run(self, payload: RunnerPayload) -> None:  # pragma: no cover - unused
            seen.append(payload.step.metadata.get("mode"))

        def capture(self, payload: RunnerPayload) -> str:
            seen.append(payload.step.metadata.get("mode"))
            return "ok"

    RunnerRegistry.register("recording-mode", _RecordingRunner, override=True)
    try:
        orch = _bare_orchestrator()
        task = TaskConfig(description="d", steps=[TaskStep(name="s", runner="recording-mode")])
        project = ProjectConfig(path=tmp_path)
        with (
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch("hivepilot.orchestrator.perform_git_actions"),
        ):
            orch._execute_task_body(
                project=project,
                task_name="t",
                task=task,
                extra_prompt=None,
                auto_git=True,
                run_id=None,
                policy=None,
                simulate=False,
                dry_run=True,
                mode="api",
            )
        assert seen == ["api"]
    finally:
        RUNNER_MAP.pop("recording-mode", None)


def test_default_run_leaves_mode_cli(tmp_path: Path) -> None:
    """With no pipeline mode (the plain run_task default), the runner sees the
    unchanged `cli` default — the byte-identical existing behaviour."""
    from hivepilot.registry import RUNNER_MAP, RunnerRegistry

    seen: list[str | None] = []

    class _RecordingRunner(BaseRunner):
        supported_modes = frozenset({"cli", "api"})

        def __init__(self, definition: RunnerDefinition, settings) -> None:  # noqa: ANN001
            self.definition = definition
            self.settings = settings

        def run(self, payload: RunnerPayload) -> None:  # pragma: no cover - unused
            seen.append(payload.step.metadata.get("mode"))

        def capture(self, payload: RunnerPayload) -> str:
            seen.append(payload.step.metadata.get("mode"))
            return "ok"

    RunnerRegistry.register("recording-default", _RecordingRunner, override=True)
    try:
        orch = _bare_orchestrator()
        task = TaskConfig(description="d", steps=[TaskStep(name="s", runner="recording-default")])
        project = ProjectConfig(path=tmp_path)
        with (
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch("hivepilot.orchestrator.perform_git_actions"),
        ):
            orch._execute_task_body(
                project=project,
                task_name="t",
                task=task,
                extra_prompt=None,
                auto_git=True,
                run_id=None,
                policy=None,
                simulate=False,
                dry_run=True,
                # no mode kwarg → default "cli"
            )
        # cli default: the step metadata was never populated with a non-cli
        # override, so the runner falls through to its own "cli" default.
        assert seen == [None] or seen == ["cli"]
    finally:
        RUNNER_MAP.pop("recording-default", None)
