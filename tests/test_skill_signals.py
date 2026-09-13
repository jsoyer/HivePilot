"""HP-106: failure classes, network must not demote, FIX triple gate."""

from __future__ import annotations

from hivepilot.pass_store import inbox
from hivepilot.skill_events import record_skill_event, revision_for_skill
from hivepilot.skill_signals import (
    AMBIGUOUS,
    ATTRIBUTED,
    ENV,
    NOT_SKILL,
    PERMISSION,
    SKILL_DEFECT,
    TOOL,
    assess_fix_eligibility,
    classify_failure,
    detect_failure_class,
    draft_fix_proposal,
    link_skill_contexts,
)
from hivepilot.skill_trust import (
    PROVISIONAL,
    classify_attribution,
    evaluate_promotion,
    get,
    register_revision,
    report_failure,
)


def _register(name: str, files: dict[str, str] | None = None):
    logical, rev = revision_for_skill(name, files or {"SKILL.md": name})
    trust = register_revision(revision_id=rev, skill_name=name, logical_id=logical)
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


def _invoke(name: str, run_id: int | str, *, files: dict[str, str] | None = None):
    logical, rev = revision_for_skill(name, files or {"SKILL.md": name})
    return record_skill_event(
        event_type="invoked",
        revision_id=rev,
        logical_id=logical,
        skill_name=name,
        run_id=run_id,
        step="impl",
    )


def _trusted(name: str) -> str:
    _, _, rev = _register(name)
    _complete(name, 1)
    _complete(name, 2)
    evaluate_promotion(rev)
    assert get(rev).trusted is True
    return rev


class TestFailureClasses:
    def test_tool_class(self) -> None:
        classified = classify_failure("tool_call_failed")
        assert classified.failure_class == TOOL
        assert classified.trust_attribution == NOT_SKILL
        assert detect_failure_class("tool_error")[0] == TOOL

    def test_env_includes_network(self) -> None:
        classified = classify_failure(
            payload={"error_type": "TimeoutError", "message": "network is unreachable"}
        )
        assert classified.failure_class == ENV
        assert classified.trust_attribution == NOT_SKILL
        assert detect_failure_class("dns")[0] == ENV

    def test_permission_class(self) -> None:
        classified = classify_failure(payload={"permission_status": "denied"})
        assert classified.failure_class == PERMISSION
        assert classified.trust_attribution == NOT_SKILL
        assert detect_failure_class(payload={"error_message": "Permission denied"})[0] == PERMISSION

    def test_skill_defect_class(self) -> None:
        classified = classify_failure(
            payload={"phase_failed": True},
            revision_id="rev-1",
        )
        assert classified.failure_class == SKILL_DEFECT
        assert classified.trust_attribution == ATTRIBUTED
        assert classified.revision_id == "rev-1"

    def test_unrecognized_is_ambiguous(self) -> None:
        classified = classify_failure()
        assert classified.failure_class == ""
        assert classified.trust_attribution == AMBIGUOUS


class TestTrustImpact:
    def test_network_outage_does_not_demote(self) -> None:
        rev = _trusted("netskill")
        decision = report_failure(
            revision_id=rev,
            run_id="outage",
            skill_name="netskill",
            payload={"error_type": "ConnectionError", "message": "connection refused"},
        )
        assert decision.action == "ignore"
        assert decision.attribution == NOT_SKILL
        assert decision.failure_class == ENV
        assert get(rev).trusted is True
        assert get(rev).trust_failures == 0
        assert inbox(kind="skill_evolution") == []

    def test_network_token_still_ignored(self) -> None:
        rev = _trusted("nettoken")
        decision = report_failure(
            revision_id=rev,
            run_id="timeout",
            skill_name="nettoken",
            attribution="network",
        )
        assert decision.action == "ignore"
        assert get(rev).trusted is True

    def test_tool_failure_does_not_demote(self) -> None:
        rev = _trusted("toolish")
        decision = report_failure(
            revision_id=rev,
            run_id="crash",
            skill_name="toolish",
            payload={"error_type": "tool_call_failed"},
        )
        assert decision.action == "ignore"
        assert decision.failure_class == TOOL
        assert get(rev).trusted is True

    def test_permission_failure_does_not_demote(self) -> None:
        rev = _trusted("gated")
        decision = report_failure(
            revision_id=rev,
            run_id="denied",
            skill_name="gated",
            payload={"status": "permission_denied"},
        )
        assert decision.action == "ignore"
        assert decision.failure_class == PERMISSION
        assert get(rev).trusted is True

    def test_env_failure_does_not_demote(self) -> None:
        rev = _trusted("diskful")
        decision = report_failure(
            revision_id=rev,
            run_id="enospc",
            skill_name="diskful",
            attribution="env",
        )
        assert decision.action == "ignore"
        assert decision.failure_class == ENV
        assert get(rev).trusted is True

    def test_skill_defect_demotes(self) -> None:
        rev = _trusted("faulty")
        decision = report_failure(
            revision_id=rev,
            run_id="boom",
            skill_name="faulty",
            payload={"phase_failed": True},
        )
        assert decision.action == "demote"
        assert decision.attribution == ATTRIBUTED
        assert decision.failure_class == SKILL_DEFECT
        assert get(rev).trust_state == PROVISIONAL
        assert get(rev).trusted is False

    def test_network_wins_over_phase_failed(self) -> None:
        rev = _trusted("mixed")
        decision = report_failure(
            revision_id=rev,
            run_id="both",
            skill_name="mixed",
            payload={"phase_failed": True, "error_type": "network"},
        )
        assert decision.action == "ignore"
        assert decision.failure_class == ENV
        assert get(rev).trusted is True


class TestLinkers:
    def test_multiple_invoked_skills_are_ambiguous(self) -> None:
        _invoke("alpha", "shared")
        _invoke("beta", "shared")
        classified = classify_failure(
            payload={"phase_failed": True, "run_id": "shared"},
            run_id="shared",
        )
        assert classified.failure_class == SKILL_DEFECT
        assert classified.trust_attribution == AMBIGUOUS
        assert len(classified.skill_ids) == 2

    def test_explicit_revision_disambiguates(self) -> None:
        alpha = _invoke("alpha2", "shared2")
        _invoke("beta2", "shared2")
        classified = classify_failure(
            payload={"phase_failed": True},
            revision_id=alpha.revision_id,
            run_id="shared2",
        )
        assert classified.trust_attribution == ATTRIBUTED
        assert classified.revision_id == alpha.revision_id
        assert classified.causal_event_id == alpha.event_id

    def test_link_skill_contexts_reads_invoked_events(self) -> None:
        event = _invoke("solo", "run-solo")
        contexts = link_skill_contexts(run_id="run-solo", revision_id=event.revision_id)
        assert len(contexts) == 1
        assert contexts[0].event_id == event.event_id
        assert contexts[0].revision_id == event.revision_id


class TestFixGate:
    def test_fix_requires_revision_causal_and_result(self) -> None:
        classified = classify_failure(
            payload={
                "phase_failed": True,
                "causal_event_id": "evt-1",
                "representative_result": "result-1",
            },
            revision_id="rev-fix",
        )
        eligibility = assess_fix_eligibility(classified)
        assert eligibility.admissible is True
        assert eligibility.revision_id == "rev-fix"
        assert eligibility.causal_event_id == "evt-1"
        assert eligibility.representative_result == "result-1"
        draft = draft_fix_proposal(classified)
        assert draft["status"] == "draft"
        assert draft["admissible"] is True

    def test_fix_blocked_when_result_missing(self) -> None:
        classified = classify_failure(
            payload={"phase_failed": True, "causal_event_id": "evt-1"},
            revision_id="rev-fix",
        )
        eligibility = assess_fix_eligibility(classified)
        assert eligibility.admissible is False
        assert "representative_result" in eligibility.missing
        assert draft_fix_proposal(classified)["status"] == "blocked"

    def test_fix_blocked_for_network(self) -> None:
        classified = classify_failure(
            payload={
                "error_type": "network",
                "causal_event_id": "evt-1",
                "representative_result": "result-1",
            },
            revision_id="rev-net",
        )
        eligibility = assess_fix_eligibility(classified)
        assert eligibility.admissible is False
        assert eligibility.reason == "fix_not_for_env"

    def test_fix_blocked_when_ambiguous(self) -> None:
        eligibility = assess_fix_eligibility(
            revision_id="rev-a",
            causal_event_id="evt-1",
            representative_result="result-1",
            failure_class=SKILL_DEFECT,
            trust_attribution=AMBIGUOUS,
        )
        assert eligibility.admissible is False
        assert eligibility.reason == "fix_not_for_ambiguous"


class TestTrustWrapper:
    def test_classify_attribution_uses_signals(self) -> None:
        assert classify_attribution("network") == NOT_SKILL
        assert classify_attribution(payload={"phase_failed": True}) == ATTRIBUTED
        assert classify_attribution() == AMBIGUOUS
        assert classify_attribution(payload={"error_message": "Permission denied"}) == NOT_SKILL
