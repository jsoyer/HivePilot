"""Tests for Sprint 3 (Debate Judge & Consensus PRD) — the flag-gated,
fail-closed verdict -> PR-gate wiring.

Covers:
- `git_service.is_blocking` — the default-deny gate over a judge/arbiter
  `Verdict`: only an explicit approval decision (ACCEPT/ACCEPTED/APPROVE/
  APPROVED) with a present, finite confidence >= threshold proceeds; every
  other shape (None, empty/blank decision, non-approval decision, missing/
  non-finite/below-threshold confidence) blocks.
- `git_service.perform_git_actions`'s `judge_gate_enabled`/`confidence_threshold`
  wiring: flags-off (default) is byte-identical to pre-Sprint-3 behaviour —
  a blocking verdict is IGNORED unless `judge_gate_enabled=True`.
- `agent_report.report_confidence_value` — coercing the free-text
  `AgentReport.confidence` string into a finite float or `None`.
- `state_service.record_verdict` — redacted persistence, queryable back.
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from hivepilot.models import GitActions, ProjectConfig
from hivepilot.orchestrator import Verdict
from hivepilot.services import git_service, state_service
from hivepilot.services.agent_report import AgentReport, report_confidence_value
from hivepilot.services.git_service import is_blocking

MARKER = "VERDICT-GATE-SECRET-MARKER-9f13ac02-DO-NOT-LEAK"


def _report(confidence: str = "") -> AgentReport:
    return AgentReport(
        status="",
        summary=[],
        decisions="",
        blockers="",
        next_handoff="",
        confidence=confidence,
        links=[],
        raw="",
    )


# ---------------------------------------------------------------------------
# is_blocking — direct unit tests over the full negative + positive matrix
# ---------------------------------------------------------------------------


class TestIsBlockingNegativeMatrix:
    def test_none_verdict_blocks(self) -> None:
        assert is_blocking(None, 0.5) is True

    def test_decision_none_blocks(self) -> None:
        assert is_blocking(Verdict(decision=None, confidence=0.9), 0.5) is True

    def test_empty_decision_blocks(self) -> None:
        assert is_blocking(Verdict(decision="", confidence=0.9), 0.5) is True

    def test_whitespace_only_decision_blocks(self) -> None:
        assert is_blocking(Verdict(decision="   ", confidence=0.9), 0.5) is True

    def test_maintain_decision_blocks(self) -> None:
        assert is_blocking(Verdict(decision="MAINTAIN", confidence=0.9), 0.5) is True

    def test_defend_decision_blocks(self) -> None:
        assert is_blocking(Verdict(decision="DEFEND", confidence=0.9), 0.5) is True

    def test_needs_human_decision_blocks(self) -> None:
        assert is_blocking(Verdict(decision="NEEDS_HUMAN", confidence=0.99), 0.5) is True

    def test_decline_decision_blocks(self) -> None:
        assert is_blocking(Verdict(decision="DECLINE", confidence=0.99), 0.5) is True

    def test_confidence_none_blocks(self) -> None:
        assert is_blocking(Verdict(decision="ACCEPT", confidence=None), 0.5) is True

    def test_confidence_nan_blocks(self) -> None:
        assert is_blocking(Verdict(decision="ACCEPT", confidence=float("nan")), 0.5) is True

    def test_confidence_inf_blocks(self) -> None:
        assert is_blocking(Verdict(decision="ACCEPT", confidence=float("inf")), 0.5) is True

    def test_confidence_negative_inf_blocks(self) -> None:
        assert is_blocking(Verdict(decision="ACCEPT", confidence=float("-inf")), 0.5) is True

    def test_confidence_below_threshold_blocks(self) -> None:
        assert is_blocking(Verdict(decision="ACCEPT", confidence=0.2), 0.5) is True

    def test_confidence_bool_blocks(self) -> None:
        """bool is an int subclass in Python -- must be explicitly excluded,
        never treated as a numeric confidence."""
        assert is_blocking(Verdict(decision="ACCEPT", confidence=True), 0.5) is True  # type: ignore[arg-type]

    def test_confidence_zero_is_not_falsy_hole(self) -> None:
        """0.0 confidence must block (below any positive threshold) via the
        real numeric comparison -- not because 0.0 is falsy."""
        assert is_blocking(Verdict(decision="ACCEPT", confidence=0.0), 0.5) is True


class TestIsBlockingPositivePath:
    def test_explicit_accept_above_threshold_proceeds(self) -> None:
        assert is_blocking(Verdict(decision="ACCEPT", confidence=0.9), 0.5) is False

    @pytest.mark.parametrize("decision", ["ACCEPT", "ACCEPTED", "APPROVE", "APPROVED"])
    def test_all_approve_synonyms_proceed(self, decision: str) -> None:
        assert is_blocking(Verdict(decision=decision, confidence=0.9), 0.5) is False

    def test_case_insensitive_decision_proceeds(self) -> None:
        assert is_blocking(Verdict(decision="accept", confidence=0.9), 0.5) is False

    def test_decision_with_whitespace_proceeds(self) -> None:
        assert is_blocking(Verdict(decision="  ACCEPT  ", confidence=0.9), 0.5) is False


class TestIsBlockingBoundary:
    def test_confidence_equal_to_threshold_proceeds(self) -> None:
        """confidence == threshold uses >=, not >, so an exact match proceeds."""
        assert is_blocking(Verdict(decision="ACCEPT", confidence=0.5), 0.5) is False

    def test_confidence_hair_below_threshold_blocks(self) -> None:
        assert is_blocking(Verdict(decision="ACCEPT", confidence=0.4999999), 0.5) is True


# ---------------------------------------------------------------------------
# perform_git_actions -- judge_gate_enabled / confidence_threshold wiring
# ---------------------------------------------------------------------------


def _init_repo(tmp_path: Path) -> ProjectConfig:
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    return ProjectConfig(path=tmp_path)


@pytest.mark.parametrize(
    "verdict",
    [
        None,
        Verdict(decision=None, confidence=0.9),
        Verdict(decision="", confidence=0.9),
        Verdict(decision="MAINTAIN", confidence=0.9),
        Verdict(decision="NEEDS_HUMAN", confidence=0.99),
        Verdict(decision="ACCEPT", confidence=None),
        Verdict(decision="ACCEPT", confidence=float("nan")),
        Verdict(decision="ACCEPT", confidence=float("inf")),
        Verdict(decision="ACCEPT", confidence=0.2),  # below threshold=0.5
    ],
    ids=[
        "none",
        "decision_none",
        "decision_empty",
        "maintain",
        "needs_human",
        "confidence_none",
        "confidence_nan",
        "confidence_inf",
        "confidence_below_threshold",
    ],
)
def test_gate_blocks_promote_and_merge_on_every_negative_verdict(
    tmp_path: Path, verdict: Verdict | None
) -> None:
    project = _init_repo(tmp_path)
    ga = GitActions(promote_pr=True, merge_pr=True)
    with (
        patch("hivepilot.services.git_service.promote_pr") as mock_promote,
        patch("hivepilot.services.git_service.merge_pr") as mock_merge,
    ):
        git_service.perform_git_actions(
            project_name="p",
            project=project,
            git=ga,
            verdict=verdict,
            judge_gate_enabled=True,
            confidence_threshold=0.5,
        )
    mock_promote.assert_not_called()
    mock_merge.assert_not_called()


def test_gate_promotes_and_merges_on_confident_accept(tmp_path: Path) -> None:
    project = _init_repo(tmp_path)
    ga = GitActions(promote_pr=True, merge_pr=True)
    verdict = Verdict(decision="ACCEPT", confidence=0.9)
    with (
        patch("hivepilot.services.git_service.promote_pr") as mock_promote,
        patch("hivepilot.services.git_service.merge_pr") as mock_merge,
    ):
        git_service.perform_git_actions(
            project_name="p",
            project=project,
            git=ga,
            verdict=verdict,
            judge_gate_enabled=True,
            confidence_threshold=0.5,
        )
    mock_promote.assert_called_once()
    mock_merge.assert_called_once()


def test_gate_boundary_confidence_equal_threshold_proceeds(tmp_path: Path) -> None:
    project = _init_repo(tmp_path)
    ga = GitActions(promote_pr=True)
    verdict = Verdict(decision="ACCEPT", confidence=0.5)
    with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
        git_service.perform_git_actions(
            project_name="p",
            project=project,
            git=ga,
            verdict=verdict,
            judge_gate_enabled=True,
            confidence_threshold=0.5,
        )
    mock_promote.assert_called_once()


def test_gate_boundary_confidence_hair_below_threshold_blocks(tmp_path: Path) -> None:
    project = _init_repo(tmp_path)
    ga = GitActions(promote_pr=True)
    verdict = Verdict(decision="ACCEPT", confidence=0.4999999)
    with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
        git_service.perform_git_actions(
            project_name="p",
            project=project,
            git=ga,
            verdict=verdict,
            judge_gate_enabled=True,
            confidence_threshold=0.5,
        )
    mock_promote.assert_not_called()


def test_flag_off_ignores_blocking_verdict_legacy_path_governs(tmp_path: Path) -> None:
    """Backward-compat: judge_gate_enabled=False (the default) must ignore
    even an obviously-blocking verdict -- only the legacy
    `_agent_verdict_blocked(task_result)` path governs. Byte-identical to
    pre-Sprint-3 behaviour."""
    project = _init_repo(tmp_path)
    ga = GitActions(promote_pr=True)
    blocking_verdict = Verdict(decision=None, confidence=None)
    with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
        git_service.perform_git_actions(
            project_name="p",
            project=project,
            git=ga,
            verdict=blocking_verdict,
            # judge_gate_enabled defaults False -- verdict must be ignored
            task_result=None,
        )
    mock_promote.assert_called_once()


def test_flag_off_default_signature_unchanged(tmp_path: Path) -> None:
    """Calling perform_git_actions with ONLY the pre-Sprint-3 kwargs (no
    verdict/judge_gate_enabled/confidence_threshold) must still work exactly
    as before -- these are purely additive."""
    project = _init_repo(tmp_path)
    ga = GitActions(promote_pr=True)
    with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
        git_service.perform_git_actions(project_name="p", project=project, git=ga)
    mock_promote.assert_called_once()


def test_create_pr_never_gated_by_judge_verdict(tmp_path: Path) -> None:
    """create_pr must run even when judge_gate_enabled blocks -- a human must
    still be able to see the review report on the (draft) PR."""
    project = _init_repo(tmp_path)
    ga = GitActions(create_pr=True, promote_pr=True)
    with (
        patch("hivepilot.services.git_service.create_pr") as mock_create,
        patch("hivepilot.services.git_service.promote_pr") as mock_promote,
    ):
        git_service.perform_git_actions(
            project_name="p",
            project=project,
            git=ga,
            verdict=None,
            judge_gate_enabled=True,
            confidence_threshold=0.5,
        )
    mock_create.assert_called_once()
    mock_promote.assert_not_called()


# ---------------------------------------------------------------------------
# report_confidence_value
# ---------------------------------------------------------------------------


class TestReportConfidenceValue:
    def test_valid_numeric_string(self) -> None:
        assert report_confidence_value(_report("0.75")) == 0.75

    def test_empty_string_returns_none(self) -> None:
        assert report_confidence_value(_report("")) is None

    def test_whitespace_only_returns_none(self) -> None:
        assert report_confidence_value(_report("   ")) is None

    def test_unparseable_text_returns_none(self) -> None:
        assert report_confidence_value(_report("abc")) is None

    def test_nan_string_returns_none(self) -> None:
        assert report_confidence_value(_report("nan")) is None

    def test_inf_string_returns_none(self) -> None:
        assert report_confidence_value(_report("inf")) is None

    def test_out_of_range_high_clamps_to_one(self) -> None:
        """Documented design choice: a valid finite number outside [0, 1] is
        CLAMPED (not rejected), mirroring orchestrator._parse_verdict's own
        clamp rule for judge-verdict confidence."""
        assert report_confidence_value(_report("1.5")) == 1.0

    def test_out_of_range_negative_clamps_to_zero(self) -> None:
        assert report_confidence_value(_report("-0.5")) == 0.0

    def test_zero_is_not_none(self) -> None:
        assert report_confidence_value(_report("0")) == 0.0

    def test_result_is_finite(self) -> None:
        value = report_confidence_value(_report("0.42"))
        assert value is not None
        assert math.isfinite(value)


# ---------------------------------------------------------------------------
# state_service.record_verdict -- redacted persistence
# ---------------------------------------------------------------------------


class TestRecordVerdict:
    def test_verdicts_table_exists_after_init_db(self) -> None:
        state_service.init_db()
        with sqlite3.connect(state_service.DB_PATH) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='verdicts'"
            ).fetchone()
        assert row is not None, "verdicts table must be created by init_db()"

    def test_record_and_read_back(self) -> None:
        vid = state_service.record_verdict(
            run_id=None,
            project="demo",
            task="release",
            role="reviewer",
            kind="challenge",
            decision="ACCEPT",
            confidence=0.9,
            summary="looks fine",
        )
        assert isinstance(vid, int)
        rows = state_service.list_recent_verdicts()
        assert any(r["id"] == vid for r in rows)
        row = next(r for r in rows if r["id"] == vid)
        assert row["kind"] == "challenge"
        assert row["decision"] == "ACCEPT"
        assert row["confidence"] == 0.9
        assert row["summary"] == "looks fine"

    def test_summary_is_redacted(self) -> None:
        with patch("hivepilot.services.config_provenance.redact_text") as mock_redact:
            mock_redact.return_value = "[REDACTED]"
            vid = state_service.record_verdict(
                run_id=None,
                project="demo",
                task=None,
                role="reviewer",
                kind="debate",
                decision="ACCEPT",
                confidence=0.9,
                summary=MARKER,
            )
        rows = state_service.list_recent_verdicts()
        row = next(r for r in rows if r["id"] == vid)
        assert row["summary"] == "[REDACTED]"
        assert MARKER not in (row["summary"] or "")

    def test_run_id_foreign_key_column_present(self) -> None:
        run_id = state_service.record_run_start("demo", "release")
        vid = state_service.record_verdict(
            run_id=run_id,
            project="demo",
            task="release",
            role="reviewer",
            kind="challenge",
            decision="ACCEPT",
            confidence=0.9,
        )
        rows = state_service.list_recent_verdicts(run_id=run_id)
        assert any(r["id"] == vid for r in rows)
