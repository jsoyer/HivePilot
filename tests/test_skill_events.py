"""HP-104: skill-cycle events, idempotency, absence-of-measurement ≠ zero."""

from __future__ import annotations

from hivepilot.services import events
from hivepilot.skill_catalog import logical_skill_id
from hivepilot.skill_events import (
    EVENT_TYPES,
    SkillEventError,
    rank_skills,
    record_cycle,
    record_skill_event,
    revision_for_skill,
    success_rate,
    summarize_skills,
)


def _record(
    *,
    name: str,
    event_type: str,
    run_id: int | str,
    step: str,
    files: dict[str, str] | None = None,
    tenant: str = "default",
):
    logical, rev = revision_for_skill(name, files or {"SKILL.md": name})
    return record_skill_event(
        event_type=event_type,
        revision_id=rev,
        logical_id=logical,
        skill_name=name,
        run_id=run_id,
        step=step,
        tenant=tenant,
    )


class TestEventTypes:
    def test_closed_vocabulary(self) -> None:
        assert EVENT_TYPES == (
            "selected",
            "invoked",
            "applied",
            "completed",
            "fallback",
            "excluded",
        )

    def test_each_type_persists(self) -> None:
        for kind in EVENT_TYPES:
            stored = _record(name="demo", event_type=kind, run_id=1, step="impl")
            assert stored.event_type == kind
            assert stored.skill_name == "demo"
            assert stored.step == "impl"
            assert stored.run_id == "1"

    def test_unknown_type_rejected(self) -> None:
        try:
            _record(name="demo", event_type="trusted", run_id=1, step="impl")
        except SkillEventError as exc:
            assert "unknown skill event type" in str(exc)
        else:
            raise AssertionError("expected SkillEventError")


class TestIdempotency:
    def test_same_key_returns_existing_row(self) -> None:
        first = _record(name="review", event_type="applied", run_id=9, step="s1")
        second = _record(name="review", event_type="applied", run_id=9, step="s1")
        assert first.event_id == second.event_id
        rows = summarize_skills(tenant="default")
        review = next(row for row in rows if row.skill_name == "review")
        assert review.applied == 1

    def test_different_type_same_step_is_a_new_row(self) -> None:
        selected = _record(name="review", event_type="selected", run_id=9, step="s1")
        invoked = _record(name="review", event_type="invoked", run_id=9, step="s1")
        assert selected.event_id != invoked.event_id

    def test_content_change_is_a_new_revision_key(self) -> None:
        first = _record(
            name="review",
            event_type="selected",
            run_id=3,
            step="s1",
            files={"SKILL.md": "v1"},
        )
        second = _record(
            name="review",
            event_type="selected",
            run_id=3,
            step="s1",
            files={"SKILL.md": "v2"},
        )
        assert first.revision_id != second.revision_id
        assert first.logical_id == second.logical_id == logical_skill_id("review")
        assert first.event_id != second.event_id

    def test_bus_emit_only_on_first_insert(self) -> None:
        before = events.latest_change_id()
        _record(name="once", event_type="invoked", run_id=4, step="s1")
        after_first = events.latest_change_id()
        _record(name="once", event_type="invoked", run_id=4, step="s1")
        after_replay = events.latest_change_id()
        assert after_first > before
        assert after_replay == after_first
        kinds = [row["kind"] for row in events.read_since(before)]
        assert kinds.count("skill.invoked") == 1


class TestAbsenceIsNotZero:
    def test_success_rate_none_when_unselected(self) -> None:
        assert success_rate(0, 0) is None
        assert success_rate(0, 4) is None
        assert success_rate(2, 0) == 0.0
        assert success_rate(4, 2) == 0.5

    def test_unmeasured_skill_omitted_from_ranking(self) -> None:
        _record(name="winner", event_type="selected", run_id=1, step="a")
        _record(name="winner", event_type="completed", run_id=1, step="a")
        ranking = rank_skills(tenant="default")
        names = {row.skill_name for row in ranking.top}
        assert "winner" in names
        assert "ghost" not in names
        assert all(row.rate is not None for row in ranking.top)

    def test_excluded_only_is_measured_but_has_no_rate(self) -> None:
        _record(name="skipped", event_type="excluded", run_id=2, step="a")
        stats = {row.skill_name: row for row in summarize_skills(tenant="default")}
        assert stats["skipped"].measured is True
        assert stats["skipped"].rate is None
        ranking = rank_skills(tenant="default")
        assert all(row.skill_name != "skipped" for row in ranking.top)
        assert ranking.unmeasured >= 1

    def test_zero_completions_is_a_real_zero(self) -> None:
        _record(name="flop", event_type="selected", run_id=1, step="a")
        _record(name="flop", event_type="selected", run_id=2, step="a")
        stats = {row.skill_name: row for row in summarize_skills(tenant="default")}
        assert stats["flop"].rate == 0.0
        ranking = rank_skills(tenant="default")
        assert any(row.skill_name == "flop" and row.rate == 0.0 for row in ranking.bottom)

    def test_top_and_bottom_are_measured_only(self) -> None:
        _record(name="best", event_type="selected", run_id=1, step="a")
        _record(name="best", event_type="completed", run_id=1, step="a")
        _record(name="worst", event_type="selected", run_id=1, step="a")
        ranking = rank_skills(tenant="default", limit=5)
        assert ranking.top[0].skill_name == "best"
        assert ranking.top[0].rate == 1.0
        assert ranking.bottom[0].skill_name == "worst"
        assert ranking.bottom[0].rate == 0.0


class TestRecordCycle:
    def test_skips_when_run_id_missing(self) -> None:
        empty = record_cycle(
            [{"name": "x", "files": {}}],
            event_type="selected",
            run_id=None,
            step="s",
        )
        assert empty == []

    def test_records_from_skill_specs(self) -> None:
        rows = record_cycle(
            [{"name": "spec", "files": {"SKILL.md": "body"}}],
            event_type="selected",
            run_id=11,
            step="build",
        )
        assert len(rows) == 1
        assert rows[0].skill_name == "spec"
        assert rows[0].event_type == "selected"
