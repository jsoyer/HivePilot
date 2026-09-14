"""HP-109: draft-only FIX/DERIVED/CAPTURED, CAPTURED gate, idempotent merge key."""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.evidence import ingest_ref
from hivepilot.pass_store import PENDING, decide, inbox
from hivepilot.skill_catalog import SKILL_ID_SIDECAR, SkillCatalog
from hivepilot.skill_evolution import (
    AUTONOMOUS_TOKENS,
    BLOCKED,
    CAPTURED,
    DERIVED,
    DRAFT,
    FIX,
    EvolutionDraft,
    SkillEvolutionError,
    apply_approved,
    assess_captured_eligibility,
    evolution_merge_key,
    find_by_merge_key,
    preview_accept,
    propose,
    propose_fix,
)
from hivepilot.skill_signals import classify_failure, draft_fix_proposal


def _ref(ref_id: str, *, tenant: str = "default", **metadata: object) -> None:
    ingest_ref(
        ref_id=ref_id,
        preview=ref_id,
        tenant=tenant,
        metadata=metadata,
    )


def _fix_attribution(**extra: object):
    payload = {
        "phase_failed": True,
        "causal_event_id": "evt-1",
        "representative_result": "result-1",
        **extra,
    }
    return classify_failure(payload=payload, revision_id="rev-fix")


class TestDraftOnly:
    def test_fix_lands_in_pass_inbox(self) -> None:
        _ref("fix-ev")
        classified = _fix_attribution()
        draft = propose_fix(classified, evidence_refs=["fix-ev"], name="faulty")
        assert draft.status == DRAFT
        assert draft.admissible is True
        assert draft.persisted is True
        assert draft.evolution_type == FIX
        assert draft.origin == "fixed"
        assert draft.payload is not None
        assert draft.payload["draft_only"] is True
        assert draft.payload["applied"] is False
        cards = inbox(kind="skill_evolution")
        assert len(cards) == 1
        assert cards[0].id == draft.proposal_id
        assert cards[0].status == PENDING
        assert cards[0].action == FIX
        assert cards[0].payload["merge_key"] == draft.merge_key

    def test_fix_blocked_without_triple_does_not_persist(self) -> None:
        _ref("fix-ev")
        classified = classify_failure(
            payload={"phase_failed": True, "causal_event_id": "evt-1"},
            revision_id="rev-fix",
        )
        draft = propose_fix(classified, evidence_refs=["fix-ev"])
        assert draft.status == BLOCKED
        assert draft.persisted is False
        assert "representative_result" in draft.missing
        assert inbox(kind="skill_evolution") == []

    def test_fix_blocked_without_evidence_does_not_persist(self) -> None:
        classified = _fix_attribution()
        draft = propose_fix(classified, name="faulty")
        assert draft.status == BLOCKED
        assert draft.persisted is False
        assert "evidence_refs" in draft.missing
        assert inbox(kind="skill_evolution") == []

    def test_draft_fix_proposal_stub_still_non_persisting(self) -> None:
        classified = _fix_attribution()
        described = draft_fix_proposal(classified)
        assert described["status"] == DRAFT
        assert described["admissible"] is True
        assert described["persisted"] is False
        assert described["proposal_id"] == ""
        assert inbox(kind="skill_evolution") == []

    def test_derived_requires_parents(self) -> None:
        _ref("der-ev")
        draft = propose(
            evolution_type=DERIVED,
            name="specialized",
            evidence_refs=["der-ev"],
            files={"SKILL.md": "# specialized\n"},
        )
        assert draft.status == BLOCKED
        assert draft.persisted is False
        assert "parents" in draft.missing

    def test_derived_draft_persists(self) -> None:
        _ref("der-ev")
        draft = propose(
            evolution_type=DERIVED,
            name="specialized",
            parent_logical_ids=["parent-skill"],
            evidence_refs=["der-ev"],
            files={"SKILL.md": "# specialized\n"},
        )
        assert draft.status == DRAFT
        assert draft.persisted is True
        assert draft.origin == "derived"
        assert inbox(kind="skill_evolution")[0].payload["parent_logical_ids"] == ["parent-skill"]


class TestCapturedGate:
    def test_missing_validation_is_blocked(self) -> None:
        _ref("exec-1", run_id="r1", event_type="applied")
        draft = propose(
            evolution_type=CAPTURED,
            name="captured-skill",
            execution_ref="exec-1",
            files={"SKILL.md": "# captured\n"},
        )
        assert draft.status == BLOCKED
        assert draft.independently_validated is False
        assert "independent_validation" in draft.missing
        assert inbox(kind="skill_evolution") == []

    def test_same_ref_is_not_independent(self) -> None:
        _ref("same-1", run_id="r1", event_type="applied")
        eligibility = assess_captured_eligibility(
            execution_ref="same-1",
            validation_ref="same-1",
        )
        assert eligibility.admissible is False
        assert eligibility.reason == "validation_not_independent"
        draft = propose(
            evolution_type=CAPTURED,
            name="captured-skill",
            execution_ref="same-1",
            validation_ref="same-1",
        )
        assert draft.status == BLOCKED
        assert draft.persisted is False

    def test_same_run_completed_is_not_sufficient(self) -> None:
        _ref("exec-2", run_id="r1", event_type="applied")
        _ref("done-2", run_id="r1", event_type="completed")
        draft = propose(
            evolution_type=CAPTURED,
            name="captured-skill",
            execution_ref="exec-2",
            validation_ref="done-2",
        )
        assert draft.status == BLOCKED
        assert draft.reason == "whole_task_success_not_sufficient"
        assert draft.independently_validated is False
        assert inbox(kind="skill_evolution") == []

    def test_caller_flag_is_not_enough(self) -> None:
        _ref("exec-3", run_id="r1", event_type="applied")
        draft = propose(
            evolution_type=CAPTURED,
            name="captured-skill",
            execution_ref="exec-3",
            independently_validated=True,
        )
        assert draft.status == BLOCKED
        assert draft.independently_validated is False
        assert inbox(kind="skill_evolution") == []

    def test_independent_validation_persists(self) -> None:
        _ref("exec-4", run_id="r1", event_type="applied")
        _ref("val-4", run_id="r2", event_type="postcondition")
        draft = propose(
            evolution_type=CAPTURED,
            name="captured-skill",
            execution_ref="exec-4",
            validation_ref="val-4",
            files={"SKILL.md": "# captured\n"},
        )
        assert draft.status == DRAFT
        assert draft.persisted is True
        assert draft.independently_validated is True
        assert draft.origin == "captured"
        card = inbox(kind="skill_evolution")[0]
        assert card.payload["independently_validated"] is True
        assert card.payload["execution_ref"] == "exec-4"
        assert card.payload["validation_ref"] == "val-4"


def _fix_draft() -> EvolutionDraft:
    return propose(
        evolution_type=FIX,
        name="faulty",
        revision_id="rev-fix",
        causal_event_id="evt-1",
        representative_result="result-1",
        evidence_refs=["idem-ev"],
        files={"SKILL.md": "# fix\n"},
    )


def _derived_draft() -> EvolutionDraft:
    return propose(
        evolution_type=DERIVED,
        name="child",
        parent_logical_ids=["parent"],
        evidence_refs=["idem-ev2"],
        files={"SKILL.md": "# child\n"},
    )


class TestMergeKeyIdempotency:
    def test_same_payload_returns_same_card(self) -> None:
        _ref("idem-ev")
        first = _fix_draft()
        second = _fix_draft()
        assert first.proposal_id == second.proposal_id
        assert first.merge_key == second.merge_key
        assert first.payload is not None
        assert first.merge_key == evolution_merge_key(
            evolution_type=FIX,
            name="faulty",
            revision_id="rev-fix",
            causal_event_id="evt-1",
            representative_result="result-1",
            content_hash=first.payload["content_hash"],
        )
        assert len(inbox(kind="skill_evolution", status=None)) == 1
        stored = find_by_merge_key(first.merge_key)
        assert stored is not None
        assert stored.id == first.proposal_id

    def test_idempotent_after_approve(self) -> None:
        _ref("idem-ev2")
        first = _derived_draft()
        decide(first.proposal_id, "approve", actor="reviewer")
        again = _derived_draft()
        assert again.proposal_id == first.proposal_id
        assert again.persisted is True
        assert len(inbox(kind="skill_evolution", status=None)) == 1


class TestNoAutonomousApply:
    def test_autonomous_mode_is_refused(self) -> None:
        _ref("auto-ev")
        for token in sorted(AUTONOMOUS_TOKENS):
            with pytest.raises(SkillEvolutionError, match="autonomous"):
                propose(
                    evolution_type=FIX,
                    name="faulty",
                    revision_id="rev-fix",
                    causal_event_id="evt-1",
                    representative_result="result-1",
                    evidence_refs=["auto-ev"],
                    mode=token,
                )
        with pytest.raises(SkillEvolutionError, match="autonomous"):
            propose(
                evolution_type=FIX,
                name="faulty",
                revision_id="rev-fix",
                causal_event_id="evt-1",
                representative_result="result-1",
                evidence_refs=["auto-ev"],
                auto_apply=True,
            )
        assert inbox(kind="skill_evolution") == []

    def test_propose_does_not_write_skill_files(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills" / "drafted"
        skills.mkdir(parents=True)
        body = "# original\n"
        (skills / "SKILL.md").write_text(body, encoding="utf-8")
        _ref("disk-ev")
        propose(
            evolution_type=FIX,
            name="drafted",
            revision_id="rev-disk",
            causal_event_id="evt-1",
            representative_result="result-1",
            evidence_refs=["disk-ev"],
            files={"SKILL.md": "# mutated\n"},
        )
        assert (skills / "SKILL.md").read_text(encoding="utf-8") == body
        assert not (skills / SKILL_ID_SIDECAR).exists()
        catalog = SkillCatalog()
        assert len(catalog) == 0

    def test_approve_and_apply_hook_do_not_write(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills" / "drafted"
        skills.mkdir(parents=True)
        body = "# original\n"
        (skills / "SKILL.md").write_text(body, encoding="utf-8")
        _ref("disk-ev2")
        draft = propose(
            evolution_type=CAPTURED,
            name="drafted",
            execution_ref="disk-ev2",
            validation_ref="val-disk",
            files={"SKILL.md": "# captured write\n"},
        )
        # validation ref missing → should not have persisted
        assert draft.persisted is False
        _ref("val-disk", run_id="r9", event_type="postcondition")
        draft = propose(
            evolution_type=CAPTURED,
            name="drafted",
            execution_ref="disk-ev2",
            validation_ref="val-disk",
            files={"SKILL.md": "# captured write\n"},
        )
        assert draft.persisted is True
        decide(draft.proposal_id, "approve", actor="reviewer")
        preview = preview_accept(draft.proposal_id)
        assert preview["would_mutate"] is True
        assert preview["reason"] == "ready"
        refusal = apply_approved(draft.proposal_id)
        assert refusal.ok is False
        assert refusal.mutated is False
        assert refusal.code == "skill_root_required"
        assert (skills / "SKILL.md").read_text(encoding="utf-8") == body
        assert not (skills / SKILL_ID_SIDECAR).exists()
        catalog = SkillCatalog()
        assert catalog.get_by_name("drafted") is None

    def test_create_pending_not_submit_stays_pending(self) -> None:
        """Drafts never go through match_auto / submit auto-approve."""
        _ref("stay-ev")
        draft = propose(
            evolution_type=FIX,
            name="faulty",
            revision_id="rev-fix",
            causal_event_id="evt-1",
            representative_result="result-1",
            evidence_refs=["stay-ev"],
        )
        stored = inbox(kind="skill_evolution")[0]
        assert stored.status == PENDING
        assert stored.id == draft.proposal_id
        assert apply_approved(stored.id).mutated is False
