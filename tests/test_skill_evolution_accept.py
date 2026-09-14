"""HP-111: atomic accept, recovery, idempotent double-accept, stale digest, HITL."""

from __future__ import annotations

import json
from pathlib import Path

from hivepilot.evidence import ingest_ref
from hivepilot.pass_store import PENDING, create_pending, decide, inbox
from hivepilot.skill_catalog import SkillCatalog, snapshot_hash
from hivepilot.skill_evolution import (
    CAPTURED,
    DERIVED,
    FIX,
    EvolutionDraft,
    apply_approved,
    file_diffs,
    lineage_graph,
    preview_accept,
    propose,
)
from hivepilot.skill_evolution_accept import (
    ALREADY_APPLIED,
    BACKUP,
    HITL_REQUIRED,
    JOURNAL_DIR_NAME,
    STAGED,
    STALE_DIGEST,
    VALIDATION_REJECTED,
    recover_accept,
)
from hivepilot.skill_evolution_validator import REJECT


def _ref(ref_id: str, **metadata: object) -> None:
    ingest_ref(ref_id=ref_id, preview=ref_id, metadata=metadata)


def _draft(
    *,
    name: str = "faulty",
    files: dict[str, str] | None = None,
    baseline_files: dict[str, str] | None = None,
    evidence: str = "acc-ev",
) -> EvolutionDraft:
    _ref(evidence)
    return propose(
        evolution_type=FIX,
        name=name,
        revision_id="rev-fix",
        causal_event_id="evt-1",
        representative_result="result-1",
        evidence_refs=[evidence],
        files=files or {"SKILL.md": "# fixed\n", "notes.md": "# note\n"},
        baseline_files=baseline_files,
    )


def _approve(proposal_id: str) -> None:
    decide(proposal_id, "approve", actor="reviewer")


class TestHitlGate:
    def test_accept_without_approve_does_not_write(self, tmp_path: Path) -> None:
        dest = tmp_path / "skills" / "faulty"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("# original\n", encoding="utf-8")
        draft = _draft()
        assert inbox(kind="skill_evolution")[0].status == PENDING
        result = apply_approved(draft.proposal_id, skill_root=dest)
        assert result.ok is False
        assert result.mutated is False
        assert result.code == HITL_REQUIRED
        assert (dest / "SKILL.md").read_text(encoding="utf-8") == "# original\n"
        preview = preview_accept(draft.proposal_id)
        assert preview["would_mutate"] is False
        assert preview["reason"] == "hitl_required"

    def test_rejected_proposal_cannot_accept(self, tmp_path: Path) -> None:
        dest = tmp_path / "skills" / "faulty"
        dest.mkdir(parents=True)
        draft = _draft(evidence="rej-ev")
        decide(draft.proposal_id, "reject", actor="reviewer")
        result = apply_approved(draft.proposal_id, skill_root=dest)
        assert result.ok is False
        assert result.mutated is False
        assert result.code == "rejected_proposal"
        assert not (dest / "notes.md").exists()


class TestAtomicAccept:
    def test_approved_accept_writes_all_files(self, tmp_path: Path) -> None:
        dest = tmp_path / "skills" / "faulty"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("# original\n", encoding="utf-8")
        catalog = SkillCatalog()
        catalog.record(name="faulty", files={"SKILL.md": "# original\n"})
        draft = _draft(baseline_files={"SKILL.md": "# original\n"})
        _approve(draft.proposal_id)
        result = apply_approved(
            draft.proposal_id,
            skill_root=dest,
            catalog=catalog,
            expected_digest=draft.payload["content_hash"],
            actor="reviewer",
        )
        assert result.ok is True
        assert result.mutated is True
        assert result.idempotent is False
        assert (dest / "SKILL.md").read_text(encoding="utf-8") == "# fixed\n"
        assert (dest / "notes.md").read_text(encoding="utf-8") == "# note\n"
        assert not (dest / ".skill_id").exists()
        assert not (tmp_path / "skills" / JOURNAL_DIR_NAME).exists()
        stored = inbox(kind="skill_evolution", status=None)[0]
        assert stored.payload["applied"] is True
        assert stored.payload["applied_digest"] == draft.payload["content_hash"]
        assert catalog.active_revision("faulty").content_hash == draft.payload["content_hash"]

    def test_double_accept_is_idempotent(self, tmp_path: Path) -> None:
        dest = tmp_path / "skills" / "faulty"
        dest.mkdir(parents=True)
        draft = _draft(evidence="idemp-ev")
        _approve(draft.proposal_id)
        first = apply_approved(draft.proposal_id, skill_root=dest)
        second = apply_approved(
            draft.proposal_id,
            skill_root=dest,
            expected_digest=draft.payload["content_hash"],
        )
        assert first.mutated is True
        assert second.ok is True
        assert second.mutated is False
        assert second.idempotent is True
        assert second.code == ALREADY_APPLIED
        assert (dest / "SKILL.md").read_text(encoding="utf-8") == "# fixed\n"

    def test_stale_expected_digest_is_blocked(self, tmp_path: Path) -> None:
        dest = tmp_path / "skills" / "faulty"
        dest.mkdir(parents=True)
        draft = _draft(evidence="stale-ev")
        _approve(draft.proposal_id)
        result = apply_approved(
            draft.proposal_id,
            skill_root=dest,
            expected_digest="not-the-digest",
        )
        assert result.ok is False
        assert result.mutated is False
        assert result.code == STALE_DIGEST
        assert (
            not (dest / "SKILL.md").exists()
            or (dest / "SKILL.md").read_text(encoding="utf-8") != "# fixed\n"
        )

    def test_stale_on_disk_baseline_is_blocked(self, tmp_path: Path) -> None:
        dest = tmp_path / "skills" / "faulty"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("# original\n", encoding="utf-8")
        draft = _draft(
            evidence="disk-stale",
            baseline_files={"SKILL.md": "# original\n"},
        )
        (dest / "SKILL.md").write_text("# moved under us\n", encoding="utf-8")
        _approve(draft.proposal_id)
        result = apply_approved(draft.proposal_id, skill_root=dest)
        assert result.ok is False
        assert result.mutated is False
        assert result.code == STALE_DIGEST
        assert (dest / "SKILL.md").read_text(encoding="utf-8") == "# moved under us\n"


class TestRecovery:
    def test_staged_journal_recovers_on_accept(self, tmp_path: Path) -> None:
        dest = tmp_path / "skills" / "faulty"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("# original\n", encoding="utf-8")
        draft = _draft(
            evidence="rec-ev",
            files={"SKILL.md": "# recovered\n", "extra.md": "x\n"},
            baseline_files={"SKILL.md": "# original\n"},
        )
        _approve(draft.proposal_id)
        journal_dir = tmp_path / "skills" / JOURNAL_DIR_NAME
        staging = journal_dir / "faulty.new"
        staging.mkdir(parents=True)
        (staging / "SKILL.md").write_text("# recovered\n", encoding="utf-8")
        (staging / "extra.md").write_text("x\n", encoding="utf-8")
        (journal_dir / "faulty.json").write_text(
            json.dumps(
                {
                    "state": STAGED,
                    "proposal_id": draft.proposal_id,
                    "content_hash": draft.payload["content_hash"],
                    "dest": str(dest),
                    "files": ["SKILL.md", "extra.md"],
                }
            ),
            encoding="utf-8",
        )
        result = apply_approved(draft.proposal_id, skill_root=dest)
        assert result.ok is True
        assert result.recovered is True
        assert (dest / "SKILL.md").read_text(encoding="utf-8") == "# recovered\n"
        assert (dest / "extra.md").read_text(encoding="utf-8") == "x\n"
        assert not journal_dir.exists()

    def test_backup_journal_finishes_swap(self, tmp_path: Path) -> None:
        dest = tmp_path / "skills" / "faulty"
        journal_dir = tmp_path / "skills" / JOURNAL_DIR_NAME
        staging = journal_dir / "faulty.new"
        backup = journal_dir / "faulty.bak"
        staging.mkdir(parents=True)
        backup.mkdir(parents=True)
        (staging / "SKILL.md").write_text("# swapped\n", encoding="utf-8")
        (backup / "SKILL.md").write_text("# original\n", encoding="utf-8")
        digest = snapshot_hash({"SKILL.md": "# swapped\n"})
        (journal_dir / "faulty.json").write_text(
            json.dumps(
                {
                    "state": BACKUP,
                    "proposal_id": "manual",
                    "content_hash": digest,
                    "dest": str(dest),
                    "files": ["SKILL.md"],
                }
            ),
            encoding="utf-8",
        )
        assert recover_accept(dest) is True
        assert dest.is_dir()
        assert (dest / "SKILL.md").read_text(encoding="utf-8") == "# swapped\n"
        assert not journal_dir.exists()


class TestValidatorHook:
    def test_validate_reject_blocks_accept(self, tmp_path: Path) -> None:
        dest = tmp_path / "skills" / "faulty"
        dest.mkdir(parents=True)
        _ref("bad-ev")
        stored = create_pending(
            kind="skill_evolution",
            action=FIX,
            project="faulty",
            payload={
                "applied": False,
                "content_hash": snapshot_hash({"../escape.md": "x"}),
                "evolution_type": FIX,
                "files": {"../escape.md": "x"},
                "name": "faulty",
                "origin": "fixed",
            },
        )
        decide(stored.id, "approve", actor="reviewer")
        result = apply_approved(stored.id, skill_root=dest)
        assert result.ok is False
        assert result.mutated is False
        assert result.code == VALIDATION_REJECTED
        assert result.validation["result"] == REJECT
        assert list(dest.iterdir()) == []

    def test_needs_human_review_stays_hitl_until_approve(self, tmp_path: Path) -> None:
        dest = tmp_path / "skills" / "faulty"
        dest.mkdir(parents=True)
        _ref("priv-ev")
        draft = propose(
            evolution_type=FIX,
            name="faulty",
            revision_id="rev-fix",
            causal_event_id="evt-1",
            representative_result="result-1",
            evidence_refs=["priv-ev"],
            files={"SKILL.md": ("---\nname: faulty\nallowed-tools:\n  - Bash\n---\n# widen\n")},
        )
        assert draft.payload is not None
        assert draft.payload["validation"]["result"] == "needs_human_review"
        blocked = apply_approved(draft.proposal_id, skill_root=dest)
        assert blocked.code == HITL_REQUIRED
        assert blocked.mutated is False
        _approve(draft.proposal_id)
        accepted = apply_approved(draft.proposal_id, skill_root=dest)
        assert accepted.ok is True
        assert accepted.mutated is True
        assert (dest / "SKILL.md").read_text(encoding="utf-8").startswith("---")


class TestDiffAndLineage:
    def test_multi_file_diff_and_derived_dag(self) -> None:
        _ref("der-ev")
        draft = propose(
            evolution_type=DERIVED,
            name="child",
            parent_logical_ids=["parent-skill"],
            evidence_refs=["der-ev"],
            files={"SKILL.md": "# child\n", "refs.md": "see parent\n"},
            baseline_files={"SKILL.md": "# parent\n"},
        )
        diffs = file_diffs(draft.proposal_id)
        paths = {item["path"] for item in diffs}
        assert paths == {"SKILL.md", "refs.md"}
        assert any("child" in item["unified"] for item in diffs)
        graph = lineage_graph(draft.proposal_id)
        assert any(node["kind"] == "draft" for node in graph["nodes"])
        assert any(node["id"] == "parent-skill" for node in graph["nodes"])
        assert graph["edges"][0]["source"] == "parent-skill"

    def test_captured_lineage_is_a_root(self) -> None:
        _ref("exec-c", run_id="r1", event_type="applied")
        _ref("val-c", run_id="r2", event_type="postcondition")
        draft = propose(
            evolution_type=CAPTURED,
            name="captured-skill",
            execution_ref="exec-c",
            validation_ref="val-c",
            files={"SKILL.md": "# captured\n"},
        )
        graph = lineage_graph(draft.proposal_id)
        assert graph["edges"] == []
        assert len(graph["nodes"]) == 1
        assert graph["nodes"][0]["origin"] == "captured"
