"""How often the agents' verdicts matched what the human then decided.

Why this is a service and not a number
--------------------------------------
The autonomy ladder's premise is simple: grant a role more autonomy once its
verdicts have agreed with the operator often enough, on enough occasions. The
premise is only as good as the measurement, and this measurement was already
wrong once in a way that looked right.

``agreement_rows`` joins verdicts to approvals on the pipeline run id. That
join was fixed to return rows -- and on the production database it still
returns **zero**, for a reason the fix could not address::

    approvals   run ids 243 .. 321   (10 rows, all "approved")
    verdicts    run ids 438 .. 467   (26 rows, 7 carrying a decision)

The two populations do not overlap at all. Verdicts come from the adversarial
review; approvals come from the destructive-step gate. No single run has
produced both. A perfect join key changes nothing. (Every verdict also carries
a NULL ``pipeline_run_id``: they predate the column.)

So the report says that, in as many words. A rate of ``0%`` from zero rows and
a rate of ``0%`` from two hundred rows are opposite findings, and a metric that
renders them identically is worse than no metric: it invites a decision about
autonomy to be made on an artefact of an empty table.

The rungs
---------
Nothing here grants anything. It reports what the evidence supports; changing
what a role is allowed to do stays an operator action, taken against this
report. An automated authority increase driven by a self-reported agreement
rate has no outside check on it at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Below this many comparable observations, no rung is reported for a role --
#: not a low one, none. Eight agreements out of eight is not evidence of
#: anything, and a ladder that reads it as a perfect score promotes on noise.
MIN_SAMPLE = 20

#: Verdict decisions meaning "the agents see no reason to stop".
PROCEED_DECISIONS: frozenset[str] = frozenset(
    {"ACCEPT", "ACCEPTED", "APPROVE", "APPROVED", "PASS", "PASSED", "OK", "CLEARED"}
)

#: Verdict decisions that stop a release. Deliberately the same vocabulary the
#: release gate enforces -- a decision that blocks there and reads as agreement
#: here would make the ladder measure something other than the gate it governs.
STOP_DECISIONS: frozenset[str] = frozenset(
    {
        "BLOCK",
        "BLOCKED",
        "REJECT",
        "REJECTED",
        "REQUEST_CHANGES",
        "CHANGES_REQUESTED",
        "NEEDS_HUMAN",
        "FAIL",
        "FAILED",
        "DENY",
        "DENIED",
    }
)

#: What the human did. Anything else -- "pending" above all -- is not a
#: decision, and never counts as either agreement or disagreement.
HUMAN_PROCEED: frozenset[str] = frozenset({"approved"})
HUMAN_STOP: frozenset[str] = frozenset({"rejected", "denied", "declined"})


@dataclass(frozen=True)
class RoleAgreement:
    """One role's record, with the sample size kept beside the rate.

    ``unclassifiable`` is reported rather than dropped: a verdict whose
    decision is in neither vocabulary is a gap in this mapping, and hiding it
    would quietly shrink the denominator and flatter the role.
    """

    role: str
    agreed: int
    disagreed: int
    unclassifiable: int

    @property
    def comparable(self) -> int:
        return self.agreed + self.disagreed

    @property
    def rate(self) -> float | None:
        """Agreement rate, or ``None`` when there is nothing to divide by."""
        if self.comparable == 0:
            return None
        return self.agreed / self.comparable

    @property
    def has_enough_evidence(self) -> bool:
        return self.comparable >= MIN_SAMPLE


@dataclass(frozen=True)
class AgreementReport:
    """Per-role records plus, when there are none, why there are none."""

    roles: list[RoleAgreement]
    rows_joined: int
    empty_reason: str | None

    @property
    def measurable(self) -> bool:
        return any(role.has_enough_evidence for role in self.roles)


def _classify(decision: str | None, human: str | None) -> str:
    """``agreed`` / ``disagreed`` / ``unclassifiable`` for one row.

    Fail-closed on both sides: an empty or unknown value on EITHER side is
    unclassifiable, never agreement. An absent verdict is not endorsement, and
    a pending approval is not consent. The release gate deliberately treats an
    unparseable verdict as "proceed" so a broken parser cannot freeze a
    pipeline; that reasoning does not carry here, where the same leniency would
    manufacture evidence for granting autonomy.
    """
    agent = (decision or "").strip().upper()
    person = (human or "").strip().lower()

    if agent in PROCEED_DECISIONS:
        agent_stops = False
    elif agent in STOP_DECISIONS:
        agent_stops = True
    else:
        return "unclassifiable"

    if person in HUMAN_PROCEED:
        human_stops = False
    elif person in HUMAN_STOP:
        human_stops = True
    else:
        return "unclassifiable"

    return "agreed" if agent_stops == human_stops else "disagreed"


def _diagnose_empty() -> str:
    """Say WHY there is nothing to measure, reading the tables themselves.

    An empty report that does not explain itself gets read as "the agents never
    agree" or "the feature is broken", and both readings have been made in this
    codebase before.
    """
    from hivepilot.services import db, state_service

    state_service.init_db()
    try:
        with db.connect() as conn:
            verdicts = conn.execute(
                "SELECT COUNT(*) AS n FROM verdicts WHERE decision IS NOT NULL"
            ).fetchone()["n"]
            approvals = conn.execute(
                "SELECT COUNT(*) AS n FROM approvals WHERE status IS NOT NULL"
            ).fetchone()["n"]
            shared = conn.execute(
                "SELECT COUNT(*) AS n FROM verdicts v JOIN approvals a ON a.run_id = v.pipeline_run_id"
            ).fetchone()["n"]
            unlinked = conn.execute(
                "SELECT COUNT(*) AS n FROM verdicts "
                "WHERE decision IS NOT NULL AND pipeline_run_id IS NULL"
            ).fetchone()["n"]
    except Exception as exc:  # noqa: BLE001 - a report must never raise
        logger.warning("autonomy.diagnose_failed", error=str(exc))
        return "the verdict and approval tables could not be read"

    if verdicts == 0 and approvals == 0:
        return "no verdict and no human decision has been recorded yet"
    if verdicts == 0:
        return f"{approvals} human decisions recorded, but no verdict carries a decision"
    if approvals == 0:
        return f"{verdicts} verdicts recorded, but no human decision has been recorded"
    if unlinked == verdicts:
        return (
            f"all {verdicts} verdicts carry no pipeline run id, so none can be matched "
            "to a human decision -- they predate the column"
        )
    if shared == 0:
        return (
            f"{verdicts} verdicts and {approvals} human decisions exist, but no single run "
            "produced both: verdicts come from the adversarial review and approvals from "
            "the destructive-step gate, and the two have never co-occurred. The ladder "
            "cannot be measured until one run does both"
        )
    return "the joined rows carry no decision this mapping recognises"


def agreement_report(limit: int = 500) -> AgreementReport:
    """Per-role agreement, or an explained emptiness."""
    from hivepilot.services import state_service

    rows: list[dict[str, Any]] = state_service.agreement_rows(limit=limit)
    if not rows:
        return AgreementReport(roles=[], rows_joined=0, empty_reason=_diagnose_empty())

    tally: dict[str, dict[str, int]] = {}
    for row in rows:
        role = (row.get("role") or "").strip() or "(unattributed)"
        bucket = tally.setdefault(role, {"agreed": 0, "disagreed": 0, "unclassifiable": 0})
        bucket[_classify(row.get("decision"), row.get("human"))] += 1

    roles = sorted(
        (
            RoleAgreement(
                role=role,
                agreed=counts["agreed"],
                disagreed=counts["disagreed"],
                unclassifiable=counts["unclassifiable"],
            )
            for role, counts in tally.items()
        ),
        key=lambda role: (-role.comparable, role.role),
    )
    empty = None if any(role.comparable for role in roles) else _diagnose_empty()
    return AgreementReport(roles=roles, rows_joined=len(rows), empty_reason=empty)


def render_report(report: AgreementReport) -> list[str]:
    """Human-readable lines; the sample size is never separated from the rate."""
    if report.empty_reason:
        return [
            "Autonomy ladder: NOT MEASURABLE",
            f"  reason: {report.empty_reason}",
            "",
            "  No rung is reported. A rate computed from zero comparable",
            "  observations is not a low rate -- it is an absent one.",
        ]

    lines = [
        f"Autonomy ladder: {report.rows_joined} joined rows, "
        f"minimum sample per role = {MIN_SAMPLE}",
        "",
        f"  {'role':<24} {'agreed':>7} {'comparable':>11} {'rate':>7}  evidence",
    ]
    for role in report.roles:
        rate = "n/a" if role.rate is None else f"{role.rate:.0%}"
        evidence = "sufficient" if role.has_enough_evidence else "INSUFFICIENT"
        lines.append(
            f"  {role.role:<24} {role.agreed:>7} {role.comparable:>11} {rate:>7}  {evidence}"
        )
        if role.unclassifiable:
            lines.append(
                f"  {'':<24} {role.unclassifiable} row(s) carried a decision this mapping "
                "does not recognise and were excluded"
            )
    lines.append("")
    lines.append("  Reporting only. Granting a role more autonomy stays an operator action.")
    return lines
