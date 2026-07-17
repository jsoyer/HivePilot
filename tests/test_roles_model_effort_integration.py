"""Sprint 3 (roles-model-effort-config-owned PRD): cross-cutting integration
tests binding together Sprint 1 (stage `model`/`effort`) and Sprint 2 (roles
config-owned, fail-closed validation) end-to-end.

Each piece already has focused unit coverage elsewhere
(`tests/test_stage_model_effort.py`, `tests/test_roles_config_owned.py`).
This file instead exercises the SEAMS between them, matching the sprint
spec's six scenarios -- asserting real behaviour (resolved dispatch tuples
actually reaching a runner's subprocess argv, roles actually loading off
disk, validation actually firing before dispatch), not just return shapes:

(a) a two-stage pipeline where per-stage `model`+`effort` are resolved via
    the REAL `resolve_stage_model`/`resolve_effort` (pipeline-vs-stage) +
    `resolve_stage_dispatch` (policy/stage/role/runner-default) chain, and
    the resolved values are then fed into REAL `ClaudeRunner`/`CodexRunner`
    instances whose subprocess argv (and, for Claude's effort, the
    MAX_THINKING_TOKENS env var) is asserted to actually carry them.
(b) `policy.role_overrides` outranks a stage-level override for BOTH `model`
    and `effort`, verified both at `resolve_stage_dispatch` and by building
    the runner argv from the resolved value (the stage's value never
    appears in argv, only the policy's).
(c) booting with no `roles.yaml` on disk resolves the code-owned
    `_DEFAULT_ROLES` fallback -- and that fallback is genuinely dispatchable
    (not just structurally present): plugged into `hivepilot.roles.ROLES`,
    `resolve_runner("developer")` resolves to `claude` with no crash.
(d) copying `examples/roles.yaml` into the active resolution chain restores
    the FULL company roster -- all seven removed business roles parse into
    valid `Role` objects via the real `load_roles()`, with their documented
    runner bindings (not just the three already spot-checked by
    `test_roles_config_owned.py`).
(e) an unknown `task.role` is caught by `pipeline_service.validate_pipeline`
    as an actionable `ValueError` naming the task/role -- BEFORE the bare
    `KeyError` that the same unknown role would raise at real dispatch
    (`hivepilot.roles.get_role`), proving validation runs strictly earlier
    in the pipeline than dispatch.
(f) a role with `models` length >= 2 (`ceo`, from this repo's own
    dogfooded root `roles.yaml`) still enters the dual-model debate
    fan-out through the REAL `Orchestrator.run_debate` -> `_run_debate_body`
    -> `DebateService` path (unchanged by the Sprint 1/2 refactor), while a
    single-model role (`developer`) is rejected.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import (
    PipelineConfig,
    PipelineStage,
    ProjectConfig,
    RunnerDefinition,
    TaskConfig,
    TasksFile,
    TaskStep,
    resolve_effort,
    resolve_stage_model,
)
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import EFFORT_TOKEN_MAP, ClaudeRunner
from hivepilot.runners.prompt_cli_runner import CodexRunner
from hivepilot.services.pipeline_service import validate_pipeline
from hivepilot.services.policy_service import Policy

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _payload(tmp_path: Path, step_metadata: dict | None = None) -> RunnerPayload:
    """Mirrors `tests/test_stage_model_effort.py::_payload` -- a minimal,
    real `RunnerPayload` with an on-disk prompt file so `ClaudeRunner`/
    `CodexRunner` can actually build their argv."""
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("do the thing", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(
            name="s", runner="x", prompt_file=str(prompt_file), metadata=step_metadata or {}
        ),
        metadata={},
        secrets={},
    )


def _bare_orchestrator():
    """Mirrors `tests/test_stage_model_effort.py::_bare_orchestrator` --
    an `Orchestrator` with no real projects/tasks/pipelines/plugins loaded,
    so `run_debate` can be exercised without any on-disk config."""
    from hivepilot.orchestrator import Orchestrator

    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch(
            "hivepilot.orchestrator.load_pipelines",
            return_value=MagicMock(pipelines={}),
        ),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
    ):
        orch = Orchestrator()
    orch.plugins = MagicMock()
    return orch


# ---------------------------------------------------------------------------
# (a) Two-stage pipeline: per-stage model+effort actually reach the runners.
# ---------------------------------------------------------------------------


class TestTwoStagePipelineModelEffortReachesRunners:
    def test_stage_overrides_reach_claude_and_codex_argv(self, tmp_path: Path) -> None:
        from hivepilot.roles import resolve_stage_dispatch

        # Pipeline-wide default effort ("high"), no pipeline-wide model.
        pipeline = PipelineConfig(description="two-stage", effort="high")
        dev_stage = PipelineStage(
            name="dev-stage", task="dev-task", model="claude-fast-x", effort="low"
        )
        review_stage = PipelineStage(name="review-stage", task="review-task")

        # Stage-vs-pipeline resolution (models.py).
        dev_model = resolve_stage_model(pipeline, dev_stage)
        dev_effort = resolve_effort(pipeline, dev_stage)
        review_model = resolve_stage_model(pipeline, review_stage)
        review_effort = resolve_effort(pipeline, review_stage)

        assert dev_model == "claude-fast-x"
        assert dev_effort == "low"
        assert review_model is None  # no pipeline-wide model set
        assert review_effort == "high"  # inherited from the pipeline default

        # Role-vs-stage-vs-policy resolution (roles.py). "developer" and
        # "reviewer" come from this repo's own dogfooded root roles.yaml
        # (module-level ROLES, loaded at import time) -- reviewer's own
        # binding is runner="codex", model="gpt-5.5".
        dev_runner, dev_final_model, dev_final_effort = resolve_stage_dispatch(
            "developer", None, dev_model, dev_effort
        )
        review_runner, review_final_model, review_final_effort = resolve_stage_dispatch(
            "reviewer", None, review_model, review_effort
        )

        assert (dev_runner, dev_final_model, dev_final_effort) == (
            "claude",
            "claude-fast-x",
            "low",
        )
        # No stage model for review -> falls back to the role's own model.
        assert (review_runner, review_final_model, review_final_effort) == (
            "codex",
            "gpt-5.5",
            "high",
        )

        # Now actually build runner definitions from these resolved values
        # and assert the CONCRETE subprocess argv carries them.
        claude_def = RunnerDefinition(
            kind=dev_runner, command="claude", model=dev_final_model, effort=dev_final_effort
        )
        claude_runner = ClaudeRunner(claude_def, settings)
        with patch("hivepilot.runners.claude_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            claude_runner.run(_payload(tmp_path))
        claude_args = mock_run.call_args.args[0]
        assert "--model" in claude_args
        assert claude_args[claude_args.index("--model") + 1] == "claude-fast-x"
        # Claude effort is NOT a no-op: the resolved "low" is injected as the
        # MAX_THINKING_TOKENS env var (EFFORT_TOKEN_MAP["low"] == 4000) on the
        # subprocess -- it just never becomes an argv entry.
        assert not any("effort" in str(a).lower() for a in claude_args)
        assert mock_run.call_args.kwargs["env"]["MAX_THINKING_TOKENS"] == str(
            EFFORT_TOKEN_MAP["low"]
        )

        codex_def = RunnerDefinition(
            kind=review_runner,
            command="codex",
            model=review_final_model,
            effort=review_final_effort,
        )
        codex_runner = CodexRunner(codex_def, settings)
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run2:
            codex_runner.run(_payload(tmp_path))
        codex_args = mock_run2.call_args.args[0]
        assert "--model" in codex_args
        assert codex_args[codex_args.index("--model") + 1] == "gpt-5.5"
        assert "-c" in codex_args
        idx = codex_args.index("-c")
        assert codex_args[idx + 1] == "model_reasoning_effort=high"


# ---------------------------------------------------------------------------
# (b) policy.role_overrides > stage precedence, propagated into argv.
# ---------------------------------------------------------------------------


class TestPolicyOverridesOutrankStageInArgv:
    def test_policy_model_and_effort_win_over_stage_and_reach_argv(self, tmp_path: Path) -> None:
        from hivepilot.roles import resolve_stage_dispatch

        policy = Policy(
            role_overrides={"developer": {"model": "policy-pinned-model", "effort": "max"}}
        )
        runner, model, effort = resolve_stage_dispatch(
            "developer", policy, stage_model="stage-model", stage_effort="low"
        )
        assert runner == "claude"
        assert model == "policy-pinned-model"  # policy beats stage
        assert effort == "max"  # policy beats stage

        definition = RunnerDefinition(kind=runner, command="claude", model=model, effort=effort)
        runner_obj = ClaudeRunner(definition, settings)
        with patch("hivepilot.runners.claude_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            runner_obj.run(_payload(tmp_path))
        args = mock_run.call_args.args[0]
        assert "--model" in args
        assert args[args.index("--model") + 1] == "policy-pinned-model"
        assert "stage-model" not in args


# ---------------------------------------------------------------------------
# (c) Boot with no roles.yaml -> _DEFAULT_ROLES fallback, genuinely
# dispatchable via resolve_runner.
# ---------------------------------------------------------------------------


class TestNoRolesYamlFallsBackToDeveloperAndDispatches:
    def test_missing_roles_yaml_falls_back_and_resolve_runner_works(self, monkeypatch) -> None:
        from hivepilot import roles as roles_module

        non_existent = Path("/tmp/does_not_exist_hivepilot_roles_sprint3.yaml")
        mock_settings = type(
            "MockSettings",
            (),
            {
                "roles_file": non_existent,
                "resolve_config_path": lambda self, f: non_existent,
            },
        )()

        import hivepilot.config as config_module

        original_settings = config_module.settings
        try:
            config_module.settings = mock_settings
            fallback_roles = roles_module.load_roles()
        finally:
            config_module.settings = original_settings

        assert set(fallback_roles) == {"developer"}
        assert fallback_roles["developer"].runner == "claude"

        # Prove it's genuinely dispatchable, not just structurally present:
        # plug the fallback dict into the module-level ROLES registry
        # (exactly what `refresh_roles()` does after a real load) and drive
        # it through the real `resolve_runner`.
        original_roles = roles_module.ROLES
        try:
            roles_module.ROLES = fallback_roles
            runner, model, effort = roles_module.resolve_runner("developer")
        finally:
            roles_module.ROLES = original_roles

        assert runner == "claude"
        assert model is None  # no hard-coded model on the fallback developer role
        assert effort is None  # the generic developer declares no effort tier


# ---------------------------------------------------------------------------
# (d) examples/roles.yaml restores the full company roster via load_roles().
# ---------------------------------------------------------------------------


class TestExampleRolesYamlRestoresFullRoster:
    REPO_ROOT = Path(__file__).parent.parent
    EXPECTED_RUNNERS = {
        "ceo": "opencode",
        "chief_of_staff": "cursor",
        "cto": "opencode",
        "reviewer": "codex",
        "ciso": "opencode",
        "qa": "cursor",
        "documentation": "gemini",
    }

    def test_all_seven_removed_roles_restore_with_expected_runners(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        import hivepilot.config as config_module
        from hivepilot.roles import Role, load_roles

        test_settings = config_module.Settings(base_dir=self.REPO_ROOT / "examples")
        original_settings = config_module.settings
        try:
            config_module.settings = test_settings
            loaded = load_roles()
        finally:
            config_module.settings = original_settings

        assert self.EXPECTED_RUNNERS.keys() <= set(loaded)
        for role_name, expected_runner in self.EXPECTED_RUNNERS.items():
            role = loaded[role_name]
            assert isinstance(role, Role)
            assert role.runner == expected_runner, (
                f"{role_name}: expected runner={expected_runner!r}, got {role.runner!r}"
            )
        # developer also ships in the example (order=4, runner=claude).
        assert loaded["developer"].runner == "claude"


# ---------------------------------------------------------------------------
# (e) Unknown role -> actionable ValueError at validation, strictly BEFORE
# the bare KeyError the same role would raise at real dispatch.
# ---------------------------------------------------------------------------


class TestUnknownRoleValidatesBeforeDispatch:
    def test_validate_pipeline_raises_actionable_error_for_unknown_role(self) -> None:
        pipeline = PipelineConfig(
            description="t", stages=[PipelineStage(name="Stage A", task="task-a")]
        )
        tasks = TasksFile(
            tasks={"task-a": TaskConfig(description="d", role="totally_unknown_role")}
        )

        with pytest.raises(ValueError) as exc_info:
            validate_pipeline(pipeline, tasks)

        assert not isinstance(exc_info.value, KeyError)
        message = str(exc_info.value)
        assert "task-a" in message
        assert "totally_unknown_role" in message
        assert "roles.yaml" in message

    def test_same_unknown_role_raises_bare_keyerror_at_real_dispatch(self) -> None:
        """Contrast case: proves *why* the validation gate in
        `validate_pipeline` matters -- without it, the SAME unknown role
        would only surface as an unhelpful `KeyError` deep inside dispatch,
        well after a run has already started."""
        from hivepilot.roles import get_role

        with pytest.raises(KeyError):
            get_role("totally_unknown_role")


# ---------------------------------------------------------------------------
# (f) Dual-model debate fan-out (>= 2 models) unchanged by the refactor.
# ---------------------------------------------------------------------------


class TestDualModelDebateFanOutUnchanged:
    def test_ceo_role_with_two_models_enters_debate_and_synthesizes(self, monkeypatch) -> None:
        from hivepilot.models import ProjectConfig

        orch = _bare_orchestrator()
        orch.registry = MagicMock()
        monkeypatch.setattr(orch, "_project", lambda name: ProjectConfig(path=Path("/tmp/p")))
        monkeypatch.setattr(orch, "_resolve_secrets", lambda *a, **k: {})

        captured: dict = {}

        class FakeDebate:
            def __init__(self, vault, dry_run=True):
                pass

            def run(self, topic, positions, decision=None, **kw):
                captured["positions"] = positions
                captured["decision"] = decision
                return {"path": "ADR.md", "dry_run": True}

        monkeypatch.setattr("hivepilot.services.debate_service.DebateService", FakeDebate)
        # Deliberately NOT "p" -- tests/test_policy_service.py's monkeypatched
        # `policy_service._cache` for project "p" is only ever cleared by
        # `reload_policies()` right before its own assertion, never restored
        # after; sharing that project name here would depend on suite run
        # order (whether that module-level cache got poisoned by an earlier
        # test) instead of on this test's own real behaviour.
        with patch("hivepilot.orchestrator.state_service.record_interaction"):
            adr = orch.run_debate(
                project_name="roles-effort-debate-project",
                role_name="ceo",
                topic="adopt X?",
                simulate=True,
            )

        assert adr == {"path": "ADR.md", "dry_run": True}
        # ceo has models=["opencode-go/qwen3.7-max", "opencode-go/kimi-k2.6"]
        # in this repo's own root roles.yaml -- both must produce a position.
        assert len(captured["positions"]) == 2
        roles_seen = {pos.role for pos in captured["positions"]}
        assert roles_seen == {
            "ceo:opencode-go/qwen3.7-max",
            "ceo:opencode-go/kimi-k2.6",
        }
        # simulate=True -> never a real dispatch call.
        orch.registry.capture_definition.assert_not_called()

    def test_single_model_role_rejected_from_debate(self) -> None:
        orch = _bare_orchestrator()
        with pytest.raises(ValueError, match="not a dual-model debate role"):
            orch.run_debate(
                project_name="roles-effort-debate-project",
                role_name="developer",
                topic="x",
                simulate=True,
            )
