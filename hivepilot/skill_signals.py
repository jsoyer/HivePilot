"""HP-106 causal signals — failure class + skill attribution.

OpenSpace ``skill_engine/signals`` detector + linker pattern, rewritten
in Python. This module does **not** vendor OpenSpace, talk to OpenSpace
cloud, persist pickle embeddings, or implement HP-107 BM25. HP-108
skill→tools lives in ``hivepilot.skill_capabilities``. HP-109 persists
FIX/DERIVED/CAPTURED drafts; apply/commit is HP-111.

Contracts:

- Failure classes are ``tool`` / ``env`` / ``permission`` / ``skill_defect``.
  A network outage is ``env`` and must never become a skill fault.
- Trust mapping (HP-105): ``skill_defect`` with a unique skill context is
  ``attributed``; multiple or missing skill subjects are ``ambiguous``;
  ``tool`` / ``env`` / ``permission`` are ``not_skill``. Unrecognized
  evidence is ``ambiguous`` (review, never auto-demote).
- FIX is admissible only with a revision + causal event + representative
  result. This module gates that triple. Persisting a PASS draft is
  ``hivepilot.skill_evolution`` (HP-109); apply/commit is HP-111.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from hivepilot.skill_events import list_skill_events

FAILURE_CLASSES: tuple[str, ...] = ("tool", "env", "permission", "skill_defect")
TOOL = "tool"
ENV = "env"
PERMISSION = "permission"
SKILL_DEFECT = "skill_defect"

ATTRIBUTED = "attributed"
AMBIGUOUS = "ambiguous"
NOT_SKILL = "not_skill"

ACTIVE_SKILL_EVENT_TYPES: frozenset[str] = frozenset({"invoked", "applied"})

_PERMISSION_TOKENS: frozenset[str] = frozenset(
    {
        "401",
        "403",
        "blocked",
        "denied",
        "eacces",
        "eperm",
        "forbidden",
        "permission",
        "permission_denied",
        "rejected",
        "unauthorized",
    }
)
_ENV_TOKENS: frozenset[str] = frozenset(
    {
        "certificate",
        "connection",
        "connection_refused",
        "connection_reset",
        "dns",
        "econnrefused",
        "econnreset",
        "ehostunreach",
        "enetunreach",
        "enospace",
        "enospc",
        "env",
        "environment",
        "external",
        "network",
        "offline",
        "oom",
        "out_of_memory",
        "socket",
        "ssl",
        "timed_out",
        "timeout",
        "tls",
        "unreachable",
    }
)
_TOOL_TOKENS: frozenset[str] = frozenset(
    {
        "command_not_found",
        "exit_code",
        "tool",
        "tool_call_failed",
        "tool_crash",
        "tool_error",
        "tool_failure",
        "tool_timeout",
    }
)
_SKILL_TOKENS: frozenset[str] = frozenset(
    {
        "attributed",
        "misleading",
        "phase_failed",
        "skill",
        "skill_defect",
        "skill_fault",
        "skill_phase_failed",
        "wrong_guidance",
    }
)
_TOKEN_KEYS: tuple[str, ...] = (
    "attribution",
    "cause",
    "error_bucket",
    "error_code",
    "error_type",
    "exception_class",
    "exception_type",
    "failure_class",
    "failure_mode",
    "permission_status",
    "status",
)
_TEXT_KEYS: tuple[str, ...] = ("error_message", "message", "reason", "summary")
_RESULT_KEYS: tuple[str, ...] = (
    "representative_result",
    "result_ref",
    "result_id",
    "tool_result",
    "tool_result_id",
)
_CAUSAL_KEYS: tuple[str, ...] = (
    "causal_event_id",
    "causal_event",
    "event_id",
    "skill_event_id",
    "skill_event_ref",
)
_NETWORK_TEXT_RE = re.compile(
    r"\b("
    r"network|dns|socket|ssl|tls|offline|unreachable|"
    r"connection refused|connection reset|name or service not known|"
    r"nodename nor servname|temporary failure in name resolution|"
    r"no route to host|network is unreachable"
    r")\b",
    re.IGNORECASE,
)
_PERMISSION_TEXT_RE = re.compile(
    r"\b(permission denied|access denied|not authorized|unauthorized|forbidden)\b",
    re.IGNORECASE,
)
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_CAMEL_RE = re.compile(r"([a-z])([A-Z])")


@dataclass(frozen=True)
class SkillContext:
    """One linked skill subject for a failure (OpenSpace linker shape)."""

    revision_id: str
    logical_id: str = ""
    skill_name: str = ""
    event_id: str = ""
    link_type: str = ""


@dataclass(frozen=True)
class FailureAttribution:
    """Classified failure plus the HP-105 trust token it should feed."""

    failure_class: str
    trust_attribution: str
    revision_id: str = ""
    causal_event_id: str = ""
    representative_result: str = ""
    reason: str = ""
    skill_ids: tuple[str, ...] = ()
    failure_signature: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class,
            "trust_attribution": self.trust_attribution,
            "revision_id": self.revision_id,
            "causal_event_id": self.causal_event_id,
            "representative_result": self.representative_result,
            "reason": self.reason,
            "skill_ids": list(self.skill_ids),
            "failure_signature": self.failure_signature,
        }


@dataclass(frozen=True)
class FixEligibility:
    """FIX gate. Persist is HP-109; apply/commit is HP-111."""

    admissible: bool
    revision_id: str = ""
    causal_event_id: str = ""
    representative_result: str = ""
    missing: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "admissible": self.admissible,
            "revision_id": self.revision_id,
            "causal_event_id": self.causal_event_id,
            "representative_result": self.representative_result,
            "missing": list(self.missing),
            "reason": self.reason,
        }


def classify_failure(
    raw: str | None = None,
    payload: Mapping[str, Any] | None = None,
    *,
    revision_id: str = "",
    run_id: str = "",
    tenant: str = "default",
) -> FailureAttribution:
    """Detect the failure class and link it to zero, one, or many skills."""
    body = dict(payload or {})
    token = (raw or "").strip().lower()
    if not token:
        token = str(body.get("attribution") or body.get("cause") or "").strip().lower()
    failure_class, class_reason = detect_failure_class(token, body)
    contexts = link_skill_contexts(
        body,
        revision_id=revision_id,
        run_id=run_id,
        tenant=tenant,
    )
    representative = representative_result_id(body)
    causal = _first_text(body, *_CAUSAL_KEYS)
    skill_ids = tuple(ctx.revision_id for ctx in contexts if ctx.revision_id)
    if not skill_ids and revision_id.strip():
        skill_ids = (revision_id.strip(),)
    resolved_revision = ""
    if len(contexts) == 1:
        resolved_revision = contexts[0].revision_id
        if not causal:
            causal = contexts[0].event_id
    elif revision_id.strip():
        resolved_revision = revision_id.strip()
        match = next((ctx for ctx in contexts if ctx.revision_id == resolved_revision), None)
        if match is not None and not causal:
            causal = match.event_id

    trust, reason = _trust_for(
        failure_class,
        class_reason=class_reason,
        contexts=contexts,
        explicit_revision=bool(revision_id.strip() or _explicit_skill_ids(body)),
        token=token,
        payload=body,
    )
    signature = stable_failure_signature(
        failure_class,
        token=token,
        revision_id=resolved_revision,
        representative_result=representative,
    )
    return FailureAttribution(
        failure_class=failure_class,
        trust_attribution=trust,
        revision_id=resolved_revision,
        causal_event_id=causal,
        representative_result=representative,
        reason=reason,
        skill_ids=skill_ids,
        failure_signature=signature,
    )


def detect_failure_class(
    raw: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """Return ``(failure_class, reason)``. External faults win over skill."""
    body = dict(payload or {})
    exact = (raw or "").strip().lower()
    tokens = _collect_tokens(raw, body)
    text = _collect_text(body)

    if exact == "not_skill" or "not_skill" in tokens:
        return ENV, "external_not_skill"
    if _PERMISSION_TOKENS & tokens or _PERMISSION_TEXT_RE.search(text):
        return PERMISSION, "permission_denied_without_skill_evidence"
    if _ENV_TOKENS & tokens or _NETWORK_TEXT_RE.search(text):
        return ENV, "external_env_or_network"
    if _TOOL_TOKENS & tokens:
        return TOOL, "observe_tool_event_failed"
    if body.get("phase_failed") or body.get("skill_phase_failed") or _SKILL_TOKENS & tokens:
        return SKILL_DEFECT, "skill_phase_or_fault"
    if raw and str(raw).strip():
        return "", "unrecognized_token"
    return "", "missing_class"


def link_skill_contexts(
    payload: Mapping[str, Any] | None = None,
    *,
    revision_id: str = "",
    run_id: str = "",
    tenant: str = "default",
) -> list[SkillContext]:
    """Resolve unique vs ambiguous skill subjects for a failure.

    Mirrors OpenSpace ``resolve_skill_contexts``: an explicit revision or
    skill id wins; otherwise invoked/applied HP-104 events on the same run
    are candidates. Multiple unresolved subjects stay ambiguous.
    """
    body = dict(payload or {})
    explicit = _explicit_skill_ids(body)
    if revision_id.strip():
        explicit = (revision_id.strip(), *explicit)
    seen: dict[str, SkillContext] = {}
    events = _active_events(run_id=run_id, tenant=tenant, revision_id=revision_id)
    for event in events:
        if explicit and event.revision_id not in explicit and event.logical_id not in explicit:
            continue
        link_type = _skill_link_type(body, event)
        if not link_type and not revision_id.strip() and not explicit:
            continue
        if event.revision_id in seen:
            continue
        seen[event.revision_id] = SkillContext(
            revision_id=event.revision_id,
            logical_id=event.logical_id,
            skill_name=event.skill_name,
            event_id=event.event_id,
            link_type=link_type or "run_window",
        )
    if seen:
        return sorted(seen.values(), key=lambda item: item.revision_id)
    if len(explicit) == 1:
        rev = next(iter(explicit))
        return [
            SkillContext(
                revision_id=rev,
                link_type="explicit_revision",
                event_id=_first_text(body, *_CAUSAL_KEYS),
            )
        ]
    if len(explicit) > 1:
        return [SkillContext(revision_id=item, link_type="explicit_revision") for item in explicit]
    if revision_id.strip():
        return [
            SkillContext(
                revision_id=revision_id.strip(),
                link_type="caller_revision",
                event_id=_first_text(body, *_CAUSAL_KEYS),
            )
        ]
    return []


def representative_result_id(payload: Mapping[str, Any] | None = None) -> str:
    """A present, non-missing result identifier — empty when absent."""
    body = dict(payload or {})
    if body.get("missing") is True:
        return ""
    result = _first_text(body, *_RESULT_KEYS)
    if result.strip().lower() in {"", "missing", "none", "null"}:
        return ""
    return result


def assess_fix_eligibility(
    attribution: FailureAttribution | None = None,
    *,
    revision_id: str = "",
    causal_event_id: str = "",
    representative_result: str = "",
    failure_class: str = "",
    trust_attribution: str = "",
) -> FixEligibility:
    """FIX only with revision + causal event + representative result.

    External (``not_skill``) classes and ambiguous subjects are not
    FIX-admissible. This does not write a PASS card or apply a patch.
    """
    if attribution is not None:
        revision_id = revision_id or attribution.revision_id
        causal_event_id = causal_event_id or attribution.causal_event_id
        representative_result = representative_result or attribution.representative_result
        failure_class = failure_class or attribution.failure_class
        trust_attribution = trust_attribution or attribution.trust_attribution
    missing: list[str] = []
    if not (revision_id or "").strip():
        missing.append("revision")
    if not (causal_event_id or "").strip():
        missing.append("causal_event")
    if not (representative_result or "").strip():
        missing.append("representative_result")
    if failure_class and failure_class != SKILL_DEFECT:
        return FixEligibility(
            admissible=False,
            revision_id=revision_id,
            causal_event_id=causal_event_id,
            representative_result=representative_result,
            missing=tuple(missing),
            reason=f"fix_not_for_{failure_class or 'unknown'}",
        )
    if trust_attribution == NOT_SKILL:
        return FixEligibility(
            admissible=False,
            revision_id=revision_id,
            causal_event_id=causal_event_id,
            representative_result=representative_result,
            missing=tuple(missing),
            reason="fix_not_for_not_skill",
        )
    if trust_attribution == AMBIGUOUS:
        return FixEligibility(
            admissible=False,
            revision_id=revision_id,
            causal_event_id=causal_event_id,
            representative_result=representative_result,
            missing=tuple(missing),
            reason="fix_not_for_ambiguous",
        )
    if missing:
        return FixEligibility(
            admissible=False,
            revision_id=revision_id,
            causal_event_id=causal_event_id,
            representative_result=representative_result,
            missing=tuple(missing),
            reason="missing_" + "+".join(missing),
        )
    return FixEligibility(
        admissible=True,
        revision_id=revision_id.strip(),
        causal_event_id=causal_event_id.strip(),
        representative_result=representative_result.strip(),
        reason="revision_causal_event_representative_result",
    )


def draft_fix_proposal(attribution: FailureAttribution) -> dict[str, Any]:
    """Describe a FIX draft. Persist via ``skill_evolution.propose_fix``."""
    eligibility = assess_fix_eligibility(attribution)
    return {
        "kind": "skill_evolution",
        "action": "fix",
        "status": "draft" if eligibility.admissible else "blocked",
        "admissible": eligibility.admissible,
        "persisted": False,
        "proposal_id": "",
        "eligibility": eligibility.to_dict(),
        "attribution": attribution.to_dict(),
    }


def stable_failure_signature(
    failure_class: str,
    *,
    token: str = "",
    revision_id: str = "",
    representative_result: str = "",
) -> str:
    payload = [failure_class or "unknown", token, revision_id, representative_result]
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def _trust_for(
    failure_class: str,
    *,
    class_reason: str,
    contexts: Sequence[SkillContext],
    explicit_revision: bool,
    token: str,
    payload: Mapping[str, Any],
) -> tuple[str, str]:
    if failure_class in {TOOL, ENV, PERMISSION}:
        return NOT_SKILL, class_reason
    if failure_class != SKILL_DEFECT:
        return AMBIGUOUS, class_reason or "unrecognized"
    if len(contexts) > 1 and not explicit_revision:
        return AMBIGUOUS, "ambiguous_skill_context"
    if payload.get("phase_failed") or payload.get("skill_phase_failed") or token in _SKILL_TOKENS:
        if len(contexts) > 1 and not (explicit_revision or token):
            return AMBIGUOUS, "ambiguous_skill_context"
        return ATTRIBUTED, class_reason
    if len(contexts) == 1 or explicit_revision:
        return ATTRIBUTED, class_reason
    return AMBIGUOUS, "missing_skill_context"


def _active_events(
    *,
    run_id: str,
    tenant: str,
    revision_id: str,
) -> list[Any]:
    run = (run_id or "").strip()
    if not run:
        return []
    found: list[Any] = []
    for event_type in ACTIVE_SKILL_EVENT_TYPES:
        found.extend(
            list_skill_events(
                tenant=tenant or "default",
                run_id=run,
                revision_id=revision_id.strip() or None,
                event_type=event_type,
                limit=200,
            )
        )
    return found


def _skill_link_type(payload: Mapping[str, Any], event: Any) -> str:
    explicit = _explicit_skill_ids(payload)
    if event.revision_id in explicit or event.logical_id in explicit:
        return "explicit_invocation_scope"
    tool_use = _first_text(payload, "tool_use_id", "tool_call_id", "call_id")
    event_uses = _string_values(event.payload.get("tool_use_id"))
    event_uses.update(_string_values(event.payload.get("tool_use_ids")))
    event_uses.update(_string_values(event.payload.get("tool_call_id")))
    if tool_use and tool_use in event_uses:
        return "skill_event_tool_use"
    backrefs = _string_values(payload.get("raw_backrefs"))
    backrefs.update(_string_values(payload.get("skill_event_ref")))
    backrefs.update(_string_values(payload.get("skill_event_ids")))
    if event.event_id in backrefs:
        return "skill_event_backref"
    if (event.run_id or "") and event.run_id == str(payload.get("run_id") or event.run_id):
        return "run_window"
    return "run_window"


def _explicit_skill_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        for item in _string_values(raw):
            cleaned = item.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                values.append(cleaned)

    for key in (
        "revision_id",
        "skill_id",
        "logical_id",
        "skill_ids",
        "revision_ids",
        "active_skill_id",
        "active_skill_ids",
        "invoked_skill_id",
        "candidate_skill_ids",
    ):
        _add(payload.get(key))
    return tuple(values)


def _collect_tokens(raw: str | None, payload: Mapping[str, Any]) -> set[str]:
    tokens: set[str] = set()
    if raw:
        tokens.update(_tokenize(raw))
    for key in _TOKEN_KEYS:
        tokens.update(_tokenize(payload.get(key)))
    if payload.get("phase_failed") or payload.get("skill_phase_failed"):
        tokens.add("phase_failed")
    return {item for item in tokens if item}


def _collect_text(payload: Mapping[str, Any]) -> str:
    parts = [str(payload.get(key) or "") for key in _TEXT_KEYS]
    return " ".join(part for part in parts if part)


def _tokenize(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, bool):
        return set()
    if isinstance(value, (list, tuple, set)):
        tokens: set[str] = set()
        for item in value:
            tokens.update(_tokenize(item))
        return tokens
    raw_text = str(value).strip()
    if not raw_text:
        return set()
    text = raw_text.lower()
    spaced = _CAMEL_RE.sub(r"\1_\2", raw_text).lower()
    parts = {text, spaced, *{part for part in _TOKEN_SPLIT_RE.split(spaced) if part}}
    return parts


def _first_text(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _string_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value} if value.strip() else set()
    if isinstance(value, Mapping):
        found: set[str] = set()
        for item in value.values():
            found.update(_string_values(item))
        return found
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        found = set()
        for item in value:
            found.update(_string_values(item))
        return found
    text = str(value).strip()
    return {text} if text else set()
