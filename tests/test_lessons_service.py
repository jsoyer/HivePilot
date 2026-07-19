"""
Unit tests for `hivepilot.services.lessons_service` (Auto-Learning Lessons
Loop PRD, Sprint 2) — the pure prompt-building / JSON-parsing / definition-
building helpers, plus `distill_lessons`'s ONE `capture_fn` call with a
mocked capture function (no real Orchestrator/RunnerRegistry needed).

Covers:
- `parse_distilled_lessons`: well-formed list, fenced ```json block,
  malformed/non-JSON/non-list/empty -> [], per-item validation (missing
  text dropped, bad id types dropped without invalidating the element,
  category default, self-reported score/confidence ignored).
- `build_distiller_definition`: runner/model passthrough.
- `distill_lessons`: builds ONE prompt, calls `capture_fn` exactly once,
  redacts the FULL PROMPT before it ever reaches `capture_fn` (egress choke
  point — a HIGH-severity fix: `outcomes[].detail` is sourced from
  `RunResult.detail`, a field known to reach other sinks in cleartext, so
  without this a secret would be sent verbatim to the external distiller
  model even though the response redaction never let it back into the DB),
  redacts the raw response before parsing, and skips the `capture_fn` call
  entirely when there's no verdict/interaction signal to distill from.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from hivepilot.models import ProjectConfig
from hivepilot.services.lessons_service import (
    Lesson,
    build_distiller_definition,
    distill_lessons,
    parse_distilled_lessons,
)


class TestParseDistilledLessons:
    def test_well_formed_list_parses(self) -> None:
        raw = (
            '[{"text": "Always run tests before merging.", "category": "testing", '
            '"source_verdict_id": 3, "source_interaction_id": null}]'
        )
        lessons = parse_distilled_lessons(raw)
        assert lessons == [
            Lesson(
                text="Always run tests before merging.",
                category="testing",
                source_verdict_id=3,
                source_interaction_id=None,
            )
        ]

    def test_fenced_json_block_tolerated(self) -> None:
        raw = '```json\n[{"text": "Fenced lesson.", "category": "ops"}]\n```'
        lessons = parse_distilled_lessons(raw)
        assert len(lessons) == 1
        assert lessons[0].text == "Fenced lesson."
        assert lessons[0].category == "ops"

    def test_empty_string_returns_empty_list(self) -> None:
        assert parse_distilled_lessons("") == []
        assert parse_distilled_lessons("   ") == []

    def test_non_json_returns_empty_list(self) -> None:
        assert parse_distilled_lessons("not json at all") == []

    def test_non_list_json_returns_empty_list(self) -> None:
        assert parse_distilled_lessons('{"text": "not a list"}') == []

    def test_empty_array_returns_empty_list(self) -> None:
        assert parse_distilled_lessons("[]") == []

    def test_item_missing_text_is_dropped(self) -> None:
        raw = '[{"category": "testing"}, {"text": "Valid one.", "category": "ops"}]'
        lessons = parse_distilled_lessons(raw)
        assert len(lessons) == 1
        assert lessons[0].text == "Valid one."

    def test_item_with_blank_text_is_dropped(self) -> None:
        raw = '[{"text": "   ", "category": "testing"}]'
        assert parse_distilled_lessons(raw) == []

    def test_missing_category_defaults_to_general(self) -> None:
        raw = '[{"text": "No category given."}]'
        lessons = parse_distilled_lessons(raw)
        assert lessons[0].category == "general"

    def test_bad_source_id_types_are_dropped_not_fatal(self) -> None:
        raw = '[{"text": "Bad ids.", "source_verdict_id": "not-an-int", "source_interaction_id": true}]'
        lessons = parse_distilled_lessons(raw)
        assert len(lessons) == 1
        assert lessons[0].source_verdict_id is None
        assert lessons[0].source_interaction_id is None

    def test_self_reported_score_and_confidence_are_ignored(self) -> None:
        """The distiller proposes TEXT/category ONLY — this parser must never
        surface a self-reported score/confidence; `Lesson` has no such
        fields at all."""
        raw = '[{"text": "Trust me.", "category": "x", "score": 0.99, "confidence": 1.0}]'
        lessons = parse_distilled_lessons(raw)
        assert len(lessons) == 1
        assert not hasattr(lessons[0], "score")
        assert not hasattr(lessons[0], "confidence")


class TestBuildDistillerDefinition:
    def test_runner_and_model_passthrough(self) -> None:
        d = build_distiller_definition(runner="claude", model="opus")
        assert d.kind == "claude"
        assert d.model == "opus"

    def test_none_model_passthrough(self) -> None:
        d = build_distiller_definition(runner="claude", model=None)
        assert d.model is None


class TestDistillLessons:
    _SAMPLE_VERDICT = {
        "id": 1,
        "kind": "debate",
        "decision": "adopt",
        "confidence": 0.8,
        "summary": "s",
    }

    def test_calls_capture_fn_once_and_parses_result(self, monkeypatch) -> None:
        from hivepilot.services import config_provenance

        config_provenance.clear_secret_values()
        project = ProjectConfig(path=Path("/tmp/p"))
        distiller_def = build_distiller_definition(runner="claude", model=None)
        capture_fn = MagicMock(
            return_value='[{"text": "Distilled lesson.", "category": "testing"}]'
        )

        lessons = distill_lessons(
            run_id=1,
            project=project,
            role="developer",
            task="my-task",
            verdicts=[self._SAMPLE_VERDICT],
            interactions=[],
            outcomes=[{"project": "p", "target": "developer", "success": True, "detail": "ok"}],
            distiller_def=distiller_def,
            capture_fn=capture_fn,
        )

        assert capture_fn.call_count == 1
        assert len(lessons) == 1
        assert lessons[0].text == "Distilled lesson."

    def test_malformed_capture_output_returns_empty_list(self) -> None:
        project = ProjectConfig(path=Path("/tmp/p"))
        distiller_def = build_distiller_definition(runner="claude", model=None)
        capture_fn = MagicMock(return_value="not json")

        lessons = distill_lessons(
            run_id=None,
            project=project,
            verdicts=[self._SAMPLE_VERDICT],
            interactions=[],
            outcomes=[],
            distiller_def=distiller_def,
            capture_fn=capture_fn,
        )
        assert lessons == []
        # Confirms this is exercising the malformed-response parse path (not
        # the no-signal short-circuit below) -- the call DID happen.
        assert capture_fn.call_count == 1

    def test_no_signal_skips_capture_fn_call(self) -> None:
        """No verdicts AND no interactions -> the costed `capture_fn` call
        is skipped entirely, even when outcomes are present (Sprint 2 review
        finding, LOW: an outcome-only run has near-zero distillation
        signal)."""
        project = ProjectConfig(path=Path("/tmp/p"))
        distiller_def = build_distiller_definition(runner="claude", model=None)
        capture_fn = MagicMock(
            return_value='[{"text": "Should never be reached.", "category": "x"}]'
        )

        lessons = distill_lessons(
            run_id=1,
            project=project,
            verdicts=[],
            interactions=[],
            outcomes=[{"project": "p", "target": "developer", "success": True, "detail": "ok"}],
            distiller_def=distiller_def,
            capture_fn=capture_fn,
        )

        capture_fn.assert_not_called()
        assert lessons == []

    def test_prompt_is_redacted_before_reaching_capture_fn(self) -> None:
        """HIGH-severity fix: `outcomes[].detail` (sourced from
        `RunResult.detail`, NOT pre-redacted upstream) can carry a resolved
        `${secret:NAME}` value. Before this fix only the *response* was
        redacted -- the secret still left the trust boundary via the
        *prompt* sent to `capture_fn` (i.e. to the external distiller
        model). Assert the live secret literal never reaches `capture_fn`'s
        payload at all."""
        from hivepilot.services import config_provenance

        config_provenance.clear_secret_values()
        config_provenance.register_secret_value("sk-live-outcome-secret")
        try:
            project = ProjectConfig(path=Path("/tmp/p"))
            distiller_def = build_distiller_definition(runner="claude", model=None)
            captured_payloads: list = []

            def _spy_capture(_definition, payload):
                captured_payloads.append(payload)
                return "[]"

            distill_lessons(
                run_id=1,
                project=project,
                role="developer",
                task="my-task",
                verdicts=[self._SAMPLE_VERDICT],
                interactions=[],
                outcomes=[
                    {
                        "project": "p",
                        "target": "developer",
                        "success": False,
                        "detail": "step failed: leaked sk-live-outcome-secret in output",
                    }
                ],
                distiller_def=distiller_def,
                capture_fn=_spy_capture,
            )

            assert len(captured_payloads) == 1
            prompt_sent = captured_payloads[0].metadata["extra_prompt"]
            assert "sk-live-outcome-secret" not in prompt_sent
            assert "REDACTED" in prompt_sent
        finally:
            config_provenance.clear_secret_values()
