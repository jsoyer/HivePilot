"""HP-105 skill trust ladder — provisional↔trusted, enabled orthogonal.

OpenSpace ``SkillTrustState`` / ``record_trust_observation`` pattern,
rewritten in Python. This module does **not** vendor OpenSpace, talk to
OpenSpace cloud, persist pickle embeddings, or implement HP-106 causal
signals (those stay a stub hook) / HP-107 BM25 / HP-108 skill→tools.

Contracts:

- New registered revisions start **provisional**. ``enabled`` is a
  separate flag and never implied by trust (or vice versa).
- An unknown revision is not implicitly trusted and not implicitly
  enabled. Lookup does not persist a row.
- Promotion counts distinct successful ``completed`` events across
  runs (HP-104). Default threshold is 2 (OpenSpace: origin + one
  independent reuse). After a demotion, only successes *since* the
  last attributable failure count.
- Attributed failure demotes trusted → provisional. Ambiguous
  failure opens a HP-97 PASS review (``kind=skill_evolution``,
  ``action=trust_review``) and does **not** demote. ``not_skill``
  (env / tool / network / permission) is ignored.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from hivepilot.pass_store import (
    PENDING,
    SKILL_EVOLUTION_KIND,
    PassProposal,
    create_pending,
    inbox,
)
from hivepilot.services import db, events, state_service
from hivepilot.skill_catalog import logical_skill_id
from hivepilot.skill_events import list_skill_events

TRUST_STATES: tuple[str, ...] = ("provisional", "trusted")
PROVISIONAL = "provisional"
TRUSTED = "trusted"

ATTRIBUTIONS: tuple[str, ...] = ("attributed", "ambiguous", "not_skill")
ATTRIBUTED = "attributed"
AMBIGUOUS = "ambiguous"
NOT_SKILL = "not_skill"

TRUST_REVIEW_ACTION = "trust_review"
DEFAULT_PROMOTION_THRESHOLD = 2
SKILL_TRUST_ENTITY_TYPE = "skill_revision"

_ATTRIBUTED_TOKENS: frozenset[str] = frozenset(
    {"attributed", "skill", "skill_fault", "skill_phase_failed"}
)
_NOT_SKILL_TOKENS: frozenset[str] = frozenset(
    {"not_skill", "env", "tool", "permission", "network", "external"}
)


class SkillTrustError(ValueError):
    """Invalid trust state, attribution, or unknown revision mutation."""


@dataclass(frozen=True)
class SkillTrust:
    """Trust + availability for one revision. Unknown rows are not persisted."""

    revision_id: str
    logical_id: str
    skill_name: str
    tenant: str
    trust_state: str
    enabled: bool
    known: bool
    trust_successes: int = 0
    trust_failures: int = 0
    successes_since_failure: int = 0

    @property
    def trusted(self) -> bool:
        return self.known and self.trust_state == TRUSTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "logical_id": self.logical_id,
            "skill_name": self.skill_name,
            "tenant": self.tenant,
            "trust_state": self.trust_state,
            "enabled": self.enabled,
            "known": self.known,
            "trusted": self.trusted,
            "trust_successes": self.trust_successes,
            "trust_failures": self.trust_failures,
            "successes_since_failure": self.successes_since_failure,
        }


@dataclass(frozen=True)
class TrustDecision:
    """Outcome of a success evaluation or a failure report."""

    action: str
    trust: SkillTrust
    proposal_id: str = ""
    attribution: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "trust": self.trust.to_dict(),
            "proposal_id": self.proposal_id,
            "attribution": self.attribution,
        }


def promotion_threshold(override: int | None = None) -> int:
    """Independent successful runs required to promote provisional → trusted."""
    if override is not None:
        return max(1, int(override))
    raw = (os.environ.get("HIVEPILOT_SKILL_TRUST_PROMOTION") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError as exc:
            raise SkillTrustError(
                f"HIVEPILOT_SKILL_TRUST_PROMOTION must be an int, got {raw!r}"
            ) from exc
    return DEFAULT_PROMOTION_THRESHOLD


def classify_attribution(
    raw: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> str:
    """HP-106 stub: closed vocabulary, fail-closed to ``ambiguous``.

    Explicit ``phase_failed`` / ``skill_phase_failed`` is treated as
    attributed (OpenSpace ``skill_phase_failed_skill_ids``). Anything
    unrecognized, including a missing token, is review — never demotion.
    """
    token = (raw or "").strip().lower()
    body = dict(payload or {})
    if not token:
        token = str(body.get("attribution") or body.get("cause") or "").strip().lower()
    if body.get("phase_failed") or body.get("skill_phase_failed"):
        return ATTRIBUTED
    if token in _ATTRIBUTED_TOKENS:
        return ATTRIBUTED
    if token in _NOT_SKILL_TOKENS:
        return NOT_SKILL
    return AMBIGUOUS


def _trust_state(value: str) -> str:
    cleaned = (value or "").strip().lower()
    if cleaned not in TRUST_STATES:
        raise SkillTrustError(
            f"unknown trust state {value!r}; must be one of {list(TRUST_STATES)}"
        )
    return cleaned


def _unknown(
    revision_id: str,
    *,
    logical_id: str = "",
    skill_name: str = "",
    tenant: str = "default",
) -> SkillTrust:
    name = (skill_name or "").strip()
    logical = (logical_id or "").strip() or (logical_skill_id(name) if name else "")
    return SkillTrust(
        revision_id=(revision_id or "").strip(),
        logical_id=logical,
        skill_name=name,
        tenant=(tenant or "default").strip() or "default",
        trust_state=PROVISIONAL,
        enabled=False,
        known=False,
    )


def get(
    revision_id: str,
    *,
    tenant: str = "default",
    skill_name: str = "",
    logical_id: str = "",
) -> SkillTrust:
    """Return the stored row, or an unknown view (not trusted, not enabled)."""
    rev = (revision_id or "").strip()
    if not rev:
        raise SkillTrustError("revision_id is required")
    state_service.init_db()
    tenant_key = (tenant or "default").strip() or "default"
    with db.connect() as conn:
        row = conn.execute(
            db.ph(
                "SELECT * FROM skill_trust_states "
                "WHERE tenant = ? AND revision_id = ?"
            ),
            (tenant_key, rev),
        ).fetchone()
    if row is None:
        return _unknown(
            rev, logical_id=logical_id, skill_name=skill_name, tenant=tenant_key
        )
    return _row_to_trust(row)


def is_trusted(revision_id: str, *, tenant: str = "default") -> bool:
    return get(revision_id, tenant=tenant).trusted


def is_enabled(revision_id: str, *, tenant: str = "default") -> bool:
    return get(revision_id, tenant=tenant).enabled


def register_revision(
    *,
    revision_id: str,
    skill_name: str = "",
    logical_id: str = "",
    tenant: str = "default",
    enabled: bool = True,
) -> SkillTrust:
    """Persist a new revision as provisional. Existing rows are left intact."""
    rev = (revision_id or "").strip()
    if not rev:
        raise SkillTrustError("revision_id is required")
    name = (skill_name or "").strip()
    logical = (logical_id or "").strip() or (logical_skill_id(name) if name else "")
    if not logical:
        raise SkillTrustError("logical_id or skill_name is required")
    tenant_key = (tenant or "default").strip() or "default"
    state_service.init_db()
    existing = get(rev, tenant=tenant_key)
    if existing.known:
        return existing
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                INSERT INTO skill_trust_states
                    (revision_id, tenant, logical_id, skill_name, trust_state, enabled)
                VALUES (?, ?, ?, ?, ?, ?)
                """
            ),
            (rev, tenant_key, logical, name, PROVISIONAL, 1 if enabled else 0),
        )
    stored = get(rev, tenant=tenant_key)
    events.emit(
        "skill.trust_registered",
        SKILL_TRUST_ENTITY_TYPE,
        rev,
        tenant=tenant_key,
        payload={
            "skill_name": name,
            "logical_id": logical,
            "trust_state": PROVISIONAL,
            "enabled": stored.enabled,
        },
    )
    return stored


def set_enabled(
    revision_id: str,
    enabled: bool,
    *,
    tenant: str = "default",
) -> SkillTrust:
    """Flip availability. Refuses unknown revisions (they stay disabled)."""
    current = get(revision_id, tenant=tenant)
    if not current.known:
        raise SkillTrustError(
            f"unknown revision {revision_id!r} is not implicitly enabled"
        )
    if current.enabled is bool(enabled):
        return current
    state_service.init_db()
    tenant_key = current.tenant
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                UPDATE skill_trust_states
                SET enabled = ?, updated_ts = CURRENT_TIMESTAMP
                WHERE tenant = ? AND revision_id = ?
                """
            ),
            (1 if enabled else 0, tenant_key, current.revision_id),
        )
    stored = get(current.revision_id, tenant=tenant_key)
    events.emit(
        "skill.enabled" if stored.enabled else "skill.disabled",
        SKILL_TRUST_ENTITY_TYPE,
        stored.revision_id,
        tenant=tenant_key,
        payload={
            "enabled": stored.enabled,
            "trust_state": stored.trust_state,
        },
    )
    return stored


def evaluate_promotion(
    revision_id: str,
    *,
    tenant: str = "default",
    threshold: int | None = None,
) -> TrustDecision:
    """Promote when distinct completed runs since last failure reach *N*."""
    current = get(revision_id, tenant=tenant)
    if not current.known:
        return TrustDecision(action="unknown", trust=current)
    _sync_completed_events(current)
    needed = promotion_threshold(threshold)
    counts = _observation_counts(current.revision_id, current.tenant)
    next_state = current.trust_state
    action = "hold"
    if (
        current.trust_state == PROVISIONAL
        and counts["successes_since_failure"] >= needed
    ):
        next_state = TRUSTED
        action = "promote"
        _set_trust_state(current.revision_id, current.tenant, TRUSTED)
        events.emit(
            "skill.trust_promoted",
            SKILL_TRUST_ENTITY_TYPE,
            current.revision_id,
            tenant=current.tenant,
            payload={
                "previous_trust_state": PROVISIONAL,
                "trust_state": TRUSTED,
                "successes_since_failure": counts["successes_since_failure"],
                "threshold": needed,
            },
        )
    stored = get(current.revision_id, tenant=current.tenant)
    return TrustDecision(action=action, trust=stored)


def report_failure(
    *,
    revision_id: str,
    run_id: int | str,
    skill_name: str = "",
    logical_id: str = "",
    tenant: str = "default",
    attribution: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> TrustDecision:
    """Demote only on attributed failure. Ambiguity opens a PASS review."""
    rev = (revision_id or "").strip()
    run = str(run_id).strip()
    if not rev:
        raise SkillTrustError("revision_id is required")
    if not run:
        raise SkillTrustError("run_id is required")
    kind = classify_attribution(attribution, payload)
    current = get(rev, tenant=tenant, skill_name=skill_name, logical_id=logical_id)
    if kind == NOT_SKILL:
        return TrustDecision(action="ignore", trust=current, attribution=kind)
    if kind == AMBIGUOUS:
        proposal = _open_review(current, run_id=run, payload=payload)
        return TrustDecision(
            action="review",
            trust=current,
            proposal_id=proposal.id,
            attribution=kind,
        )
    if not current.known:
        return TrustDecision(action="unknown", trust=current, attribution=kind)
    _insert_observation(
        current,
        run_id=run,
        outcome="failure",
        attribution=kind,
        source="attributed_failure",
        extra=payload,
    )
    action = "hold"
    if current.trust_state == TRUSTED:
        _set_trust_state(current.revision_id, current.tenant, PROVISIONAL)
        action = "demote"
        events.emit(
            "skill.trust_demoted",
            SKILL_TRUST_ENTITY_TYPE,
            current.revision_id,
            tenant=current.tenant,
            payload={
                "previous_trust_state": TRUSTED,
                "trust_state": PROVISIONAL,
                "run_id": run,
                "attribution": kind,
            },
        )
    stored = get(current.revision_id, tenant=current.tenant)
    return TrustDecision(action=action, trust=stored, attribution=kind)


def _row_to_trust(row: Mapping[str, Any]) -> SkillTrust:
    data = dict(row)
    rev = str(data["revision_id"])
    tenant = str(data["tenant"])
    counts = _observation_counts(rev, tenant)
    return SkillTrust(
        revision_id=rev,
        logical_id=str(data["logical_id"]),
        skill_name=str(data.get("skill_name") or ""),
        tenant=tenant,
        trust_state=_trust_state(str(data["trust_state"])),
        enabled=bool(int(data["enabled"])),
        known=True,
        trust_successes=counts["successes"],
        trust_failures=counts["failures"],
        successes_since_failure=counts["successes_since_failure"],
    )


def _observation_counts(revision_id: str, tenant: str) -> dict[str, int]:
    state_service.init_db()
    with db.connect() as conn:
        totals = conn.execute(
            db.ph(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END), 0)
                        AS successes,
                    COALESCE(SUM(CASE WHEN outcome = 'failure' THEN 1 ELSE 0 END), 0)
                        AS failures
                FROM skill_trust_observations
                WHERE tenant = ? AND revision_id = ?
                """
            ),
            (tenant, revision_id),
        ).fetchone()
        last_failure = conn.execute(
            db.ph(
                """
                SELECT created_ts FROM skill_trust_observations
                WHERE tenant = ? AND revision_id = ? AND outcome = 'failure'
                ORDER BY created_ts DESC, id DESC LIMIT 1
                """
            ),
            (tenant, revision_id),
        ).fetchone()
        if last_failure is None:
            since = int(dict(totals)["successes"] or 0)
        else:
            since_row = conn.execute(
                db.ph(
                    """
                    SELECT COUNT(*) AS n FROM skill_trust_observations
                    WHERE tenant = ? AND revision_id = ?
                      AND outcome = 'success'
                      AND created_ts > ?
                    """
                ),
                (tenant, revision_id, last_failure["created_ts"]),
            ).fetchone()
            since = int(dict(since_row)["n"] or 0)
    data = dict(totals) if totals is not None else {}
    return {
        "successes": int(data.get("successes") or 0),
        "failures": int(data.get("failures") or 0),
        "successes_since_failure": since,
    }


def _set_trust_state(revision_id: str, tenant: str, trust_state: str) -> None:
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                UPDATE skill_trust_states
                SET trust_state = ?, updated_ts = CURRENT_TIMESTAMP
                WHERE tenant = ? AND revision_id = ?
                """
            ),
            (_trust_state(trust_state), tenant, revision_id),
        )


def _insert_observation(
    trust: SkillTrust,
    *,
    run_id: str,
    outcome: str,
    attribution: str = "",
    source: str = "",
    extra: Mapping[str, Any] | None = None,
) -> None:
    state_service.init_db()
    body = dict(extra or {})
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                INSERT INTO skill_trust_observations
                    (id, revision_id, tenant, run_id, outcome, attribution, source, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant, revision_id, run_id, outcome) DO NOTHING
                """
            ),
            (
                str(uuid.uuid4()),
                trust.revision_id,
                trust.tenant,
                run_id,
                outcome,
                attribution,
                source,
                json.dumps(body, sort_keys=True, ensure_ascii=False),
            ),
        )


def _sync_completed_events(trust: SkillTrust) -> None:
    """Turn HP-104 completed rows into one success observation per run."""
    for event in list_skill_events(
        tenant=trust.tenant,
        revision_id=trust.revision_id,
        event_type="completed",
        limit=1000,
    ):
        _insert_observation(
            trust,
            run_id=event.run_id,
            outcome="success",
            source="skill_event",
            extra={"event_id": event.event_id, "step": event.step},
        )


def _open_review(
    trust: SkillTrust,
    *,
    run_id: str,
    payload: Mapping[str, Any] | None,
) -> PassProposal:
    """Idempotent PENDING card. Does not call ``submit`` (no auto-decide)."""
    body = {
        "revision_id": trust.revision_id,
        "logical_id": trust.logical_id,
        "skill_name": trust.skill_name,
        "run_id": run_id,
        "reason": "ambiguous_failure",
        "attribution": AMBIGUOUS,
        **dict(payload or {}),
    }
    for proposal in inbox(
        kind=SKILL_EVOLUTION_KIND, status=PENDING, tenant=trust.tenant
    ):
        if (
            proposal.action == TRUST_REVIEW_ACTION
            and str(proposal.payload.get("revision_id") or "") == trust.revision_id
            and str(proposal.payload.get("run_id") or "") == run_id
        ):
            return proposal
    stored = create_pending(
        kind=SKILL_EVOLUTION_KIND,
        action=TRUST_REVIEW_ACTION,
        project=trust.skill_name,
        task=trust.revision_id,
        payload=body,
        tenant=trust.tenant,
    )
    events.emit(
        "skill.trust_review",
        SKILL_TRUST_ENTITY_TYPE,
        trust.revision_id or "unknown",
        tenant=trust.tenant,
        payload={
            "proposal_id": stored.id,
            "run_id": run_id,
            "attribution": AMBIGUOUS,
            "known": trust.known,
        },
    )
    return stored
