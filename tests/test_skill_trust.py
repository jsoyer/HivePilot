"""HP-105: provisional↔trusted, enabled orthogonal, promote / demote / review."""

from __future__ import annotations

from hivepilot.pass_store import PENDING, inbox
from hivepilot.skill_catalog import logical_skill_id
from hivepilot.skill_events import record_skill_event, revision_for_skill
from hivepilot.skill_trust import (
    AMBIGUOUS,
    ATTRIBUTED,
    DEFAULT_PROMOTION_THRESHOLD,
    NOT_SKILL,
    PROVISIONAL,
    TRUSTED,
    TRUST_REVIEW_ACTION,
    SkillTrustError,
    classify_attribution,
    evaluate_promotion,
    get,
    is_enabled,
    is_trusted,
    promotion_threshold,
    register_revision,
    report_failure,
    set_enabled,
)


def _register(name: str = "review", files: dict[str, str] | None = None):
    logical, rev = revision_for_skill(name, files or {"SKILL.md": name})
    trust = register_revision(
        revision_id=rev, skill_name=name, logical_id=logical
    )
    return trust, logical, rev


def _complete(name: str, run_id: int | str, *, files: dict[str, str] | None = None):
    logical, rev = revision_for_skill(name, files or {"SKILL.md": name})
    return record_skill_event(
        event_type="completed",
        revision_id=rev,
        logical_id=logical,
        skill_name=name,
        run_id=run_id,
        step="impl",
    )


class TestDefaults:
    def test_new_revision_is_provisional_and_enabled(self) -> None:
        trust, logical, rev = _register("fresh")
        assert trust.known is True
        assert trust.trust_state == PROVISIONAL
        assert trust.enabled is True
        assert trust.trusted is False
        assert trust.logical_id == logical == logical_skill_id("fresh")
        assert get(rev).revision_id == rev

    def test_unknown_is_not_trusted_or_enabled(self) -> None:
        ghost = get("missing-revision")
        assert ghost.known is False
        assert ghost.trusted is False
        assert ghost.enabled is False
        assert is_trusted("missing-revision") is False
        assert is_enabled("missing-revision") is False

    def test_reregister_does_not_reset_trust(self) -> None:
        first, _, rev = _register("stable")
        _complete("stable", 1)
        _complete("stable", 2)
        evaluate_promotion(rev)
        assert get(rev).trusted is True
        again = register_revision(
            revision_id=rev, skill_name="stable", logical_id=first.logical_id
        )
        assert again.trusted is True
        assert again.trust_state == TRUSTED

    def test_set_enabled_refuses_unknown(self) -> None:
        try:
            set_enabled("ghost", True)
        except SkillTrustError as exc:
            assert "not implicitly enabled" in str(exc)
        else:
            raise AssertionError("expected SkillTrustError")


class TestOrthogonality:
    def test_disable_keeps_provisional(self) -> None:
        _, _, rev = _register("ortho")
        disabled = set_enabled(rev, False)
        assert disabled.enabled is False
        assert disabled.trust_state == PROVISIONAL
        assert is_enabled(rev) is False
        assert is_trusted(rev) is False

    def test_trusted_can_be_disabled(self) -> None:
        _, _, rev = _register("live")
        _complete("live", 1)
        _complete("live", 2)
        evaluate_promotion(rev)
        disabled = set_enabled(rev, False)
        assert disabled.trusted is True
        assert disabled.enabled is False
        reenabled = set_enabled(rev, True)
        assert reenabled.trusted is True
        assert reenabled.enabled is True

    def test_promotion_does_not_flip_enabled(self) -> None:
        _, _, rev = _register("held")
        set_enabled(rev, False)
        _complete("held", 1)
        _complete("held", 2)
        decision = evaluate_promotion(rev)
        assert decision.action == "promote"
        assert decision.trust.trusted is True
        assert decision.trust.enabled is False


class TestPromote:
    def test_default_threshold_is_two_inter_runs(self) -> None:
        assert DEFAULT_PROMOTION_THRESHOLD == 2
        assert promotion_threshold() == 2
        _, _, rev = _register("climb")
        _complete("climb", "run-a")
        first = evaluate_promotion(rev)
        assert first.action == "hold"
        assert first.trust.trust_state == PROVISIONAL
        assert first.trust.trust_successes == 1
        _complete("climb", "run-b")
        second = evaluate_promotion(rev)
        assert second.action == "promote"
        assert second.trust.trusted is True
        assert second.trust.trust_successes == 2

    def test_same_run_completed_twice_is_one_success(self) -> None:
        _, _, rev = _register("once")
        _complete("once", 9)
        record_skill_event(
            event_type="completed",
            revision_id=rev,
            logical_id=logical_skill_id("once"),
            skill_name="once",
            run_id=9,
            step="impl",
        )
        evaluate_promotion(rev)
        assert get(rev).trust_successes == 1
        assert get(rev).trust_state == PROVISIONAL

    def test_threshold_is_configurable(self) -> None:
        _, _, rev = _register("slow")
        _complete("slow", 1)
        _complete("slow", 2)
        still = evaluate_promotion(rev, threshold=3)
        assert still.trust.trust_state == PROVISIONAL
        _complete("slow", 3)
        done = evaluate_promotion(rev, threshold=3)
        assert done.action == "promote"
        assert done.trust.trusted is True

    def test_unknown_is_not_promoted_by_events(self) -> None:
        logical, rev = revision_for_skill("ghost", {"SKILL.md": "ghost"})
        record_skill_event(
            event_type="completed",
            revision_id=rev,
            logical_id=logical,
            skill_name="ghost",
            run_id=1,
            step="impl",
        )
        record_skill_event(
            event_type="completed",
            revision_id=rev,
            logical_id=logical,
            skill_name="ghost",
            run_id=2,
            step="impl",
        )
        decision = evaluate_promotion(rev)
        assert decision.action == "unknown"
        assert decision.trust.known is False
        assert decision.trust.trusted is False
        assert decision.trust.enabled is False


class TestDemoteAndReview:
    def _trusted(self, name: str) -> str:
        _, _, rev = _register(name)
        _complete(name, 1)
        _complete(name, 2)
        evaluate_promotion(rev)
        assert get(rev).trusted is True
        return rev

    def test_attributed_failure_demotes(self) -> None:
        rev = self._trusted("fault")
        decision = report_failure(
            revision_id=rev,
            run_id="boom",
            skill_name="fault",
            attribution="attributed",
        )
        assert decision.action == "demote"
        assert decision.attribution == ATTRIBUTED
        assert decision.trust.trust_state == PROVISIONAL
        assert decision.trust.trusted is False
        assert decision.trust.trust_failures == 1
        assert decision.trust.enabled is True

    def test_ambiguous_failure_opens_review_not_demote(self) -> None:
        rev = self._trusted("maybe")
        decision = report_failure(
            revision_id=rev,
            run_id="unclear",
            skill_name="maybe",
        )
        assert decision.action == "review"
        assert decision.attribution == AMBIGUOUS
        assert decision.proposal_id
        assert get(rev).trusted is True
        assert get(rev).trust_failures == 0
        cards = inbox(kind="skill_evolution", status=PENDING)
        match = [row for row in cards if row.id == decision.proposal_id]
        assert len(match) == 1
        assert match[0].action == TRUST_REVIEW_ACTION
        assert match[0].payload["revision_id"] == rev
        assert match[0].payload["run_id"] == "unclear"

    def test_ambiguous_review_is_idempotent(self) -> None:
        rev = self._trusted("dup")
        first = report_failure(revision_id=rev, run_id="same", skill_name="dup")
        second = report_failure(revision_id=rev, run_id="same", skill_name="dup")
        assert first.proposal_id == second.proposal_id
        cards = [
            row
            for row in inbox(kind="skill_evolution", status=PENDING)
            if row.action == TRUST_REVIEW_ACTION
            and row.payload.get("revision_id") == rev
        ]
        assert len(cards) == 1

    def test_not_skill_failure_is_ignored(self) -> None:
        rev = self._trusted("net")
        decision = report_failure(
            revision_id=rev,
            run_id="timeout",
            skill_name="net",
            attribution="network",
        )
        assert decision.action == "ignore"
        assert decision.attribution == NOT_SKILL
        assert get(rev).trusted is True
        assert get(rev).trust_failures == 0
        assert inbox(kind="skill_evolution", status=PENDING) == []

    def test_phase_failed_payload_is_attributed(self) -> None:
        assert classify_attribution(payload={"phase_failed": True}) == ATTRIBUTED
        rev = self._trusted("phase")
        decision = report_failure(
            revision_id=rev,
            run_id="phase-1",
            skill_name="phase",
            payload={"phase_failed": True},
        )
        assert decision.action == "demote"
        assert get(rev).trust_state == PROVISIONAL

    def test_repromotion_needs_fresh_successes_after_failure(self) -> None:
        rev = self._trusted("recover")
        report_failure(
            revision_id=rev, run_id="fail", skill_name="recover", attribution="skill"
        )
        assert get(rev).trust_state == PROVISIONAL
        _complete("recover", 3)
        still = evaluate_promotion(rev)
        assert still.trust.trust_state == PROVISIONAL
        assert still.trust.successes_since_failure == 1
        _complete("recover", 4)
        again = evaluate_promotion(rev)
        assert again.action == "promote"
        assert again.trust.trusted is True
