"""HP-50 structured verdict parser: improve-format, block_if, fail-safe."""

from __future__ import annotations

from hivepilot.services.file_ownership import Conflict
from hivepilot.services.structured_verdict import (
    FileLineFinding,
    from_ci_failures,
    from_conflicts,
    parse_block_if,
    parse_findings,
    parse_structured_verdict,
)
from hivepilot.services.verification_service import CheckResult


class TestParseFindings:
    def test_improve_block(self):
        text = """
severity: high
file:line: hivepilot/plugins.py:412
category: security
why: path join without a traversal check
suggested fix: validate the slug
"""
        findings = parse_findings(text)
        assert len(findings) == 1
        assert findings[0].path == "hivepilot/plugins.py"
        assert findings[0].line == 412
        assert findings[0].severity == "high"
        assert findings[0].category == "security"
        assert "traversal" in findings[0].message
        assert findings[0].ref() == "hivepilot/plugins.py:412"

    def test_range_and_loose_bullet(self):
        text = "- hivepilot/cli.py:10-12: unused import"
        findings = parse_findings(text)
        assert findings[0].line == 10
        assert findings[0].line_end == 12
        assert findings[0].ref() == "hivepilot/cli.py:10-12"
        assert "unused import" in findings[0].message

    def test_empty_is_safe(self):
        assert parse_findings("") == ()
        assert parse_findings("no locations here") == ()


class TestParseBlockIf:
    def test_french_and_english_headers(self):
        text = """
je bloque si:
- CI is red
- ownership conflict on hivepilot/**

block_if:
- ignored because previous section ended
"""
        # second header restarts capture; first bullets still collected
        conds = parse_block_if(text)
        assert "CI is red" in conds
        assert any("ownership" in c for c in conds)

    def test_empty(self):
        assert parse_block_if("") == ()
        assert parse_block_if("status: PASS") == ()


class TestParseStructuredVerdict:
    def test_combines_fields(self):
        text = """
status: REQUEST_CHANGES
je bloque si:
- tests fail
severity: medium
file:line: web/src/app.ts:9
why: missing aria label
"""
        verdict = parse_structured_verdict(text, source="review", owner_role="developer")
        assert verdict.owner_role == "developer"
        assert verdict.source == "review"
        assert verdict.block_if == ("tests fail",)
        assert verdict.findings[0].path == "web/src/app.ts"
        assert "Nudge · review → developer" in verdict.to_markdown()
        payload = verdict.to_payload()
        assert payload["findings"][0]["line"] == 9

    def test_unknown_source_falls_back(self):
        v = parse_structured_verdict("x", source="nope", owner_role="cto")
        assert v.source == "review"


class TestBuilders:
    def test_from_ci_failures_skips_green(self):
        results = [
            CheckResult("lint", "passed", 0, "", 0.1),
            CheckResult("tests", "failed", 1, "hivepilot/x.py:3: boom", 0.2),
        ]
        verdict = from_ci_failures(results, owner_role="developer")
        assert verdict.source == "ci"
        assert verdict.decision == "BLOCK"
        assert verdict.block_if == ("tests is not green",)
        assert verdict.findings[0].path == "hivepilot/x.py"
        assert verdict.findings[0].line == 3

    def test_from_conflicts_groups_by_owner(self):
        conflicts = [
            Conflict(path="a.py", owner_role="backend", offending_role="docs"),
            Conflict(path="b.py", owner_role="backend", offending_role="docs"),
            Conflict(path="c.md", owner_role="docs", offending_role="backend"),
        ]
        verdicts = from_conflicts(conflicts)
        owners = {v.owner_role: v for v in verdicts}
        assert set(owners) == {"backend", "docs"}
        assert owners["backend"].decision == "NEEDS_HUMAN"
        assert len(owners["backend"].findings) == 2
        assert owners["backend"].findings[0].category == "ownership"


def test_finding_ref_without_line():
    assert FileLineFinding(path="pytest", line=None, message="failed").ref() == "pytest"
