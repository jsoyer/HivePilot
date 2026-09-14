"""HP-109 draft FIX/DERIVED/CAPTURED skill-evolution proposals.

OpenSpace ``EvolutionType`` pattern, rewritten in Python. This module does
**not** vendor OpenSpace, talk to OpenSpace cloud, persist pickle
embeddings, write ``.skill_id`` sidecars, implement HP-112, HP-114, or
OpenSpace ``autonomous`` evolution mode. HP-110 validation lives in
``hivepilot.skill_evolution_validator`` and is a read-only gate. HP-111
atomic accept lives in ``hivepilot.skill_evolution_accept``.

Contracts:

- Draft only until HITL approve + explicit accept. Admissible proposals
  land in the HP-97 PASS inbox (``kind=skill_evolution``). ``propose``
  never writes skill files.
- CAPTURED requires independent validation: an execution evidence ref
  plus a distinct validation ref. Whole-task ``completed`` on the same
  run is not sufficient. A caller flag is not enough.
- Merge key is deterministic and idempotent: a second propose with the
  same key returns the existing card (any status).
- Never auto-commit. ``create_pending`` only (no ``submit`` /
  ``match_auto``). Autonomous mode tokens are refused.
  ``apply_approved`` writes only after PASS ``APPROVED`` (HITL).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from hivepilot.evidence import assess_evolution_claim, get_ref
from hivepilot.pass_store import (
    APPROVED,
    PENDING,
    SKILL_EVOLUTION_KIND,
    PassProposal,
    create_pending,
    inbox,
)
from hivepilot.pass_store import (
    get as get_proposal,
)
from hivepilot.services import events
from hivepilot.services.skill_workshop_service import unified_files_diff
from hivepilot.skill_catalog import (
    SKILL_ID_SIDECAR,
    SkillCatalog,
    logical_skill_id,
    snapshot_hash,
)
from hivepilot.skill_evolution_accept import ApplyResult, accept_approved
from hivepilot.skill_evolution_validator import REJECT, validate, validate_proposal
from hivepilot.skill_signals import FailureAttribution, assess_fix_eligibility

# Closed vocabularies. Adding a token is additive; renaming is a breaking change.
EVOLUTION_TYPES: tuple[str, ...] = ("fix", "derived", "captured")
FIX = "fix"
DERIVED = "derived"
CAPTURED = "captured"

TYPE_TO_ORIGIN: dict[str, str] = {
    FIX: "fixed",
    DERIVED: "derived",
    CAPTURED: "captured",
}

DRAFT = "draft"
BLOCKED = "blocked"

SKILL_EVOLUTION_ENTITY_TYPE = "skill_evolution"

# OpenSpace EVOLUTION_MODE=autonomous is a hard no-go (HP-94 / HP-109).
AUTONOMOUS_TOKENS: frozenset[str] = frozenset(
    {
        "autonomous",
        "auto_evolve",
        "auto-evolve",
        "autoevolve",
        "openspace_autonomous",
    }
)

_COMPLETED = "completed"


class SkillEvolutionError(ValueError):
    """Invalid evolution type, autonomous mode, or draft payload."""


@dataclass(frozen=True)
class CapturedEligibility:
    """CAPTURED gate. Independent validation is computed, never trusted."""

    admissible: bool
    independently_validated: bool
    execution_ref: str = ""
    validation_ref: str = ""
    missing: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "admissible": self.admissible,
            "independently_validated": self.independently_validated,
            "execution_ref": self.execution_ref,
            "validation_ref": self.validation_ref,
            "missing": list(self.missing),
            "reason": self.reason,
        }


ApplyRefusal = ApplyResult


@dataclass(frozen=True)
class EvolutionDraft:
    """One FIX/DERIVED/CAPTURED draft. ``persisted`` is the inbox flag."""

    evolution_type: str
    status: str
    admissible: bool
    persisted: bool
    merge_key: str
    independently_validated: bool
    reason: str = ""
    missing: tuple[str, ...] = ()
    proposal_id: str = ""
    origin: str = ""
    payload: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": SKILL_EVOLUTION_KIND,
            "evolution_type": self.evolution_type,
            "action": self.evolution_type,
            "status": self.status,
            "admissible": self.admissible,
            "persisted": self.persisted,
            "merge_key": self.merge_key,
            "independently_validated": self.independently_validated,
            "reason": self.reason,
            "missing": list(self.missing),
            "proposal_id": self.proposal_id,
            "origin": self.origin,
            "payload": dict(self.payload or {}),
        }


def normalize_evolution_type(raw: str) -> str:
    cleaned = (raw or "").strip().lower()
    if cleaned not in EVOLUTION_TYPES:
        raise SkillEvolutionError(
            f"evolution_type must be one of {list(EVOLUTION_TYPES)}; got {raw!r}"
        )
    return cleaned


def evolution_merge_key(
    *,
    evolution_type: str,
    tenant: str = "default",
    name: str = "",
    revision_id: str = "",
    parent_ids: Sequence[str] = (),
    content_hash: str = "",
    causal_event_id: str = "",
    representative_result: str = "",
    execution_ref: str = "",
    validation_ref: str = "",
) -> str:
    """Stable idempotency key for one draft identity."""
    payload = {
        "causal_event_id": (causal_event_id or "").strip(),
        "content_hash": content_hash or "",
        "evolution_type": normalize_evolution_type(evolution_type),
        "execution_ref": (execution_ref or "").strip(),
        "name": (name or "").strip(),
        "parent_ids": sorted({str(item).strip() for item in parent_ids if str(item).strip()}),
        "representative_result": (representative_result or "").strip(),
        "revision_id": (revision_id or "").strip(),
        "tenant": (tenant or "default").strip() or "default",
        "validation_ref": (validation_ref or "").strip(),
    }
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:32]


def assess_captured_eligibility(
    *,
    execution_ref: str = "",
    validation_ref: str = "",
    tenant: str = "default",
) -> CapturedEligibility:
    """CAPTURED needs execution + a distinct independent validation ref.

    Whole-task ``completed`` on the same run is not sufficient. A caller
    ``independently_validated=True`` flag does not bypass the refs.
    """
    execution = (execution_ref or "").strip()
    validation = (validation_ref or "").strip()
    missing: list[str] = []
    if not execution:
        missing.append("execution_evidence")
    if not validation:
        missing.append("independent_validation")
    if missing:
        return CapturedEligibility(
            admissible=False,
            independently_validated=False,
            execution_ref=execution,
            validation_ref=validation,
            missing=tuple(missing),
            reason="missing_" + "+".join(missing),
        )
    if execution == validation:
        return CapturedEligibility(
            admissible=False,
            independently_validated=False,
            execution_ref=execution,
            validation_ref=validation,
            reason="validation_not_independent",
        )
    if _same_run_completed(execution, validation, tenant=tenant):
        return CapturedEligibility(
            admissible=False,
            independently_validated=False,
            execution_ref=execution,
            validation_ref=validation,
            reason="whole_task_success_not_sufficient",
        )
    return CapturedEligibility(
        admissible=True,
        independently_validated=True,
        execution_ref=execution,
        validation_ref=validation,
        reason="execution_and_independent_validation",
    )


def find_by_merge_key(merge_key: str, *, tenant: str = "default") -> PassProposal | None:
    """Return the existing PASS card for ``merge_key``, if any."""
    key = (merge_key or "").strip()
    if not key:
        return None
    scoped = (tenant or "default").strip() or "default"
    for proposal in inbox(kind=SKILL_EVOLUTION_KIND, status=None, tenant=scoped):
        if str(proposal.payload.get("merge_key") or "") == key:
            return proposal
    return None


def propose(
    *,
    evolution_type: str,
    name: str = "",
    files: Mapping[str, str] | None = None,
    tenant: str = "default",
    evidence_refs: Sequence[str] = (),
    revision_id: str = "",
    causal_event_id: str = "",
    representative_result: str = "",
    parent_logical_ids: Sequence[str] = (),
    execution_ref: str = "",
    validation_ref: str = "",
    independently_validated: bool = False,
    attribution: FailureAttribution | None = None,
    mode: str = "",
    auto_apply: bool = False,
    baseline_files: Mapping[str, str] | None = None,
    specific_approvals: Sequence[str] = (),
) -> EvolutionDraft:
    """Stage a draft into PASS. Never writes skill bytes or auto-applies."""
    _refuse_autonomous(mode, auto_apply)
    kind = normalize_evolution_type(evolution_type)
    origin = TYPE_TO_ORIGIN[kind]
    scoped = (tenant or "default").strip() or "default"
    files_copy = _copy_files(files)
    digest = snapshot_hash(files_copy)
    skill_name = (name or "").strip()
    if attribution is not None:
        revision_id = revision_id or attribution.revision_id
        causal_event_id = causal_event_id or attribution.causal_event_id
        representative_result = representative_result or attribution.representative_result
        if not skill_name:
            skill_name = _skill_name_from_attribution(attribution)

    missing: list[str] = []
    reason = ""
    captured = CapturedEligibility(
        admissible=kind != CAPTURED,
        independently_validated=False,
    )
    if kind == FIX:
        eligibility = assess_fix_eligibility(
            attribution,
            revision_id=revision_id,
            causal_event_id=causal_event_id,
            representative_result=representative_result,
        )
        if not eligibility.admissible:
            missing = list(eligibility.missing)
            reason = eligibility.reason
        revision_id = eligibility.revision_id or revision_id
        causal_event_id = eligibility.causal_event_id or causal_event_id
        representative_result = eligibility.representative_result or representative_result
    elif kind == DERIVED:
        parents = _clean_ids(parent_logical_ids)
        if not skill_name:
            missing.append("name")
        if not parents:
            missing.append("parents")
        if missing:
            reason = "missing_" + "+".join(missing)
    else:
        captured = assess_captured_eligibility(
            execution_ref=execution_ref,
            validation_ref=validation_ref,
            tenant=scoped,
        )
        if not skill_name:
            missing.append("name")
        if not captured.admissible:
            missing.extend(item for item in captured.missing if item not in missing)
            reason = captured.reason
        elif missing:
            reason = "missing_" + "+".join(missing)
        execution_ref = captured.execution_ref
        validation_ref = captured.validation_ref

    cited = _collect_evidence_refs(
        evidence_refs,
        execution_ref=execution_ref,
        validation_ref=validation_ref,
    )
    admission = assess_evolution_claim({"evidence_refs": list(cited)}, tenant=scoped)
    if not admission.admissible:
        if "evidence_refs" not in missing:
            missing.append("evidence_refs")
        if not reason:
            reason = admission.reason or "not admissible: evolution claim has no evidence refs"

    logical = ""
    if skill_name:
        logical = logical_skill_id(skill_name)
    merge_key = evolution_merge_key(
        evolution_type=kind,
        tenant=scoped,
        name=skill_name,
        revision_id=revision_id,
        parent_ids=parent_logical_ids,
        content_hash=digest,
        causal_event_id=causal_event_id,
        representative_result=representative_result,
        execution_ref=execution_ref,
        validation_ref=validation_ref,
    )
    payload = {
        "applied": False,
        "attribution": attribution.to_dict() if attribution is not None else {},
        "causal_event_id": (causal_event_id or "").strip(),
        "content_hash": digest,
        "draft_only": True,
        "evolution_type": kind,
        "evidence_refs": list(cited),
        "caller_independently_validated": bool(independently_validated),
        "execution_ref": (execution_ref or "").strip(),
        "files": files_copy,
        "independently_validated": captured.independently_validated,
        "logical_id": logical,
        "merge_key": merge_key,
        "name": skill_name,
        "origin": origin,
        "parent_logical_ids": list(_clean_ids(parent_logical_ids)),
        "representative_result": (representative_result or "").strip(),
        "revision_id": (revision_id or "").strip(),
        "skill_id_sidecar": SKILL_ID_SIDECAR,
        "specific_approvals": list(specific_approvals),
        "validation_ref": (validation_ref or "").strip(),
        "base_files": _copy_files(baseline_files),
        "base_digest": snapshot_hash(_copy_files(baseline_files)) if baseline_files else "",
    }
    verdict = validate(
        files=files_copy,
        name=skill_name,
        baseline_files=baseline_files,
        specific_approvals=specific_approvals,
    )
    payload["validation"] = verdict.to_dict()
    if verdict.result == REJECT:
        if "validation" not in missing:
            missing.append("validation")
        if not reason:
            reason = verdict.reason or "validation_rejected"
    if reason:
        return EvolutionDraft(
            evolution_type=kind,
            status=BLOCKED,
            admissible=False,
            persisted=False,
            merge_key=merge_key,
            independently_validated=captured.independently_validated,
            reason=reason,
            missing=tuple(dict.fromkeys(missing)),
            origin=origin,
            payload=payload,
        )

    existing = find_by_merge_key(merge_key, tenant=scoped)
    if existing is not None:
        return _from_proposal(existing, merge_key=merge_key, captured=captured)
    stored = create_pending(
        kind=SKILL_EVOLUTION_KIND,
        action=kind,
        project=skill_name,
        task=(revision_id or logical or merge_key).strip(),
        payload=payload,
        tenant=scoped,
    )
    events.emit(
        "skill.evolution_drafted",
        SKILL_EVOLUTION_ENTITY_TYPE,
        stored.id,
        tenant=scoped,
        payload={
            "evolution_type": kind,
            "merge_key": merge_key,
            "origin": origin,
            "independently_validated": captured.independently_validated,
        },
    )
    return EvolutionDraft(
        evolution_type=kind,
        status=DRAFT,
        admissible=True,
        persisted=True,
        merge_key=merge_key,
        independently_validated=captured.independently_validated,
        proposal_id=stored.id,
        origin=origin,
        payload=dict(stored.payload),
    )


def propose_fix(
    attribution: FailureAttribution,
    *,
    evidence_refs: Sequence[str] = (),
    files: Mapping[str, str] | None = None,
    tenant: str = "default",
    name: str = "",
    mode: str = "",
    auto_apply: bool = False,
    baseline_files: Mapping[str, str] | None = None,
    specific_approvals: Sequence[str] = (),
) -> EvolutionDraft:
    """Persist a FIX draft from an HP-106 attribution. Does not apply."""
    return propose(
        evolution_type=FIX,
        name=name,
        files=files,
        tenant=tenant,
        evidence_refs=evidence_refs,
        attribution=attribution,
        mode=mode,
        auto_apply=auto_apply,
        baseline_files=baseline_files,
        specific_approvals=specific_approvals,
    )


def preview_accept(proposal_id: str) -> dict[str, Any]:
    """Describe what accept would commit. Never mutates disk or catalog."""
    proposal = get_proposal(proposal_id)
    if proposal is None:
        return {
            "proposal_id": proposal_id,
            "would_mutate": False,
            "reason": "proposal_not_found",
        }
    payload = proposal.payload
    verdict = validate_proposal(proposal.id)
    applied = bool(payload.get("applied"))
    would_mutate = proposal.status == APPROVED and not applied and verdict.result != REJECT
    if applied:
        reason = "already_applied"
    elif proposal.status != APPROVED:
        reason = "hitl_required"
    elif verdict.result == REJECT:
        reason = verdict.reason or "validation_rejected"
    else:
        reason = "ready"
    return {
        "proposal_id": proposal.id,
        "evolution_type": str(payload.get("evolution_type") or proposal.action),
        "origin": str(payload.get("origin") or ""),
        "name": str(payload.get("name") or ""),
        "content_hash": str(payload.get("content_hash") or ""),
        "merge_key": str(payload.get("merge_key") or ""),
        "pass_status": proposal.status,
        "applied": applied,
        "would_mutate": would_mutate,
        "reason": reason,
        "validation": verdict.to_dict(),
        "diffs": file_diffs(proposal.id),
        "lineage": lineage_graph(proposal.id),
    }


def file_diffs(proposal_id: str) -> list[dict[str, Any]]:
    """Per-file unified diffs for Pollen. Empty when the proposal is missing."""
    proposal = get_proposal(proposal_id)
    if proposal is None:
        return []
    payload = proposal.payload
    before = payload.get("base_files") if isinstance(payload.get("base_files"), dict) else {}
    after = payload.get("files") if isinstance(payload.get("files"), dict) else {}
    diffs: list[dict[str, Any]] = []
    for rel in sorted(set(before) | set(after)):
        old = before.get(rel, "") if isinstance(before.get(rel, ""), str) else ""
        new = after.get(rel, "") if isinstance(after.get(rel, ""), str) else ""
        if old == new:
            continue
        if not isinstance(rel, str):
            continue
        diffs.append(
            {
                "path": rel,
                "before": old,
                "after": new,
                "unified": unified_files_diff({rel: old}, {rel: new}),
            }
        )
    return diffs


def lineage_graph(proposal_id: str) -> dict[str, Any]:
    """Parent → draft DAG for Pollen ``@xyflow``. Missing proposal → empty."""
    proposal = get_proposal(proposal_id)
    if proposal is None:
        return {"nodes": [], "edges": []}
    payload = proposal.payload
    name = str(payload.get("name") or proposal.project or proposal.id)
    logical = str(payload.get("logical_id") or (logical_skill_id(name) if name else proposal.id))
    origin = str(payload.get("origin") or payload.get("evolution_type") or proposal.action)
    parents = [
        str(item).strip()
        for item in (payload.get("parent_logical_ids") or [])
        if str(item).strip()
    ]
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for parent in parents:
        nodes.append(
            {
                "id": parent,
                "label": parent,
                "kind": "parent",
                "origin": "",
            }
        )
        edges.append({"source": parent, "target": logical})
    nodes.append(
        {
            "id": logical,
            "label": name,
            "kind": "draft",
            "origin": origin,
            "proposal_id": proposal.id,
        }
    )
    return {"nodes": nodes, "edges": edges}


def apply_approved(
    proposal_id: str = "",
    *,
    proposal: PassProposal | None = None,
    skill_root: Path | None = None,
    catalog: SkillCatalog | None = None,
    expected_digest: str = "",
    actor: str = "",
) -> ApplyResult:
    """HP-111 accept. Writes only after PASS approve; double-accept is a no-op."""
    return accept_approved(
        proposal_id,
        proposal=proposal,
        skill_root=skill_root,
        catalog=catalog,
        expected_digest=expected_digest,
        actor=actor,
    )


def _refuse_autonomous(mode: str, auto_apply: bool) -> None:
    token = (mode or "").strip().lower()
    if auto_apply or token in AUTONOMOUS_TOKENS:
        raise SkillEvolutionError("OpenSpace autonomous evolution mode is no-go")


def _copy_files(files: Mapping[str, str] | None) -> dict[str, str]:
    if files is None:
        return {}
    copied: dict[str, str] = {}
    for rel, content in files.items():
        if not isinstance(rel, str) or not isinstance(content, str):
            raise SkillEvolutionError("skill files must be str → str")
        copied[rel] = content
    return copied


def _clean_ids(values: Sequence[str]) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for raw in values:
        cleaned = str(raw or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            found.append(cleaned)
    return tuple(found)


def _collect_evidence_refs(
    evidence_refs: Sequence[str],
    *,
    execution_ref: str,
    validation_ref: str,
) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for raw in (*evidence_refs, execution_ref, validation_ref):
        cleaned = str(raw or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            found.append(cleaned)
    return tuple(found)


def _skill_name_from_attribution(attribution: FailureAttribution) -> str:
    extra = attribution.to_dict()
    for key in ("skill_name", "name"):
        text = str(extra.get(key) or "").strip()
        if text:
            return text
    return ""


def _same_run_completed(execution_ref: str, validation_ref: str, *, tenant: str) -> bool:
    execution = get_ref(execution_ref, tenant=tenant)
    validation = get_ref(validation_ref, tenant=tenant)
    if execution is None or validation is None:
        return False
    exec_meta = execution.metadata
    val_meta = validation.metadata
    val_type = str(val_meta.get("event_type") or val_meta.get("skill_event_type") or "").strip()
    if val_type != _COMPLETED:
        return False
    exec_run = str(exec_meta.get("run_id") or "").strip()
    val_run = str(val_meta.get("run_id") or "").strip()
    return bool(exec_run) and exec_run == val_run


def _from_proposal(
    proposal: PassProposal,
    *,
    merge_key: str,
    captured: CapturedEligibility,
) -> EvolutionDraft:
    kind = str(proposal.payload.get("evolution_type") or proposal.action or "")
    origin = str(proposal.payload.get("origin") or TYPE_TO_ORIGIN.get(kind, ""))
    validated = bool(proposal.payload.get("independently_validated"))
    if captured.independently_validated:
        validated = True
    return EvolutionDraft(
        evolution_type=kind or proposal.action,
        status=DRAFT if proposal.status == PENDING else proposal.status.lower(),
        admissible=True,
        persisted=True,
        merge_key=merge_key,
        independently_validated=validated,
        proposal_id=proposal.id,
        origin=origin,
        payload=dict(proposal.payload),
    )
