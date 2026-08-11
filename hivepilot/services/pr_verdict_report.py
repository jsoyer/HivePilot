"""Compose the report a blocked release gate posts onto the pull request.

Why this exists
---------------
Measured on PR #428 before it did: `reviews: 0, comments: 0`, and a
43-character body reading "Automated pull request opened by HivePilot." The
reviewer, CISO and QA had all run and produced 7 735, 13 958 and 9 702
characters of findings, and the gate had correctly refused to promote. None of
that was anywhere a human would look.

The stored verdict was the whole problem in one line::

    adversarial review by reviewer, ciso, qa:
    reviewer: REQUEST_CHANGES; ciso: BLOCKED; qa: REQUEST_CHANGES

Three status tokens and not one reason. A gate that says BLOCKED without
saying why moves the work of finding out onto the person least able to do it,
and `git_service`'s own comment says the opposite was intended: "opening (or
keeping) the draft PR must still happen even when the gate blocks, so a human
can see the review report on the PR itself."

Where the reasons come from
---------------------------
Each stage's full output is already persisted as an ``interactions`` row
against the PIPELINE run. Matching a row to a role became possible when the
role key started travelling in ``interactions.metadata`` -- ``actor`` holds a
display label ("Hugo (CISO)") that belongs to the tenant, not the engine.

Nothing new is captured here. The material was always there; only the gate
never read it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import structlog

from hivepilot.services import db

logger = structlog.get_logger(__name__)

#: Statuses that stop a release. Kept in step with `git_service`'s own set --
#: a role listed as blocking there and passing here would explain the wrong
#: outcome, which is worse than explaining nothing.
BLOCKING_STATUSES: frozenset[str] = frozenset(
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

#: Per-role excerpt cap. A CISO answer ran to 13 958 characters on the run that
#: prompted this; three of those would make a comment nobody scrolls to the end
#: of. The cut is declared, because a silent one reads as "that was all it
#: said".
_MAX_ROLE_CHARS = 4_000

#: Whole-comment cap, well under GitHub's own limit, so a pathological run
#: cannot turn a review into a wall.
_MAX_REPORT_CHARS = 18_000

_ROLE_STATUS_RE = re.compile(r"([A-Za-z0-9_.-]+)\s*:\s*([A-Z_]+)")


@dataclass(frozen=True)
class RoleVerdict:
    role: str
    status: str
    reason: str | None

    @property
    def blocking(self) -> bool:
        return self.status.upper() in BLOCKING_STATUSES


def parse_verdict_summary(summary: str | None) -> list[RoleVerdict]:
    """Pull ``role: STATUS`` pairs out of the stored verdict summary.

    Tolerant by design: the summary is free text assembled upstream, and a
    shape this cannot read must degrade to "no roles parsed" rather than
    raise inside a git action.
    """
    if not summary:
        return []
    seen: dict[str, str] = {}
    for role, status in _ROLE_STATUS_RE.findall(summary):
        # Later mentions win: the summary reads "…by reviewer, ciso, qa:
        # reviewer: X; …", so the first occurrence of a name is the roll-call,
        # not its verdict.
        seen[role.lower()] = status.upper()
    return [RoleVerdict(role=r, status=s, reason=None) for r, s in seen.items()]


def _role_outputs(run_id: int) -> dict[str, str]:
    """Each role's full stage output for *run_id*, keyed by role.

    Reads `interactions.metadata["role"]`, not `actor`: the actor is a
    tenant-owned display label and parsing it back would bake persona naming
    into the engine.
    """
    try:
        with db.connect() as conn:
            rows = conn.execute(
                db.ph(
                    "SELECT metadata, summary FROM interactions "
                    "WHERE run_id = ? AND summary IS NOT NULL ORDER BY id"
                ),
                (run_id,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - a report must not break a git action
        logger.warning("pr_verdict_report.read_failed", run_id=run_id, error=str(exc))
        return {}

    outputs: dict[str, str] = {}
    for raw_meta, summary in rows:
        if not raw_meta or not summary:
            continue
        try:
            meta: Any = json.loads(raw_meta)
        except (TypeError, ValueError):
            continue
        role = meta.get("role") if isinstance(meta, dict) else None
        if isinstance(role, str) and role.strip():
            outputs[role.strip().lower()] = str(summary)
    return outputs


def _excerpt(text: str) -> str:
    """Bounded, redacted excerpt of one role's output, cut declared."""
    from hivepilot.services.config_provenance import redact_text

    body = redact_text(text.strip())
    if len(body) <= _MAX_ROLE_CHARS:
        return body
    return (
        body[:_MAX_ROLE_CHARS]
        + f"\n\n_[truncated: {len(body):,} characters, {_MAX_ROLE_CHARS:,} shown]_"
    )


def build_report(*, run_id: int, verdict_summary: str | None) -> str | None:
    """Markdown explaining WHY the gate blocked, or ``None`` if it did not.

    Returns ``None`` when no role reported a blocking status. A gate that
    comments on every green run trains people to ignore its comments, and the
    comment that matters then arrives in a stream they have learned to skip.
    """
    verdicts = parse_verdict_summary(verdict_summary)
    blockers = [v for v in verdicts if v.blocking]
    if not blockers:
        return None

    outputs = _role_outputs(run_id)

    lines: list[str] = [
        "## HivePilot release gate: **BLOCKED**",
        "",
        f"Run `{run_id}`. The pull request stays a draft until these are resolved.",
        "",
        "| role | verdict |",
        "| --- | --- |",
    ]
    for verdict in verdicts:
        mark = "🚫" if verdict.blocking else "✅"
        lines.append(f"| `{verdict.role}` | {mark} {verdict.status} |")

    lines.append("")
    for verdict in blockers:
        lines.append(f"### `{verdict.role}` — {verdict.status}")
        body = outputs.get(verdict.role)
        if body:
            lines.append("")
            lines.append(_excerpt(body))
        else:
            # Say it plainly. A blocker silently dropped for want of a
            # recorded output would make the list look complete when it is not.
            lines.append("")
            lines.append(
                "_This role blocked, but its output was not recorded for this run, "
                "so there is no reasoning to quote here._"
            )
        lines.append("")

    report = "\n".join(lines).rstrip() + "\n"
    if len(report) > _MAX_REPORT_CHARS:
        report = (
            report[:_MAX_REPORT_CHARS]
            + f"\n\n_[report truncated at {_MAX_REPORT_CHARS:,} characters]_\n"
        )
    return report
