"""HP-50 nudge engine: persist kind=nudge, post to Orchestrateur space, fail-safe."""

from __future__ import annotations

from hivepilot.services import nudge_engine, state_service
from hivepilot.services.file_ownership import Conflict
from hivepilot.services.structured_verdict import StructuredVerdict
from hivepilot.services.verification_service import CheckResult


def test_observe_persists_nudge_and_posts_space():
    verdict = StructuredVerdict(
        decision="BLOCK",
        source="ci",
        owner_role="developer",
        block_if=("tests is not green",),
        findings=(),
        summary="tests failed",
    )
    verdict_id = nudge_engine.observe(verdict, project="atlas", run_id=None)
    assert verdict_id is not None

    rows = state_service.list_recent_verdicts(limit=10)
    nudge_rows = [r for r in rows if r.get("kind") == "nudge"]
    assert len(nudge_rows) == 1
    assert nudge_rows[0]["decision"] == "BLOCK"
    assert nudge_rows[0]["role"] == "developer"
    assert "je bloque si" in (nudge_rows[0].get("summary") or "")
    assert nudge_rows[0].get("findings_json")
    assert "tests is not green" in (nudge_rows[0].get("block_if_json") or "")

    spaces = state_service.list_spaces("default")
    titles = [s.get("title") for s in spaces]
    assert any(t and "atlas" in t for t in titles)
    space_id = next(s["id"] for s in spaces if s.get("title") and "atlas" in s["title"])
    messages = state_service.list_space_messages(space_id)
    assert any("Nudge · ci → developer" in (m.get("body") or "") for m in messages)


def test_observe_ci_skips_all_green():
    results = [CheckResult("lint", "passed", 0, "", 0.1)]
    assert nudge_engine.observe_ci(results, project="p", owner_role="developer") is None
    assert [r for r in state_service.list_recent_verdicts() if r.get("kind") == "nudge"] == []


def test_observe_ci_posts_on_failure():
    results = [CheckResult("tests", "failed", 1, "hivepilot/x.py:8: boom", 0.2)]
    verdict_id = nudge_engine.observe_ci(
        results, project="acme", owner_role="developer", summary="tests failed"
    )
    assert verdict_id is not None
    row = next(r for r in state_service.list_recent_verdicts() if r.get("kind") == "nudge")
    assert "hivepilot/x.py:8" in (row.get("summary") or "")


def test_observe_review_skips_pass():
    assert (
        nudge_engine.observe_review(project="p", owner_role="developer", decision="PASS", text="ok")
        is None
    )


def test_observe_review_parses_findings():
    text = """
reviewer: REQUEST_CHANGES
je bloque si:
- missing tests
severity: high
file:line: hivepilot/cli.py:20
why: no coverage
"""
    verdict_id = nudge_engine.observe_review(
        project="p", owner_role="developer", decision="REQUEST_CHANGES", text=text
    )
    assert verdict_id is not None
    row = next(r for r in state_service.list_recent_verdicts() if r.get("kind") == "nudge")
    assert row["decision"] == "REQUEST_CHANGES"
    assert "hivepilot/cli.py:20" in (row.get("summary") or "")


def test_observe_conflicts_one_per_owner():
    ids = nudge_engine.observe_conflicts(
        [
            Conflict(path="a.py", owner_role="backend", offending_role="docs"),
            Conflict(path="c.md", owner_role="docs", offending_role="backend"),
        ],
        project="fleet",
    )
    assert len(ids) == 2
    roles = {r["role"] for r in state_service.list_recent_verdicts() if r.get("kind") == "nudge"}
    assert roles == {"backend", "docs"}


def test_observe_never_raises(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(state_service, "record_verdict", boom)
    verdict = StructuredVerdict(
        decision="BLOCK",
        source="ci",
        owner_role="developer",
        block_if=(),
        findings=(),
        summary="x",
    )
    assert nudge_engine.observe(verdict, project="p") is None
