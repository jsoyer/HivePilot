"""Integration tests for the per-pipeline `lessons:` YAML wiring
(per-pipeline-lessons-yaml PRD, Sprint 2) -- makes S1's dormant
`resolve_lessons_config` LIVE at the distillation gate + injection call
sites (`hivepilot.orchestrator`, `hivepilot.services.knowledge_service`,
`hivepilot.runners.claude_runner`/`prompt_cli_runner`).

Covers:
- AC6 -- per-pipeline scoping: a pipeline WITH `lessons.enable_distillation
  =True` (global floor OFF) activates injection scoped to its OWN resolved
  config; a sibling pipeline WITHOUT a `lessons:` block sees zero lessons
  behaviour, even against the SAME project/validated-lesson data.
- AC7 -- byte-identical off-path: no `lessons:` block + global flags
  default-off -> `build_lessons_context` returns `""` WITHOUT ever
  importing `state_service`/`lessons_service` (no DB, no query).
- AC8 -- adversarial fail-open sweep (SECURITY CRITICAL): every
  `lessons:` field is attacker-controlled. A pipeline-level block can
  NEVER turn OFF a floor-ON gate (strengthen-only), can never produce an
  injected lesson that is `validated=0` or scores below the RESOLVED
  `min_score`, and `min_score`/`inject_limit` can never degrade to an
  allow-all sentinel via absence/blank/`0`.

Mirrors `tests/test_pipeline_lessons_config.py` (S1's pure resolver
tests) and `tests/test_lessons_injection.py`/`test_lessons_loop_
integration.py`'s wiring/end-to-end idioms, but exercises the NOW-LIVE
consumption sites this Sprint threads the resolver into.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from hivepilot.config import settings
from hivepilot.models import (
    EffectiveLessonsConfig,
    LessonsConfig,
    PipelineConfig,
    ProjectConfig,
    RunnerDefinition,
    TaskStep,
    resolve_lessons_config,
)
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner
from hivepilot.services import state_service
from hivepilot.services.knowledge_service import build_lessons_context
from hivepilot.services.lessons_service import Lesson, OutcomeSignal, validate_lesson


@dataclass
class _FakeFloor:
    """Lightweight settings-floor double (mirrors `test_pipeline_lessons_
    config.py::_FakeFloor`) -- immune to whatever the real `Settings`
    defaults happen to be, so the adversarial sweep pins EXACT floor
    values per case."""

    enable_lesson_distillation: bool = False
    enable_semantic_lesson_retrieval: bool = False
    lesson_distill_runner: str = "claude"
    lesson_distill_model: str | None = None
    lesson_min_score: float = 0.5
    lesson_inject_limit: int = 5


@pytest.fixture(autouse=True)
def _reset_lesson_settings() -> Iterator[None]:
    """Guarantee every opt-in flag/threshold this Sprint reads never leaks
    between tests (mirrors the equivalent fixtures in `tests/test_lessons_
    injection.py` / `tests/test_lessons_loop_integration.py`)."""
    original_flag = settings.enable_lesson_distillation
    original_semantic = settings.enable_semantic_lesson_retrieval
    original_limit = settings.lesson_inject_limit
    original_min_score = settings.lesson_min_score
    yield
    settings.enable_lesson_distillation = original_flag
    settings.enable_semantic_lesson_retrieval = original_semantic
    settings.lesson_inject_limit = original_limit
    settings.lesson_min_score = original_min_score


def _seed_lesson(
    *,
    project: str,
    role: str | None,
    task: str | None,
    text: str,
    score: float,
    validated: bool = True,
) -> int:
    """Persist a lesson row and (optionally) validate it at a caller-chosen
    score -- mirrors `tests/test_lessons_injection.py::_seed_validated_
    lesson`, duplicated locally to keep this Sprint's security-critical
    test file self-contained."""
    run_id = state_service.record_run_start(project, task or "t")
    lesson_id = state_service.record_lesson(
        run_id=run_id,
        project=project,
        role=role,
        task=task,
        text=text,
        score=None,
        confidence=None,
        category="general",
        validated=False,
    )
    if validated:
        state_service.update_lesson_validation(lesson_id, validated=True, score=score)
    return lesson_id


def _claude_payload(
    tmp_path: Path, *, role: str | None, lessons: EffectiveLessonsConfig | None
) -> RunnerPayload:
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata={"role": role} if role is not None else {},
        secrets={},
        lessons=lessons,
    )


def _claude_runner() -> ClaudeRunner:
    return ClaudeRunner(RunnerDefinition(name="claude", kind="claude", command="claude"), settings)


# ---------------------------------------------------------------------------
# AC6 -- per-pipeline scoping
# ---------------------------------------------------------------------------


class TestPerPipelineScoping:
    def test_pipeline_with_block_activates_when_floor_off(self, tmp_path: Path) -> None:
        settings.enable_lesson_distillation = False  # global floor OFF
        _seed_lesson(project="p", role="developer", task="t", text="Ship small PRs.", score=0.9)

        pipeline_on = PipelineConfig(
            description="opted-in pipeline",
            lessons=LessonsConfig(enable_distillation=True),
        )
        resolved_on = resolve_lessons_config(pipeline=pipeline_on)
        assert resolved_on.enable_distillation is True

        text = build_lessons_context("p", "developer", "t", effective=resolved_on)
        assert "Ship small PRs." in text

        payload = _claude_payload(tmp_path, role="developer", lessons=resolved_on)
        prompt = _claude_runner()._build_prompt(payload, "INSTRUCTIONS", None)
        assert "Lessons learned:" in prompt
        assert "Ship small PRs." in prompt

    def test_sibling_pipeline_without_block_sees_zero_lessons(self, tmp_path: Path) -> None:
        """Same project, SAME validated lesson data as the opted-in pipeline
        above -- a sibling pipeline with NO `lessons:` block must behave as
        if the feature doesn't exist at all, even though the floor stays
        off and a validated lesson genuinely exists for this project."""
        settings.enable_lesson_distillation = False
        _seed_lesson(project="p", role="developer", task="t", text="Never appears here.", score=0.9)

        pipeline_off = PipelineConfig(description="untouched pipeline")  # lessons=None
        resolved_off = resolve_lessons_config(pipeline=pipeline_off)
        assert resolved_off.enable_distillation is False

        text = build_lessons_context("p", "developer", "t", effective=resolved_off)
        assert text == ""

        payload = _claude_payload(tmp_path, role="developer", lessons=resolved_off)
        prompt = _claude_runner()._build_prompt(payload, "INSTRUCTIONS", None)
        assert "Lessons learned:" not in prompt
        assert "Never appears here." not in prompt

    def test_floor_on_pipeline_without_block_still_gets_floor_behaviour(
        self, tmp_path: Path
    ) -> None:
        """The floor always applies regardless of a pipeline's own
        `lessons:` block -- a pipeline that says nothing inherits whatever
        the operator mandated fleet-wide."""
        settings.enable_lesson_distillation = True
        _seed_lesson(
            project="p", role="developer", task="t", text="Floor-driven lesson.", score=0.9
        )

        pipeline_off = PipelineConfig(description="untouched pipeline")
        resolved = resolve_lessons_config(pipeline=pipeline_off)
        assert resolved.enable_distillation is True

        text = build_lessons_context("p", "developer", "t", effective=resolved)
        assert "Floor-driven lesson." in text


# ---------------------------------------------------------------------------
# AC7 -- byte-identical off-path (no-import assertion)
# ---------------------------------------------------------------------------


class _RaiseOnAccess:
    """Sentinel that raises on ANY attribute access -- swapped in for
    `hivepilot.services.state_service`/`.lessons_service` on the package
    object so a gate-off `build_lessons_context` call that (incorrectly)
    reached either module's real code would blow up immediately, proving
    the early-return genuinely happens BEFORE those imports/queries."""

    def __getattr__(self, name: str) -> None:
        raise AssertionError(
            f"'{name}' must never be touched on the lessons-distillation gate-off path "
            "(byte-identical dormancy, AC7)"
        )


class TestByteIdenticalOffPath:
    def test_no_block_no_effective_never_imports_state_or_lessons_service(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import hivepilot.services as services_pkg

        settings.enable_lesson_distillation = False  # global floor OFF (default)
        monkeypatch.setattr(services_pkg, "state_service", _RaiseOnAccess())
        monkeypatch.setattr(services_pkg, "lessons_service", _RaiseOnAccess())

        # No `effective=` kwarg at all -- the exact call shape every
        # pre-Sprint-2 caller (and every call site that predates this
        # Sprint) uses.
        result = build_lessons_context("p", "developer", "t")

        assert result == ""

    def test_resolved_floor_off_effective_never_imports_state_or_lessons_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same guarantee when the caller DOES thread a resolved config
        (the orchestrator's real, post-Sprint-2 call shape) -- as long as
        it resolves to `enable_distillation=False`, the import-free
        early-return still holds."""
        import hivepilot.services as services_pkg

        monkeypatch.setattr(services_pkg, "state_service", _RaiseOnAccess())
        monkeypatch.setattr(services_pkg, "lessons_service", _RaiseOnAccess())

        pipeline_off = PipelineConfig(description="d")  # lessons=None
        resolved = resolve_lessons_config(
            floor=_FakeFloor(enable_lesson_distillation=False), pipeline=pipeline_off
        )

        result = build_lessons_context("p", "developer", "t", effective=resolved)

        assert result == ""

    def test_gate_off_prompt_byte_identical_to_pre_feature(self, tmp_path: Path) -> None:
        """No `Lessons learned:` section is ever added to the rendered
        prompt on the gate-off path, whether or not `RunnerPayload.lessons`
        was even set -- proven by comparing the two shapes directly."""
        settings.enable_lesson_distillation = False
        payload_no_lessons_field = _claude_payload(tmp_path, role="developer", lessons=None)
        pipeline_off = PipelineConfig(description="d")
        resolved_off = resolve_lessons_config(pipeline=pipeline_off)
        payload_with_resolved_off = _claude_payload(
            tmp_path, role="developer", lessons=resolved_off
        )

        runner = _claude_runner()
        prompt_a = runner._build_prompt(payload_no_lessons_field, "INSTRUCTIONS", None)
        prompt_b = runner._build_prompt(payload_with_resolved_off, "INSTRUCTIONS", None)

        assert prompt_a == prompt_b
        assert "Lessons learned:" not in prompt_a


# ---------------------------------------------------------------------------
# AC8 -- adversarial fail-open sweep (SECURITY CRITICAL)
# ---------------------------------------------------------------------------

_BLOCK_VARIANTS: dict[str, LessonsConfig | None] = {
    "absent": None,
    "enable_false": LessonsConfig(enable_distillation=False),
    "min_score_low_boundary": LessonsConfig(min_score=0.0001),
    "min_score_high_boundary": LessonsConfig(min_score=1.0),
    "inject_limit_boundary": LessonsConfig(inject_limit=1),
}


class TestAdversarialFailOpenSweep:
    """Cartesian product: floor ON/OFF x every pipeline-block variant
    above. Every combination must uphold the three fail-closed invariants
    from INVARIANTS.md -- treat every `lessons:` field as
    attacker-controlled."""

    @pytest.mark.parametrize("floor_on", [True, False], ids=["floor_on", "floor_off"])
    @pytest.mark.parametrize("block_name", list(_BLOCK_VARIANTS))
    def test_block_never_turns_off_a_floor_on_gate(self, floor_on: bool, block_name: str) -> None:
        floor = _FakeFloor(enable_lesson_distillation=floor_on)
        pipeline = PipelineConfig(description="d", lessons=_BLOCK_VARIANTS[block_name])
        resolved = resolve_lessons_config(floor=floor, pipeline=pipeline)

        if floor_on:
            assert resolved.enable_distillation is True, (
                f"block={block_name!r} illegally turned OFF a floor-ON distillation gate"
            )
        # (a floor-off + non-enabling block staying off is covered by the
        # per-pipeline-scoping tests above -- this test's job is exclusively
        # the strengthen-only direction.)

    @pytest.mark.parametrize("floor_on", [True, False], ids=["floor_on", "floor_off"])
    @pytest.mark.parametrize("block_name", list(_BLOCK_VARIANTS))
    def test_min_score_never_degrades_to_allow_all(self, floor_on: bool, block_name: str) -> None:
        floor = _FakeFloor(enable_lesson_distillation=floor_on, lesson_min_score=0.5)
        pipeline = PipelineConfig(description="d", lessons=_BLOCK_VARIANTS[block_name])
        resolved = resolve_lessons_config(floor=floor, pipeline=pipeline)

        assert resolved.min_score > 0.0, (
            f"block={block_name!r} degraded min_score to a non-positive allow-all sentinel"
        )
        assert resolved.min_score <= 1.0

    @pytest.mark.parametrize("floor_on", [True, False], ids=["floor_on", "floor_off"])
    @pytest.mark.parametrize("block_name", list(_BLOCK_VARIANTS))
    def test_inject_limit_never_degrades_below_one(self, floor_on: bool, block_name: str) -> None:
        floor = _FakeFloor(enable_lesson_distillation=floor_on, lesson_inject_limit=5)
        pipeline = PipelineConfig(description="d", lessons=_BLOCK_VARIANTS[block_name])
        resolved = resolve_lessons_config(floor=floor, pipeline=pipeline)

        assert resolved.inject_limit >= 1, (
            f"block={block_name!r} degraded inject_limit below the hard floor of 1"
        )

    @pytest.mark.parametrize("floor_on", [True, False], ids=["floor_on", "floor_off"])
    @pytest.mark.parametrize("block_name", list(_BLOCK_VARIANTS))
    def test_distill_runner_never_blank(self, floor_on: bool, block_name: str) -> None:
        floor = _FakeFloor(enable_lesson_distillation=floor_on, lesson_distill_runner="claude")
        pipeline = PipelineConfig(description="d", lessons=_BLOCK_VARIANTS[block_name])
        resolved = resolve_lessons_config(floor=floor, pipeline=pipeline)

        assert resolved.distill_runner and resolved.distill_runner.strip(), (
            f"block={block_name!r} degraded distill_runner to blank/empty"
        )

    def test_blank_distill_runner_rejected_at_load_never_reaches_resolver(self) -> None:
        """A blank `distill_runner`/`distill_model` can never even be
        CONSTRUCTED into a `LessonsConfig` -- rejected at YAML-load time
        (pydantic field validator), so it structurally can never reach
        `resolve_lessons_config` as an attacker-controlled blank string."""
        with pytest.raises(ValidationError):
            LessonsConfig(distill_runner="")
        with pytest.raises(ValidationError):
            LessonsConfig(distill_runner="   ")
        with pytest.raises(ValidationError):
            LessonsConfig(distill_model="")

    def test_zero_min_score_rejected_at_load_never_reaches_resolver(self) -> None:
        with pytest.raises(ValidationError):
            LessonsConfig(min_score=0)

    def test_zero_inject_limit_rejected_at_load_never_reaches_resolver(self) -> None:
        with pytest.raises(ValidationError):
            LessonsConfig(inject_limit=0)

    # -- Injection-level: no injected lesson is ever validated=0 or scores
    #    below the RESOLVED min_score --------------------------------------

    def test_unvalidated_lesson_never_injected_regardless_of_resolved_config(
        self,
    ) -> None:
        """Even an EXTREME, maximally-permissive-looking resolved config
        (min_score at its lowest legal boundary, inject_limit huge) must
        never surface an unvalidated candidate -- `state_service.
        list_ranked_lessons` hard-codes `validated=1`, unconditionally,
        independent of anything this Sprint threads through."""
        settings.enable_lesson_distillation = False
        _seed_lesson(
            project="p2",
            role="developer",
            task="t",
            text="Quarantined, must never appear.",
            score=0.99,
            validated=False,
        )
        permissive = EffectiveLessonsConfig(
            enable_distillation=True,
            enable_semantic=False,
            distill_runner="claude",
            distill_model=None,
            min_score=0.0001,
            inject_limit=999,
        )
        text = build_lessons_context("p2", "developer", "t", effective=permissive)
        assert text == ""

    @pytest.mark.parametrize(
        "resolved_min_score", [0.0001, 0.5, 0.9999, 1.0], ids=lambda v: f"min_score={v}"
    )
    def test_validate_lesson_never_validates_below_resolved_min_score(
        self, resolved_min_score: float
    ) -> None:
        lesson = Lesson(text="candidate", category="general")
        # Score derived from a genuine positive signal just BELOW the
        # resolved threshold (when the threshold isn't already at its
        # lowest legal boundary) -- must be rejected.
        just_below = OutcomeSignal(max_verdict_confidence=max(resolved_min_score - 0.00005, 0.0))
        validated, score = validate_lesson(lesson, just_below, min_score=resolved_min_score)
        if score < resolved_min_score:
            assert validated is False

        # Score AT/ABOVE the resolved threshold -- must validate.
        at_or_above = OutcomeSignal(max_verdict_confidence=min(resolved_min_score + 0.0001, 1.0))
        validated2, score2 = validate_lesson(lesson, at_or_above, min_score=resolved_min_score)
        assert score2 >= resolved_min_score
        assert validated2 is True

    def test_empty_outcome_signal_denied_even_at_razor_thin_resolved_min_score(self) -> None:
        """The core anti-poisoning property (S3) re-verified through THIS
        Sprint's threaded `min_score` path: an absent/empty outcome signal
        is DENY regardless of how permissive the resolved `min_score` is --
        never "no constraint -> allow", even at the boundary."""
        lesson = Lesson(text="candidate", category="general")
        validated, score = validate_lesson(lesson, None, min_score=0.0001)
        assert validated is False
        assert score == 0.0

        validated2, score2 = validate_lesson(lesson, OutcomeSignal(), min_score=0.0001)
        assert validated2 is False
        assert score2 == 0.0

    def test_inject_limit_override_actually_caps_injection(self, tmp_path: Path) -> None:
        """A pipeline-resolved `inject_limit` lower than the floor's own
        default must actually cap what's injected -- not just resolve to a
        number that's never applied."""
        settings.enable_lesson_distillation = True
        for i in range(5):
            _seed_lesson(
                project="p3",
                role="developer",
                task="t",
                text=f"lesson-{i}",
                score=0.5 + i * 0.01,
            )
        pipeline = PipelineConfig(description="d", lessons=LessonsConfig(inject_limit=1))
        resolved = resolve_lessons_config(pipeline=pipeline)
        assert resolved.inject_limit == 1

        text = build_lessons_context("p3", "developer", "t", effective=resolved)
        assert text.count("lesson-") == 1
