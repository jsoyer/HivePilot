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
  redacts the raw response before parsing.
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
            verdicts=[],
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
            verdicts=[],
            interactions=[],
            outcomes=[],
            distiller_def=distiller_def,
            capture_fn=capture_fn,
        )
        assert lessons == []
