"""Sprint 1 (roles-model-effort-config-owned PRD): stage `model`/`effort` +
resolution precedence + effort propagation.

Covers:
- `PipelineStage`/`PipelineConfig`/`RunnerDefinition` accept `model`/`effort`;
  an invalid `effort` value is rejected by Pydantic.
- `resolve_stage_model`/`resolve_effort` (models.py): stage-over-pipeline
  precedence, mirroring `resolve_mode`.
- `hivepilot.roles.resolve_stage_dispatch`: full `policy > stage > role >
  runner-default` precedence, `allowed_runners` fail-closed enforcement, and
  `resolve_runner` byte-identical delegation.
- Effort propagation into runners: `CodexRunner` builds
  `-c model_reasoning_effort=<level>` (defaulting to `"medium"` when unset —
  byte-identical to the pre-Sprint-1 hardcoded tuple); `ClaudeRunner` and
  other prompt-cli runners treat effort as a documented no-op (never crash).
- Follow-up fix: `options["cli_flags"]` is an operator escape hatch that on
  origin/main REPLACED the fixed effort tuple wholesale (no effort flag at
  all). `_effort_cli_flags` preserves that when no effort is resolved
  anywhere, while an explicit stage/role/policy effort still always wins
  (`TestCodexCliFlagsEscapeHatchPrecedence`).
- Orchestrator dispatch: a stage that sets neither `model` nor `effort`
  dispatches byte-identically to before these fields existed; a stage that
  DOES set them propagates into the runner definition actually used.
- The dual-model debate trigger (`len(role.models) > 1`) is unaffected by the
  new `stage_model`/`stage_effort` params.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.models import (
    EffortLevel,
    PipelineConfig,
    PipelineStage,
    ProjectConfig,
    RunnerDefinition,
    TaskConfig,
    TaskStep,
    resolve_effort,
    resolve_stage_model,
)
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner
from hivepilot.runners.prompt_cli_runner import CodexRunner, GeminiRunner
from hivepilot.services.policy_service import Policy

# ---------------------------------------------------------------------------
# Field validation: PipelineStage / PipelineConfig / RunnerDefinition
# ---------------------------------------------------------------------------


class TestFieldValidation:
    def test_pipeline_stage_model_effort_default_to_none(self) -> None:
        stage = PipelineStage(name="s", task="t")
        assert stage.model is None
        assert stage.effort is None

    def test_pipeline_stage_accepts_model_and_effort(self) -> None:
        stage = PipelineStage(name="s", task="t", model="gpt-5.5", effort="high")
        assert stage.model == "gpt-5.5"
        assert stage.effort == "high"

    @pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
    def test_pipeline_stage_accepts_every_effort_level(self, level: EffortLevel) -> None:
        assert PipelineStage(name="s", task="t", effort=level).effort == level

    def test_pipeline_stage_rejects_invalid_effort(self) -> None:
        with pytest.raises(ValueError):
            PipelineStage(name="s", task="t", effort="extreme")  # type: ignore[arg-type]

    def test_pipeline_config_model_effort_default_to_none(self) -> None:
        pipeline = PipelineConfig(description="d")
        assert pipeline.model is None
        assert pipeline.effort is None

    def test_pipeline_config_accepts_model_and_effort(self) -> None:
        pipeline = PipelineConfig(description="d", model="gpt-5.5", effort="low")
        assert pipeline.model == "gpt-5.5"
        assert pipeline.effort == "low"

    def test_pipeline_config_rejects_invalid_effort(self) -> None:
        with pytest.raises(ValueError):
            PipelineConfig(description="d", effort="extreme")  # type: ignore[arg-type]

    def test_runner_definition_effort_defaults_to_none(self) -> None:
        assert RunnerDefinition(kind="codex").effort is None

    def test_runner_definition_accepts_effort(self) -> None:
        assert RunnerDefinition(kind="codex", effort="xhigh").effort == "xhigh"

    def test_runner_definition_rejects_invalid_effort(self) -> None:
        with pytest.raises(ValueError):
            RunnerDefinition(kind="codex", effort="extreme")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# resolve_stage_model / resolve_effort (models.py) — stage over pipeline
# ---------------------------------------------------------------------------


class TestResolveStageModelAndEffort:
    def test_resolve_stage_model_defaults_to_none(self) -> None:
        pipeline = PipelineConfig(description="d")
        stage = PipelineStage(name="s", task="t")
        assert resolve_stage_model(pipeline, stage) is None

    def test_resolve_stage_model_pipeline_over_default(self) -> None:
        pipeline = PipelineConfig(description="d", model="pipeline-model")
        stage = PipelineStage(name="s", task="t")
        assert resolve_stage_model(pipeline, stage) == "pipeline-model"

    def test_resolve_stage_model_stage_over_pipeline(self) -> None:
        pipeline = PipelineConfig(description="d", model="pipeline-model")
        stage = PipelineStage(name="s", task="t", model="stage-model")
        assert resolve_stage_model(pipeline, stage) == "stage-model"

    def test_resolve_effort_defaults_to_none(self) -> None:
        pipeline = PipelineConfig(description="d")
        stage = PipelineStage(name="s", task="t")
        assert resolve_effort(pipeline, stage) is None

    def test_resolve_effort_pipeline_over_default(self) -> None:
        pipeline = PipelineConfig(description="d", effort="low")
        stage = PipelineStage(name="s", task="t")
        assert resolve_effort(pipeline, stage) == "low"

    def test_resolve_effort_stage_over_pipeline(self) -> None:
        pipeline = PipelineConfig(description="d", effort="low")
        stage = PipelineStage(name="s", task="t", effort="high")
        assert resolve_effort(pipeline, stage) == "high"


# ---------------------------------------------------------------------------
# resolve_stage_dispatch — policy > stage > role > runner-default precedence
# ---------------------------------------------------------------------------


class TestResolveStageDispatchPrecedence:
    def test_role_default_when_no_stage_no_policy(self) -> None:
        from hivepilot.roles import resolve_stage_dispatch

        runner, model, effort = resolve_stage_dispatch("developer")
        assert runner == "claude"
        assert model is None  # developer role sets no explicit model
        assert effort is None

    def test_stage_model_overrides_role_default(self) -> None:
        from hivepilot.roles import resolve_stage_dispatch

        runner, model, effort = resolve_stage_dispatch("developer", stage_model="claude-opus")
        assert runner == "claude"
        assert model == "claude-opus"
        assert effort is None

    def test_stage_effort_overrides_role_default_none(self) -> None:
        from hivepilot.roles import resolve_stage_dispatch

        runner, model, effort = resolve_stage_dispatch("developer", stage_effort="high")
        assert effort == "high"

    def test_policy_wins_over_stage_model(self) -> None:
        """A policy `role_overrides` entry must NEVER be short-circuited by a
        stage-level model — policy is the security control."""
        from hivepilot.roles import resolve_stage_dispatch

        policy = Policy(role_overrides={"developer": {"model": "policy-model"}})
        runner, model, effort = resolve_stage_dispatch(
            "developer", policy, stage_model="stage-model"
        )
        assert model == "policy-model"
        assert runner == "claude"
        assert effort is None

    def test_policy_wins_over_stage_effort(self) -> None:
        """Same policy-outranks-stage guarantee for `effort`."""
        from hivepilot.roles import resolve_stage_dispatch

        policy = Policy(role_overrides={"developer": {"effort": "low"}})
        runner, model, effort = resolve_stage_dispatch("developer", policy, stage_effort="max")
        assert effort == "low"

    def test_policy_invalid_effort_override_fails_closed(self) -> None:
        """A policy `role_overrides[role].effort` is raw, operator-supplied
        config — unlike `stage.effort`/`role.effort` (pydantic-validated
        `EffortLevel` fields), it must be validated at the point it is read
        so a typo/misconfiguration (e.g. "extreme") fails loudly via
        `hivepilot.models.validate_effort` instead of silently reaching a
        runner as an unrecognized value."""
        from hivepilot.roles import resolve_stage_dispatch

        policy = Policy(role_overrides={"developer": {"effort": "extreme"}})
        with pytest.raises(ValueError, match="effort must be one of"):
            resolve_stage_dispatch("developer", policy)

    def test_policy_valid_effort_override_passes_through(self) -> None:
        """A legal `EffortLevel` string in a policy override still resolves
        normally through the validation wrapper."""
        from hivepilot.roles import resolve_stage_dispatch

        policy = Policy(role_overrides={"developer": {"effort": "high"}})
        runner, model, effort = resolve_stage_dispatch("developer", policy)
        assert runner == "claude"
        assert effort == "high"

    def test_policy_runner_override_still_applies_with_stage_model_set(self) -> None:
        from hivepilot.roles import resolve_stage_dispatch

        policy = Policy(role_overrides={"developer": {"runner": "codex"}})
        runner, model, effort = resolve_stage_dispatch(
            "developer", policy, stage_model="stage-model"
        )
        assert runner == "codex"
        assert model == "stage-model"

    def test_allowed_runners_fails_closed_even_with_stage_override(self) -> None:
        from hivepilot.roles import resolve_stage_dispatch

        policy = Policy(allowed_runners=["opencode"])
        with pytest.raises(RuntimeError, match="allowed_runners"):
            resolve_stage_dispatch("developer", policy, stage_model="whatever")

    def test_allowed_runners_fails_closed_with_no_stage_override(self) -> None:
        """Same fail-closed guarantee on the no-stage delegate-to-resolve_runner
        path (stage_model/stage_effort both None)."""
        from hivepilot.roles import resolve_stage_dispatch

        policy = Policy(allowed_runners=["opencode"])
        with pytest.raises(RuntimeError, match="allowed_runners"):
            resolve_stage_dispatch("developer", policy)

    def test_no_stage_args_matches_resolve_runner_exactly(self) -> None:
        """`resolve_stage_dispatch(role, policy)` with no stage args must
        return the exact same (runner, model) `resolve_runner` returns — the
        byte-identical "stage sets nothing" contract."""
        from hivepilot.roles import resolve_runner, resolve_stage_dispatch

        policy = Policy(role_overrides={"reviewer": {"model": "gpt-6"}})
        expected_runner, expected_model, expected_effort = resolve_runner("reviewer", policy)
        runner, model, effort = resolve_stage_dispatch("reviewer", policy)
        assert (runner, model, effort) == (expected_runner, expected_model, expected_effort)

    def test_resolve_runner_unaffected_by_this_sprint(self) -> None:
        """`resolve_runner`'s runner/model resolution (used by callers with no
        stage context, e.g. the dual-model debate path) is unchanged; it now
        returns a 3-tuple `(runner, model, effort)` (unified effort system)."""
        from hivepilot.roles import resolve_runner

        runner, model, _effort = resolve_runner("reviewer")
        assert runner == "codex"
        assert model == "gpt-5.5"


class TestAllowedRunnersFailClosedOnEmptyList:
    """Regression: `allowed_runners=[]` MUST deny every runner (fail-closed),
    not silently allow all. `None` (absent) is the only "unconstrained" value.
    Both dispatch paths — `resolve_runner` and the no-stage delegate branch of
    `resolve_stage_dispatch`, AND the stage-override branch — must agree, so a
    future change to one gate cannot silently diverge from the other."""

    def test_empty_allowed_runners_denies_via_resolve_runner(self) -> None:
        from hivepilot.roles import resolve_runner

        policy = Policy(allowed_runners=[])
        with pytest.raises(RuntimeError, match="allowed_runners"):
            resolve_runner("developer", policy)

    def test_empty_allowed_runners_denies_via_resolve_stage_dispatch_no_stage(
        self,
    ) -> None:
        # No stage args -> delegate branch (routes through resolve_runner).
        from hivepilot.roles import resolve_stage_dispatch

        policy = Policy(allowed_runners=[])
        with pytest.raises(RuntimeError, match="allowed_runners"):
            resolve_stage_dispatch("developer", policy)

    def test_empty_allowed_runners_denies_via_resolve_stage_dispatch_with_stage(
        self,
    ) -> None:
        # Stage override present -> the SECOND, independent gate at roles.py:~382.
        from hivepilot.roles import resolve_stage_dispatch

        policy = Policy(allowed_runners=[])
        with pytest.raises(RuntimeError, match="allowed_runners"):
            resolve_stage_dispatch("developer", policy, stage_model="whatever")

    def test_none_allowed_runners_is_unconstrained(self) -> None:
        """`None` must remain "no constraint" — the developer role resolves
        cleanly through every path."""
        from hivepilot.roles import resolve_runner, resolve_stage_dispatch

        policy = Policy(allowed_runners=None)
        assert resolve_runner("developer", policy)[0] == "claude"
        assert resolve_stage_dispatch("developer", policy)[0] == "claude"
        assert resolve_stage_dispatch("developer", policy, stage_model="m")[0] == "claude"

    def test_populated_allowed_runners_allows_listed_runner(self) -> None:
        """Sanity: a non-empty allowlist that INCLUDES the resolved runner
        still permits it (guards against an over-broad fix that denies all)."""
        from hivepilot.roles import resolve_runner, resolve_stage_dispatch

        policy = Policy(allowed_runners=["claude"])
        assert resolve_runner("developer", policy)[0] == "claude"
        assert resolve_stage_dispatch("developer", policy, stage_model="m")[0] == "claude"

    def test_both_gates_agree_across_the_allowed_runners_matrix(self) -> None:
        """Parity: for every `allowed_runners` value, `resolve_runner` and BOTH
        branches of `resolve_stage_dispatch` reach the SAME allow/deny verdict —
        so the duplicated gate logic cannot silently diverge."""
        from hivepilot.roles import resolve_runner, resolve_stage_dispatch

        cases = [
            (None, True),  # unconstrained -> allow
            ([], False),  # explicit deny-all -> deny
            (["claude"], True),  # allowlist includes resolved runner -> allow
            (["opencode"], False),  # allowlist excludes it -> deny
        ]

        def _verdict(fn) -> bool:
            try:
                fn()
                return True
            except RuntimeError:
                return False

        for allowed, should_allow in cases:
            policy = Policy(allowed_runners=allowed)
            v_runner = _verdict(lambda p=policy: resolve_runner("developer", p))
            v_disp_nostage = _verdict(lambda p=policy: resolve_stage_dispatch("developer", p))
            v_disp_stage = _verdict(
                lambda p=policy: resolve_stage_dispatch("developer", p, stage_model="m")
            )
            assert v_runner == should_allow, (allowed, "resolve_runner")
            assert v_disp_nostage == should_allow, (allowed, "dispatch/no-stage")
            assert v_disp_stage == should_allow, (allowed, "dispatch/stage")
            # All three paths must agree with each other, not just with the flag.
            assert v_runner == v_disp_nostage == v_disp_stage, (
                allowed,
                "gate divergence between dispatch paths",
            )


# ---------------------------------------------------------------------------
# Effort propagation — CodexRunner / ClaudeRunner / other prompt-cli runners
# ---------------------------------------------------------------------------


def _payload(
    tmp_path: Path,
    step_metadata: dict | None = None,
    step_effort: EffortLevel | None = None,
) -> RunnerPayload:
    """*step_effort* lands on the first-class ``TaskStep.effort`` field — the
    per-step effort channel ``resolve_runner_effort`` reads (as a FALLBACK
    beneath the authoritative ``RunnerDefinition.effort``). *step_metadata*
    lands on ``step.metadata`` (the per-step *model* override channel, and the
    extra_prompt/prior_context channel is ``payload.metadata``)."""
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("do the thing", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(
            name="s",
            runner="x",
            prompt_file=str(prompt_file),
            metadata=step_metadata or {},
            effort=step_effort,
        ),
        metadata={},
        secrets={},
    )


class TestCodexEffortPropagation:
    def test_default_medium_when_effort_unset_byte_identical(self, tmp_path: Path) -> None:
        """Byte-identical to the pre-Sprint-1 hardcoded
        `cli_flags = ("-c", "model_reasoning_effort=medium")` tuple."""
        from hivepilot.config import settings

        runner = CodexRunner(RunnerDefinition(kind="codex", command="codex"), settings)
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            runner.run(_payload(tmp_path))
        args = mock_run.call_args.args[0]
        assert args[:2] == ["codex", "exec"]
        assert "-c" in args
        idx = args.index("-c")
        assert args[idx + 1] == "model_reasoning_effort=medium"
        assert args[-1] == "do the thing"

    def test_definition_effort_is_used(self, tmp_path: Path) -> None:
        from hivepilot.config import settings

        runner = CodexRunner(
            RunnerDefinition(kind="codex", command="codex", effort="high"), settings
        )
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            runner.run(_payload(tmp_path))
        args = mock_run.call_args.args[0]
        idx = args.index("-c")
        assert args[idx + 1] == "model_reasoning_effort=high"

    def test_definition_effort_authoritative_over_step(self, tmp_path: Path) -> None:
        """Unified precedence: the orchestrator-resolved
        ``RunnerDefinition.effort`` (``policy > stage > role``) is authoritative
        and WINS over a per-step ``TaskStep.effort`` — a step can never silently
        override a stage/policy-mandated effort (same rule Claude enforces)."""
        from hivepilot.config import settings

        runner = CodexRunner(
            RunnerDefinition(kind="codex", command="codex", effort="low"), settings
        )
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            runner.run(_payload(tmp_path, step_effort="xhigh"))
        args = mock_run.call_args.args[0]
        idx = args.index("-c")
        assert args[idx + 1] == "model_reasoning_effort=low"

    def test_step_effort_applies_as_fallback_when_definition_none(self, tmp_path: Path) -> None:
        """A per-step ``TaskStep.effort`` still drives Codex when nothing was
        resolved upstream (``RunnerDefinition.effort is None``)."""
        from hivepilot.config import settings

        runner = CodexRunner(RunnerDefinition(kind="codex", command="codex"), settings)
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            runner.run(_payload(tmp_path, step_effort="high"))
        args = mock_run.call_args.args[0]
        idx = args.index("-c")
        assert args[idx + 1] == "model_reasoning_effort=high"


class TestStageEffortReachesBothRunners:
    """Unified-system proof: a SINGLE stage-level effort, once resolved via
    ``resolve_stage_dispatch``, drives BOTH the Claude runner (as a
    ``MAX_THINKING_TOKENS`` value) AND the Codex runner (as a
    ``-c model_reasoning_effort=<level>`` flag) — the two previously-separate
    effort mechanisms now share one resolved value."""

    def test_single_stage_effort_reaches_claude_and_codex(self, tmp_path: Path) -> None:
        from hivepilot.config import settings
        from hivepilot.roles import resolve_stage_dispatch

        # `developer` role binds to claude; resolve a stage-level effort on it.
        _runner, _model, resolved = resolve_stage_dispatch("developer", stage_effort="xhigh")
        assert resolved == "xhigh"

        # Claude runner: resolved effort -> MAX_THINKING_TOKENS (40000 for xhigh).
        claude = ClaudeRunner(
            RunnerDefinition(name="claude", kind="claude", command="claude", effort=resolved),
            settings,
        )
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0)
            claude.run(_effort_payload_claude(tmp_path))
        assert m.call_args.kwargs["env"]["MAX_THINKING_TOKENS"] == "40000"

        # Codex runner: SAME resolved effort -> -c model_reasoning_effort=xhigh.
        codex = CodexRunner(
            RunnerDefinition(kind="codex", command="codex", effort=resolved), settings
        )
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            codex.run(_payload(tmp_path))
        args = mock_run.call_args.args[0]
        assert args[args.index("-c") + 1] == "model_reasoning_effort=xhigh"


def _effort_payload_claude(tmp_path: Path) -> RunnerPayload:
    from hivepilot.models import ProjectConfig, TaskStep

    pf = tmp_path / "c.md"
    pf.write_text("do it", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude", prompt_file=str(pf)),
        metadata={},
        secrets={},
    )


class TestCodexCliFlagsEscapeHatchPrecedence:
    """Follow-up fix (post-Sprint-1): `options["cli_flags"]` is an operator
    escape hatch that on origin/main REPLACED the fixed
    `("-c", "model_reasoning_effort=medium")` tuple wholesale — an operator
    using it got NO effort flag at all. `_effort_cli_flags` must preserve
    that behavior when no effort is resolved anywhere, while still letting
    an EXPLICIT stage/role/policy effort win (more specific config).

    Covers all four rows of the effort-resolved x cli_flags-set matrix.
    """

    def test_no_effort_no_cli_flags_override_emits_default_medium(self, tmp_path: Path) -> None:
        """Row 1: nothing configured anywhere -> byte-identical default."""
        from hivepilot.config import settings

        runner = CodexRunner(RunnerDefinition(kind="codex", command="codex"), settings)
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            runner.run(_payload(tmp_path))
        args = mock_run.call_args.args[0]
        assert args == [
            "codex",
            "exec",
            "-c",
            "model_reasoning_effort=medium",
            "do the thing",
        ]

    def test_no_effort_with_cli_flags_override_emits_no_effort_flag(self, tmp_path: Path) -> None:
        """Row 2 (the regression this follow-up fixes): an operator who set
        `options["cli_flags"]` fully owns the flag surface, as on
        origin/main — NO `model_reasoning_effort` flag is injected, and the
        full argv matches exactly what origin/main would have produced for
        the same `options["cli_flags"]`."""
        from hivepilot.config import settings

        runner = CodexRunner(
            RunnerDefinition(kind="codex", command="codex", options={"cli_flags": ["--foo"]}),
            settings,
        )
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            runner.run(_payload(tmp_path))
        args = mock_run.call_args.args[0]
        assert args == ["codex", "exec", "--foo", "do the thing"]
        assert not any("model_reasoning_effort" in str(a) for a in args)

    def test_effort_resolved_without_cli_flags_override_emits_resolved_effort(
        self, tmp_path: Path
    ) -> None:
        """Row 3: a stage/role/policy effort with no `cli_flags` override
        emits that resolved effort (already covered by
        `TestCodexEffortPropagation.test_definition_effort_is_used`; kept
        here to document the full four-row matrix in one place)."""
        from hivepilot.config import settings

        runner = CodexRunner(
            RunnerDefinition(kind="codex", command="codex", effort="high"), settings
        )
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            runner.run(_payload(tmp_path))
        args = mock_run.call_args.args[0]
        assert args == [
            "codex",
            "exec",
            "-c",
            "model_reasoning_effort=high",
            "do the thing",
        ]

    def test_no_effort_with_cli_flags_already_containing_effort_emits_exactly_one(
        self, tmp_path: Path
    ) -> None:
        """Regression guard: no effort resolved anywhere, but the operator's
        own `options["cli_flags"]` already spells out a
        `model_reasoning_effort=` entry — `_effort_cli_flags` must NOT add a
        second one. Since no effort is resolved, `_effort_cli_flags` returns
        `[]` (row 2), so the operator's own single entry is the only one in
        the final argv — exactly ONE `model_reasoning_effort` entry, no
        ambiguous duplicate/last-wins pair."""
        from hivepilot.config import settings

        runner = CodexRunner(
            RunnerDefinition(
                kind="codex",
                command="codex",
                options={"cli_flags": ["-c", "model_reasoning_effort=high"]},
            ),
            settings,
        )
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            runner.run(_payload(tmp_path))
        args = mock_run.call_args.args[0]
        effort_entries = [a for a in args if str(a).startswith("model_reasoning_effort=")]
        assert effort_entries == ["model_reasoning_effort=high"]
        assert args == [
            "codex",
            "exec",
            "-c",
            "model_reasoning_effort=high",
            "do the thing",
        ]

    def test_effort_resolved_with_cli_flags_override_wins_over_operator_flags(
        self, tmp_path: Path
    ) -> None:
        """Row 4: an EXPLICIT stage/role/policy effort is more specific
        config than the operator's `options["cli_flags"]` escape hatch, so
        it still wins and is appended — even when the operator's own
        `cli_flags` already contains a (now-stale) `model_reasoning_effort`
        entry, the explicit one is appended LAST, so `codex`'s own
        last-value-wins `-c` semantics resolve the pair unambiguously in
        favor of the explicit, more-specific value."""
        from hivepilot.config import settings

        runner = CodexRunner(
            RunnerDefinition(
                kind="codex",
                command="codex",
                effort="xhigh",
                options={"cli_flags": ["-c", "model_reasoning_effort=high"]},
            ),
            settings,
        )
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            runner.run(_payload(tmp_path))
        args = mock_run.call_args.args[0]
        effort_entries = [a for a in args if str(a).startswith("model_reasoning_effort=")]
        assert effort_entries == ["model_reasoning_effort=high", "model_reasoning_effort=xhigh"]
        assert effort_entries[-1] == "model_reasoning_effort=xhigh"
        assert args == [
            "codex",
            "exec",
            "-c",
            "model_reasoning_effort=high",
            "-c",
            "model_reasoning_effort=xhigh",
            "do the thing",
        ]


class TestNonEffortRunnersIgnoreEffortSafely:
    def test_claude_runner_never_crashes_and_emits_no_effort_flag(self, tmp_path: Path) -> None:
        from hivepilot.config import settings

        runner = ClaudeRunner(
            RunnerDefinition(kind="claude", command="claude", effort="max"), settings
        )
        with patch("hivepilot.runners.claude_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            runner.run(_payload(tmp_path))
        args = mock_run.call_args.args[0]
        assert not any("effort" in str(a).lower() for a in args)

    def test_gemini_runner_never_crashes_with_effort_set(self, tmp_path: Path) -> None:
        from hivepilot.config import settings

        runner = GeminiRunner(
            RunnerDefinition(kind="gemini", command="gemini", effort="max"), settings
        )
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            runner.run(_payload(tmp_path))
        args = mock_run.call_args.args[0]
        assert not any("effort" in str(a).lower() for a in args)


# ---------------------------------------------------------------------------
# Orchestrator dispatch — byte-identical when stage sets neither field,
# propagated when it does.
# ---------------------------------------------------------------------------


def _bare_orchestrator():
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


class TestOrchestratorStageDispatchByteIdentical:
    def test_stage_unset_dispatches_byte_identically(self, tmp_path: Path) -> None:
        """A role-driven task run with no stage_model/stage_effort (the
        plain `run_task` default) must build the EXACT same RunnerDefinition
        (model=None, effort=None) as before these fields existed."""
        orch = _bare_orchestrator()
        orch.registry = MagicMock()
        orch.registry.capture_definition.return_value = "ok"
        task = TaskConfig(
            description="dev",
            role="developer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
        )
        project = ProjectConfig(path=tmp_path)
        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            orch._execute_task(
                project=project,
                task_name="developer",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                simulate=False,
                dry_run=True,
            )
        called_def = orch.registry.capture_definition.call_args.args[0]
        assert called_def.model is None
        assert called_def.effort is None
        assert called_def.kind == "claude"

    def test_stage_model_and_effort_propagate_into_runner_definition(self, tmp_path: Path) -> None:
        orch = _bare_orchestrator()
        orch.registry = MagicMock()
        orch.registry.capture_definition.return_value = "ok"
        task = TaskConfig(
            description="dev",
            role="developer",
            engine="native",
            steps=[TaskStep(name="s", runner="claude", prompt_file="p.md")],
        )
        project = ProjectConfig(path=tmp_path)
        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            orch._execute_task(
                project=project,
                task_name="developer",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                simulate=False,
                dry_run=True,
                stage_model="claude-opus-x",
                stage_effort="high",
            )
        called_def = orch.registry.capture_definition.call_args.args[0]
        assert called_def.model == "claude-opus-x"
        assert called_def.effort == "high"

    def test_codex_default_medium_byte_identical_through_run_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: a `reviewer` (codex) role step with no stage overrides
        must still resolve to `medium` effort via the real CodexRunner path.

        codex-cursor-plugins migration: `codex` is now a PATH-gated plugin
        kind (`plugins/codex.py`), no longer unconditionally present in
        `RUNNER_MAP` -- register it directly here (mirroring what
        `plugins/codex.py`'s `register()` does when active) so this test's
        REAL dispatch through `RunnerRegistry`/`resolve_runner_class`
        resolves `CodexRunner`, independent of whether the `codex` CLI
        binary happens to be on the test host's PATH.
        """
        from hivepilot.registry import RUNNER_MAP, RunnerRegistry

        monkeypatch.setitem(RUNNER_MAP, "codex", CodexRunner)

        orch = _bare_orchestrator()
        orch.registry = RunnerRegistry({})
        prompt_file = tmp_path / "p.md"
        prompt_file.write_text("review this", encoding="utf-8")
        task = TaskConfig(
            description="review",
            role="reviewer",
            engine="native",
            steps=[TaskStep(name="s", runner="codex", prompt_file=str(prompt_file))],
        )
        project = ProjectConfig(path=tmp_path)
        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            orch._execute_task(
                project=project,
                task_name="reviewer",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                simulate=False,
                dry_run=True,
            )
        args = mock_run.call_args.args[0]
        idx = args.index("-c")
        assert args[idx + 1] == "model_reasoning_effort=medium"


# ---------------------------------------------------------------------------
# Dual-model debate path preserved — stage_model/stage_effort never
# short-circuit `len(role.models) > 1`.
# ---------------------------------------------------------------------------


class TestDebatePathPreservedWithStageParams:
    def test_dual_model_role_still_triggers_debate_with_stage_params(self, tmp_path: Path) -> None:
        orch = _bare_orchestrator()
        orch.registry = MagicMock()
        task = TaskConfig(
            description="intake",
            role="ceo",
            engine="native",
            steps=[TaskStep(name="s", runner="opencode", prompt_file="p.md")],
        )
        project = ProjectConfig(path=tmp_path)
        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "run_debate") as mock_debate,
        ):
            orch._execute_task(
                project=project,
                task_name="ceo-intake",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=1,
                simulate=True,
                dry_run=True,
                stage_model="some-model",
                stage_effort="high",
            )
        mock_debate.assert_called_once()
        assert mock_debate.call_args.kwargs["role_name"] == "ceo"
        orch.registry.execute_definition.assert_not_called()
