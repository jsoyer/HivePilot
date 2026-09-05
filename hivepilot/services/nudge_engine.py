"""Nudge engine (HP-50): observe CI / review / conflict → owner role.

Fail-SAFE by contract: a nudge must never change a gate decision, never raise
into git/orchestrator, and never start an LLM reply loop. It persists a
`kind="nudge"` verdict and posts a system message into the project's
Orchestrateur Espace (HP-49) so the owner sees the structured verdict
(decision + je bloque si + file:line) on the next read.

Does not ingest GitHub review webhooks (forges are outbound-only today).
Does not enforce file-ownership as a merge gate (still deferred).
"""

from __future__ import annotations

import json
from typing import Any

from hivepilot.services import events, state_service
from hivepilot.services.structured_verdict import (
    StructuredVerdict,
    from_ci_failures,
    from_conflicts,
    parse_structured_verdict,
)
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

NUDGE_KIND = "nudge"


def _post_space(verdict: StructuredVerdict, *, project: str, tenant: str) -> int | None:
    from hivepilot.services.orchestrator_service import get_or_create_project_space

    space_id = get_or_create_project_space(project, tenant)
    msg_id = state_service.add_space_message(
        space_id,
        "system",
        verdict.to_markdown(),
        tenant=tenant,
        actions=[
            {
                "label": f"{verdict.source} → {verdict.owner_role}",
                "detail": verdict.decision,
            },
            *[
                {"label": finding.ref(), "detail": f"{finding.severity}: {finding.message}"}
                for finding in verdict.findings[:12]
            ],
        ],
    )
    events.emit(
        "nudge.posted",
        "space",
        space_id,
        tenant=tenant,
        payload={
            "space_id": space_id,
            "message_id": msg_id,
            "owner_role": verdict.owner_role,
            "source": verdict.source,
            "decision": verdict.decision,
        },
    )
    return msg_id


def observe(
    verdict: StructuredVerdict,
    *,
    project: str,
    run_id: int | None = None,
    task: str | None = None,
    tenant: str = "default",
    pipeline_run_id: int | None = None,
) -> int | None:
    """Persist + post one nudge. Returns the verdict id, or None on failure."""
    try:
        verdict_id = state_service.record_verdict(
            run_id=run_id,
            pipeline_run_id=pipeline_run_id,
            project=project,
            task=task,
            role=verdict.owner_role,
            kind=NUDGE_KIND,
            decision=verdict.decision,
            confidence=None,
            summary=verdict.to_markdown(),
            findings_json=json.dumps(verdict.to_payload()),
            block_if_json=json.dumps(list(verdict.block_if)),
        )
    except TypeError:
        # Older record_verdict without the new kwargs (tests that stub it).
        try:
            verdict_id = state_service.record_verdict(
                run_id=run_id,
                pipeline_run_id=pipeline_run_id,
                project=project,
                task=task,
                role=verdict.owner_role,
                kind=NUDGE_KIND,
                decision=verdict.decision,
                confidence=None,
                summary=verdict.to_markdown(),
            )
        except Exception as exc:  # noqa: BLE001 — observer must not fail the gate
            logger.warning("nudge.persist_failed", source=verdict.source, error=str(exc))
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("nudge.persist_failed", source=verdict.source, error=str(exc))
        return None

    try:
        _post_space(verdict, project=project, tenant=tenant)
    except Exception as exc:  # noqa: BLE001
        logger.warning("nudge.post_failed", source=verdict.source, error=str(exc))
    return verdict_id


def observe_ci(
    results: list[Any],
    *,
    project: str,
    owner_role: str,
    run_id: int | None = None,
    task: str | None = None,
    tenant: str = "default",
    summary: str = "",
) -> int | None:
    """Nudge the owner when deterministic checks / CI are blocking."""
    failures = [r for r in results if getattr(r, "outcome", "failed") != "passed"]
    if not failures:
        return None
    try:
        verdict = from_ci_failures(failures, owner_role=owner_role, summary=summary)
        return observe(verdict, project=project, run_id=run_id, task=task, tenant=tenant)
    except Exception as exc:  # noqa: BLE001
        logger.warning("nudge.ci_failed", error=str(exc))
        return None


def observe_review(
    *,
    project: str,
    owner_role: str,
    decision: str | None,
    text: str,
    run_id: int | None = None,
    task: str | None = None,
    tenant: str = "default",
    pipeline_run_id: int | None = None,
) -> int | None:
    """Nudge the owner when an in-pipeline review is blocking."""
    token = (decision or "").strip().upper()
    if token in {"", "PASS", "APPROVE", "APPROVED"}:
        return None
    try:
        verdict = parse_structured_verdict(
            text,
            source="review",
            owner_role=owner_role,
            decision="REQUEST_CHANGES" if "REQUEST" in token else "BLOCK",
        )
        return observe(
            verdict,
            project=project,
            run_id=run_id,
            task=task,
            tenant=tenant,
            pipeline_run_id=pipeline_run_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("nudge.review_failed", error=str(exc))
        return None


def observe_conflicts(
    conflicts: list[Any],
    *,
    project: str,
    run_id: int | None = None,
    task: str | None = None,
    tenant: str = "default",
) -> list[int]:
    """Nudge each owner role that had a file stolen. Advisory — does not gate."""
    posted: list[int] = []
    try:
        for verdict in from_conflicts(conflicts):
            verdict_id = observe(verdict, project=project, run_id=run_id, task=task, tenant=tenant)
            if verdict_id is not None:
                posted.append(verdict_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("nudge.conflict_failed", error=str(exc))
    return posted
