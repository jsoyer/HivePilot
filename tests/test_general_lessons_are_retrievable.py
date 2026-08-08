"""214 lessons were written that nothing could ever read.

Measured on the box after run 412:

    role=(null)          task=noxys                   214
    role=release_manager task=noxys-cos-pr-approval     5

    retrieve_lessons("noxys")                  -> 5
    retrieve_lessons("noxys", role="reviewer") -> 0

The 214 come from the pipeline-end distiller, which passes `role=None` --
correctly, since a distillation covering fifteen stages belongs to no single
role -- and `task=<pipeline>`. Injection then asks per stage, with
`role="reviewer"` and `task="noxys-reviewer"`, and the SQL filters on
equality. The two can never meet.

So the loop closed everywhere except the last step: lessons are distilled,
scored, validated, stored — and never read back. Same family as every other
break found today: the writer stores NULL, the reader tests equality.

The fix is semantic, not a widening. A lesson with **no role** is a GENERAL
lesson: it applies to every role, and filtering it out treats "unattributed"
as "attributed to nobody". Role-specific lessons keep being narrowed exactly
as before — including by task, so a `release_manager` lesson does not leak
into a `reviewer`'s context.
"""

from __future__ import annotations

import pytest

from hivepilot.services import state_service


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
    state_service.init_db()
    return tmp_path


def _lesson(text: str, *, role: str | None, task: str | None, score: float = 0.5) -> None:
    state_service.record_lesson(
        run_id=None,
        project="noxys",
        role=role,
        task=task,
        text=text,
        category="general",
        score=score,
        confidence=None,
        validated=True,
    )


class TestGeneralLessonsReachEveryRole:
    def test_a_null_role_lesson_is_returned_to_a_named_role(self, db) -> None:
        """The production shape: 214 rows with role NULL, invisible to every
        stage because every stage carries a role."""
        _lesson("always grep before planning", role=None, task="noxys")

        rows = state_service.list_ranked_lessons("noxys", role="reviewer", task="noxys-reviewer")

        assert [r["text"] for r in rows] == ["always grep before planning"]

    def test_its_task_does_not_have_to_match_either(self, db) -> None:
        """The pipeline-end distiller stores `task=<pipeline>` while a stage
        asks for `task=<stage>`. A general lesson is general in both axes."""
        _lesson("general", role=None, task="noxys")

        rows = state_service.list_ranked_lessons("noxys", role="cto", task="noxys-cto-review")

        assert len(rows) == 1


class TestSpecificLessonsStayNarrowed:
    def test_another_roles_lesson_is_not_leaked(self, db) -> None:
        """This is the property the filter exists for, and it must survive.
        A `release_manager` lesson must not appear in a `reviewer`'s
        context."""
        _lesson("release-manager only", role="release_manager", task="noxys-cos-pr-approval")

        rows = state_service.list_ranked_lessons("noxys", role="reviewer", task="noxys-reviewer")

        assert rows == []

    def test_its_own_role_still_gets_it(self, db) -> None:
        _lesson("release-manager only", role="release_manager", task="noxys-cos-pr-approval")

        rows = state_service.list_ranked_lessons(
            "noxys", role="release_manager", task="noxys-cos-pr-approval"
        )

        assert len(rows) == 1

    def test_a_matching_role_with_a_different_task_is_excluded(self, db) -> None:
        _lesson("release-manager only", role="release_manager", task="noxys-cos-pr-approval")

        rows = state_service.list_ranked_lessons(
            "noxys", role="release_manager", task="some-other-task"
        )

        assert rows == []


class TestNothingElseChanges:
    def test_unvalidated_rows_are_still_never_surfaced(self, db) -> None:
        """The unconditional `validated=1` filter is the anti-poisoning
        guard. Widening role matching must not touch it."""
        state_service.record_lesson(
            run_id=None,
            project="noxys",
            role=None,
            task="noxys",
            text="not validated",
            category="general",
            score=0.5,
            confidence=None,
            validated=False,
        )

        assert state_service.list_ranked_lessons("noxys", role="reviewer") == []

    def test_another_project_is_still_excluded(self, db) -> None:
        _lesson("noxys general", role=None, task="noxys")

        assert state_service.list_ranked_lessons("other-project", role="reviewer") == []

    def test_ranking_is_still_score_desc(self, db) -> None:
        _lesson("weak", role=None, task="noxys", score=0.5)
        _lesson("strong", role=None, task="noxys", score=0.9)

        rows = state_service.list_ranked_lessons("noxys", role="reviewer")

        assert [r["text"] for r in rows] == ["strong", "weak"]

    def test_an_unfiltered_read_is_unchanged(self, db) -> None:
        _lesson("general", role=None, task="noxys")
        _lesson("specific", role="release_manager", task="noxys-cos-pr-approval")

        assert len(state_service.list_ranked_lessons("noxys")) == 2
