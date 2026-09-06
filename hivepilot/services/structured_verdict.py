"""Structured verdict vocabulary (HP-50): decision + « je bloque si » + file:line.

The existing `orchestrator.Verdict` / `_parse_verdict` contract stays untouched.
This module is an additive observer language: CI failures, review blocks, and
file-ownership conflicts become the same shape so a nudge can re-inject them
to the owning role.

Parser is fail-SAFE: unparseable text yields empty `block_if` / `findings`,
never an exception. Unknown severity falls back to `info`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

SOURCES = ("ci", "review", "conflict", "step_failure")
SEVERITIES = ("critical", "high", "medium", "low", "info")
DECISIONS = ("PASS", "BLOCK", "REQUEST_CHANGES", "NEEDS_HUMAN")

_FILE_LINE_RE = re.compile(
    r"(?P<path>[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+):(?P<start>\d+)(?:-(?P<end>\d+))?"
)
_SEVERITY_RE = re.compile(
    r"^\s*severity:\s*(?P<sev>critical|high|medium|low|info)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_FILELINE_FIELD_RE = re.compile(
    r"^\s*file:line:\s*(?P<ref>\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_WHY_RE = re.compile(
    r"^\s*(?:why|message):\s*(?P<why>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_CATEGORY_RE = re.compile(
    r"^\s*category:\s*(?P<cat>\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_BLOCK_IF_HEADER_RE = re.compile(
    r"^(?:block_if|je bloque si)\s*:\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")


@dataclass(frozen=True)
class FileLineFinding:
    """One constat at a file:line (range optional). Never carries file contents."""

    path: str
    line: int | None
    message: str
    severity: str = "info"
    line_end: int | None = None
    category: str | None = None

    def ref(self) -> str:
        if self.line is not None and self.line_end is not None and self.line_end != self.line:
            return f"{self.path}:{self.line}-{self.line_end}"
        if self.line is not None:
            return f"{self.path}:{self.line}"
        return self.path

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class StructuredVerdict:
    """Observer-facing verdict. Distinct from `orchestrator.Verdict`."""

    decision: str
    source: str
    owner_role: str
    block_if: tuple[str, ...]
    findings: tuple[FileLineFinding, ...]
    summary: str

    def to_markdown(self) -> str:
        lines = [
            f"Nudge · {self.source} → {self.owner_role}",
            f"decision: {self.decision}",
        ]
        if self.block_if:
            lines.append("je bloque si:")
            lines.extend(f"- {cond}" for cond in self.block_if)
        if self.findings:
            lines.append("findings:")
            for finding in self.findings:
                extra = f" [{finding.category}]" if finding.category else ""
                lines.append(f"- {finding.severity} {finding.ref()}{extra}: {finding.message}")
        if self.summary:
            lines.append(self.summary)
        return "\n".join(lines)

    def to_payload(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "source": self.source,
            "owner_role": self.owner_role,
            "block_if": list(self.block_if),
            "findings": [finding.to_dict() for finding in self.findings],
            "summary": self.summary,
        }


def _norm_severity(raw: str | None) -> str:
    value = (raw or "info").strip().lower()
    return value if value in SEVERITIES else "info"


def _parse_ref(ref: str) -> tuple[str, int | None, int | None]:
    match = _FILE_LINE_RE.search(ref.strip())
    if not match:
        return ref.strip(), None, None
    start = int(match.group("start"))
    end_raw = match.group("end")
    end = int(end_raw) if end_raw else None
    return match.group("path"), start, end


def parse_findings(text: str) -> tuple[FileLineFinding, ...]:
    """Extract improve-style finding blocks and loose `path:line` bullets."""
    if not text or not text.strip():
        return ()
    found: list[FileLineFinding] = []
    chunks = re.split(r"\n\s*\n", text)
    for chunk in chunks:
        sev_m = _SEVERITY_RE.search(chunk)
        ref_m = _FILELINE_FIELD_RE.search(chunk)
        if sev_m and ref_m:
            path, line, line_end = _parse_ref(ref_m.group("ref"))
            why_m = _WHY_RE.search(chunk)
            cat_m = _CATEGORY_RE.search(chunk)
            found.append(
                FileLineFinding(
                    path=path,
                    line=line,
                    line_end=line_end,
                    severity=_norm_severity(sev_m.group("sev")),
                    message=(why_m.group("why").strip() if why_m else chunk.strip()[:240]),
                    category=(cat_m.group("cat").strip() if cat_m else None),
                )
            )
    if found:
        return tuple(found)
    loose: list[FileLineFinding] = []
    for raw_line in text.splitlines():
        match = _FILE_LINE_RE.search(raw_line)
        if not match:
            continue
        message = raw_line
        bullet = _BULLET_RE.match(raw_line)
        if bullet:
            message = bullet.group(1)
        rest = _FILE_LINE_RE.sub("", message, count=1).strip(" :-")
        loose.append(
            FileLineFinding(
                path=match.group("path"),
                line=int(match.group("start")),
                line_end=int(match.group("end")) if match.group("end") else None,
                message=rest or message.strip(),
            )
        )
    return tuple(loose)


def parse_block_if(text: str) -> tuple[str, ...]:
    """Collect « je bloque si » / `block_if:` bullet conditions."""
    if not text:
        return ()
    conditions: list[str] = []
    capturing = False
    for raw_line in text.splitlines():
        if _BLOCK_IF_HEADER_RE.match(raw_line.strip()):
            capturing = True
            inline = raw_line.split(":", 1)[-1].strip()
            if inline:
                conditions.append(inline)
            continue
        if capturing:
            bullet = _BULLET_RE.match(raw_line)
            if bullet:
                conditions.append(bullet.group(1).strip())
                continue
            if raw_line.strip() == "":
                continue
            capturing = False
    return tuple(conditions)


def parse_structured_verdict(
    text: str,
    *,
    source: str,
    owner_role: str,
    decision: str = "BLOCK",
) -> StructuredVerdict:
    """Parse optional structured fields out of free text. Never raises."""
    src = source if source in SOURCES else "review"
    dec = decision if decision in DECISIONS else "BLOCK"
    body = text or ""
    return StructuredVerdict(
        decision=dec,
        source=src,
        owner_role=owner_role,
        block_if=parse_block_if(body),
        findings=parse_findings(body),
        summary=body.strip()[:2000],
    )


def from_ci_failures(
    results: list[object],
    *,
    owner_role: str,
    summary: str = "",
) -> StructuredVerdict:
    """Build a CI/check nudge from `CheckResult`-like objects (name/output)."""
    findings: list[FileLineFinding] = []
    block_if: list[str] = []
    for result in results:
        name = str(getattr(result, "name", "check") or "check")
        output = str(getattr(result, "output", "") or "")
        outcome = str(getattr(result, "outcome", "failed") or "failed")
        if outcome == "passed":
            continue
        block_if.append(f"{name} is not green")
        parsed = parse_findings(output)
        if parsed:
            findings.extend(parsed)
        else:
            findings.append(
                FileLineFinding(
                    path=name,
                    line=None,
                    severity="high" if outcome == "failed" else "critical",
                    message=(output.strip()[:240] or f"check {name} {outcome}"),
                    category="ci",
                )
            )
    return StructuredVerdict(
        decision="BLOCK",
        source="ci",
        owner_role=owner_role,
        block_if=tuple(block_if),
        findings=tuple(findings),
        summary=summary or f"{len(findings)} blocking check(s) for {owner_role}",
    )


def from_conflicts(conflicts: list[object]) -> list[StructuredVerdict]:
    """One nudge per owner role. Paths only — never file contents."""
    by_owner: dict[str, list[FileLineFinding]] = {}
    for conflict in conflicts:
        owner = str(getattr(conflict, "owner_role", "") or "")
        path = str(getattr(conflict, "path", "") or "")
        offender = str(getattr(conflict, "offending_role", "") or "")
        if not owner or not path:
            continue
        by_owner.setdefault(owner, []).append(
            FileLineFinding(
                path=path,
                line=None,
                severity="high",
                message=f"owned by {owner}, changed by {offender}",
                category="ownership",
            )
        )
    verdicts: list[StructuredVerdict] = []
    for owner, findings in by_owner.items():
        verdicts.append(
            StructuredVerdict(
                decision="NEEDS_HUMAN",
                source="conflict",
                owner_role=owner,
                block_if=(f"{owner}'s owned files are edited by another role",),
                findings=tuple(findings),
                summary=f"{len(findings)} ownership conflict(s) for {owner}",
            )
        )
    return verdicts
