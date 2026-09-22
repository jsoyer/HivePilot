"""HP-122 — every human decision traces run_id / step / approval_id to the verdict it consumed.

The inferred ``pipeline_run_id`` join stays empty when a review and a human
gate never share a run. These fixtures cover the explicit link, and both
doctor findings: « décisions sans verdict » and « verdicts sans décision ».
"""

from __future__ import annotations

from typing import cast

from hivepilot.orchestrator import Orchestrator, RunResult
from hivepilot.services import state_service, verdict_hitl
from hivepilot.services.doctor_liveness import check_verdict_hitl_join


def _verdict(run_id: int, decision: str | None, *, pipeline_run_id: int | None = None) -> int:
    return state_service.record_verdict(
        run_id=run_id,
        pipeline_run_id=pipeline_run_id,
        project="demo",
        task="ship",
        role="review",
        kind="review",
        decision=decision,
        confidence=1.0 if decision else None,
        summary="s",
    )


def _checks(findings) -> list[str]:
    return [finding.check for finding in findings]


class TestConsumedVerdictJoin:
    def test_decision_traces_run_step_and_approval_to_the_consumed_verdict(self) -> None:
        run = state_service.record_run_start("demo", "ship")
        other = state_service.record_run_start("demo", "other")
        _verdict(other, "ACCEPT", pipeline_run_id=other)
        blocked = _verdict(run, "BLOCKED", pipeline_run_id=run)
        accepted = _verdict(run, "ACCEPT", pipeline_run_id=run)

        traced = verdict_hitl.record_verdict_hitl_link(
            run_id=run,
            step="review",
            approval_id="ap-1",
            decision="approve",
            actor="alice",
        )

        assert traced["run_id"] == run
        assert traced["step"] == "review"
        assert traced["approval_id"] == "ap-1"
        assert traced["decision"] == "approve"
        assert traced["verdict_id"] == accepted
        assert traced["verdict"]["decision"] == "ACCEPT"
        again = verdict_hitl.trace_hitl_decision(run, "review", "ap-1")
        assert again == traced

        pinned = verdict_hitl.record_verdict_hitl_link(
            run_id=run,
            step="review",
            approval_id="ap-2",
            decision="reject",
            actor="alice",
            verdict_id=blocked,
        )
        assert pinned["decision"] == "reject"
        assert pinned["verdict_id"] == blocked
        assert pinned["verdict"]["decision"] == "BLOCKED"

    def test_edit_uses_the_same_trace_keys(self) -> None:
        run = state_service.record_run_start("demo", "ship")
        verdict_id = _verdict(run, "REQUEST_CHANGES", pipeline_run_id=run)

        traced = verdict_hitl.record_verdict_hitl_link(
            run_id=run,
            step="plan",
            approval_id="ap-edit",
            decision="edited",
            actor="alice",
        )

        assert traced["decision"] == "edit"
        assert traced["verdict_id"] == verdict_id
        assert verdict_hitl.trace_hitl_decision(run, "plan", "ap-edit")["verdict_id"] == verdict_id


class TestDoctorFindings:
    def test_decisions_sans_verdict(self) -> None:
        bare = state_service.record_run_start("demo", "ship")
        verdict_hitl.record_verdict_hitl_link(
            run_id=bare,
            step="deploy",
            approval_id="ap-bare",
            decision="approve",
            actor="alice",
        )
        orphan = state_service.record_run_start("demo", "ship")
        state_service.record_approval_request(
            orphan,
            "demo",
            "ship",
            {"kind": "step_checkpoint", "step_name": "apply", "approval_id": "ap-apply"},
        )
        state_service.update_approval(orphan, "approved", "bob")
        ruled = state_service.record_run_start("demo", "ship")
        state_service.record_approval_request(ruled, "demo", "ship", {"task": "ship"})
        state_service.update_approval(ruled, "denied", "rule")

        rows = verdict_hitl.decisions_without_verdict()
        assert {(row["run_id"], row["step"], row["approval_id"]) for row in rows} == {
            (bare, "deploy", "ap-bare"),
            (orphan, "apply", "ap-apply"),
        }
        assert ruled not in {row["run_id"] for row in rows}

        findings = check_verdict_hitl_join()
        assert _checks(findings) == ["décisions sans verdict"]
        assert findings[0].severity == "warning"
        assert "décisions sans verdict" in findings[0].message
        assert "ap-bare" in findings[0].message
        assert "ap-apply" in findings[0].message

    def test_verdicts_sans_decision(self) -> None:
        run = state_service.record_run_start("demo", "ship")
        verdict_id = _verdict(run, "BLOCKED", pipeline_run_id=run)
        _verdict(run, None, pipeline_run_id=run)

        rows = verdict_hitl.verdicts_without_decision()
        assert [row["id"] for row in rows] == [verdict_id]

        findings = check_verdict_hitl_join()
        assert _checks(findings) == ["verdicts sans décision"]
        assert findings[0].severity == "warning"
        assert "verdicts sans décision" in findings[0].message
        assert f"verdict {verdict_id}" in findings[0].message

    def test_a_joined_pair_is_absent_from_both_findings(self) -> None:
        joined = state_service.record_run_start("demo", "ship")
        consumed = _verdict(joined, "BLOCKED", pipeline_run_id=joined)
        verdict_hitl.record_verdict_hitl_link(
            run_id=joined,
            step="review",
            approval_id="ap-ok",
            decision="approve",
            actor="alice",
        )
        bare = state_service.record_run_start("demo", "ship")
        verdict_hitl.record_verdict_hitl_link(
            run_id=bare,
            step="deploy",
            approval_id="ap-gap",
            decision="reject",
            actor="alice",
        )
        loose = state_service.record_run_start("demo", "ship")
        loose_verdict = _verdict(loose, "ACCEPT", pipeline_run_id=loose)

        assert joined not in {row["run_id"] for row in verdict_hitl.decisions_without_verdict()}
        assert consumed not in {row["id"] for row in verdict_hitl.verdicts_without_decision()}
        assert bare in {row["run_id"] for row in verdict_hitl.decisions_without_verdict()}
        assert loose_verdict in {row["id"] for row in verdict_hitl.verdicts_without_decision()}

        assert _checks(check_verdict_hitl_join()) == [
            "décisions sans verdict",
            "verdicts sans décision",
        ]


class TestApproveRunWritesTheLink:
    def test_deny_consumes_the_verdict_that_existed_before_dispatch(self) -> None:
        run = state_service.record_run_start("demo", "ship")
        blocked = _verdict(run, "BLOCKED", pipeline_run_id=run)
        state_service.record_approval_request(
            run,
            "demo",
            "ship",
            {"kind": "step_checkpoint", "step_name": "deploy", "approval_id": "ap-deploy"},
        )

        class _Deny:
            later: int

            def run_approved(self, *, run_id: int, approve: bool, approver: str, reason=None):
                # A verdict written while the gate resumes is not the one
                # the human consumed.
                self.later = _verdict(run_id, "ACCEPT", pipeline_run_id=run_id)
                state_service.update_approval(run_id, "approved" if approve else "denied", approver)
                return RunResult("demo", "ship", approve)

        orch = _Deny()
        result = Orchestrator.approve_run(
            cast(Orchestrator, orch),
            run_id=run,
            approve=False,
            approver="alice",
        )

        assert result.success is False
        traced = verdict_hitl.trace_hitl_decision(run, "deploy", "ap-deploy")
        assert traced is not None
        assert traced["decision"] == "reject"
        assert traced["actor"] == "alice"
        assert traced["verdict_id"] == blocked
        findings = check_verdict_hitl_join()
        assert _checks(findings) == ["verdicts sans décision"]
        assert f"verdict {orch.later}" in findings[0].message
        assert f"verdict {blocked}" not in findings[0].message
