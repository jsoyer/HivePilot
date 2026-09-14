"""HP-113 behavior-memory eval — opt-in nightly, 0 keyword routing.

Coworker ``memory.eval`` pattern, rewritten in Python. Does not vendor TS,
call an LLM, or map an utterance onto ``memory.write``.

Default PR CI does **not** run this suite. Operators opt in with
``HIVEPILOT_BEHAVIOR_MEMORY_EVAL=1`` (GitHub Actions repository variable
and/or ``workflow_dispatch`` on ``.github/workflows/nightly.yml``).

Scenarios are named literals (``pending_unchanged``, ``approve_twice``,
…). There is no query/message argument that selects a skill or tool.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from hivepilot.eval_contract import ROUTE_FIELDS
from hivepilot.memory_proposals import (
    ALWAYS_ALLOW_TOKENS,
    JSON_MEMORY,
    WORKSPACE_TEXT,
    MemoryCorpus,
    MemoryProposalError,
    decide_memory,
    propose_model_write,
    recall,
    user_write,
)
from hivepilot.pass_store import APPROVED, PENDING, REJECTED
from hivepilot.workspace_text import IsolatedJsonMemory

BEHAVIOR_MEMORY_EVAL_ENV = "HIVEPILOT_BEHAVIOR_MEMORY_EVAL"

PENDING_UNCHANGED = "pending_unchanged"
REJECT_UNCHANGED = "reject_unchanged"
APPROVE_TWICE = "approve_twice"
EDIT_THEN_APPROVE = "edit_then_approve"
RECALL_NO_APPROVAL = "recall_no_approval"
USER_WRITE_DIRECT = "user_write_direct"
REFUSE_ALWAYS_ALLOW = "refuse_always_allow"
SCENARIOS: tuple[str, ...] = (
    PENDING_UNCHANGED,
    REJECT_UNCHANGED,
    APPROVE_TWICE,
    EDIT_THEN_APPROVE,
    RECALL_NO_APPROVAL,
    USER_WRITE_DIRECT,
    REFUSE_ALWAYS_ALLOW,
)


class BehaviorMemoryEvalError(ValueError):
    """Invalid scenario name or a failed behavior-memory assertion."""


@dataclass(frozen=True)
class BehaviorCase:
    """One explicit memory scenario. Key/path are required — never inferred."""

    name: str
    scenario: str
    key: str = "prefs"
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scenario": self.scenario,
            "key": self.key,
            "path": self.path,
        }


@dataclass(frozen=True)
class BehaviorVerdict:
    name: str
    scenario: str
    effect_count: int
    mutated: bool
    cached_second: bool = False
    status: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scenario": self.scenario,
            "effect_count": self.effect_count,
            "mutated": self.mutated,
            "cached_second": self.cached_second,
            "status": self.status,
            "detail": self.detail,
        }


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Nightly flag. Unset / any value other than ``1`` is off."""
    source = os.environ if environ is None else environ
    return source.get(BEHAVIOR_MEMORY_EVAL_ENV, "") == "1"


def uses_keyword_routing() -> bool:
    return bool(set(BehaviorCase.__dataclass_fields__) & ROUTE_FIELDS)


def _corpus() -> MemoryCorpus:
    return MemoryCorpus(memory=IsolatedJsonMemory())


def _normalize(case: BehaviorCase) -> BehaviorCase:
    scenario = (case.scenario or "").strip().lower()
    if scenario not in SCENARIOS:
        raise BehaviorMemoryEvalError(
            f"scenario must be one of {list(SCENARIOS)}; got {case.scenario!r}"
        )
    if set(BehaviorCase.__dataclass_fields__) & ROUTE_FIELDS:
        raise BehaviorMemoryEvalError(
            "behavior-memory harness must not accept utterance/query routing fields"
        )
    return case


def _verdict(
    case: BehaviorCase,
    *,
    effect_count: int,
    mutated: bool,
    cached_second: bool = False,
    status: str = "",
    detail: str = "",
) -> BehaviorVerdict:
    return BehaviorVerdict(
        name=case.name,
        scenario=case.scenario,
        effect_count=effect_count,
        mutated=mutated,
        cached_second=cached_second,
        status=status,
        detail=detail,
    )


def _run_pending(case: BehaviorCase) -> BehaviorVerdict:
    corpus = _corpus()
    proposal = propose_model_write(
        key=case.key,
        expected_revision=0,
        value={"body": "model"},
    )
    return _verdict(
        case,
        effect_count=0 if recall(corpus, case.key) is None else 1,
        mutated=recall(corpus, case.key) is not None,
        status=proposal.status,
        detail=proposal.id,
    )


def _run_reject(case: BehaviorCase) -> BehaviorVerdict:
    corpus = _corpus()
    user_write(corpus, key=case.key, expected_revision=0, value={"body": "keep"}, actor="pollen")
    proposal = propose_model_write(
        key=case.key,
        expected_revision=1,
        value={"body": "overwrite"},
    )
    rejected = decide_memory(proposal.id, "reject", corpus=corpus, actor="eval")
    envelope = recall(corpus, case.key)
    kept = envelope is not None and envelope.payload == {"body": "keep"}
    return _verdict(
        case,
        effect_count=0 if kept and envelope.revision == 1 else 1,
        mutated=not kept,
        status=rejected.proposal.status,
        detail=proposal.id,
    )


def _run_approve_twice(case: BehaviorCase) -> BehaviorVerdict:
    corpus = _corpus()
    proposal = propose_model_write(
        key=case.key,
        expected_revision=0,
        value={"body": "once"},
    )
    first = decide_memory(proposal.id, "approve", corpus=corpus, actor="eval")
    second = decide_memory(proposal.id, "approve", corpus=corpus, actor="eval")
    envelope = recall(corpus, case.key)
    revision = envelope.revision if envelope is not None else 0
    return _verdict(
        case,
        effect_count=revision,
        mutated=revision > 0,
        cached_second=second.cached,
        status=first.proposal.status,
        detail=proposal.id,
    )


def _run_edit_then_approve(case: BehaviorCase) -> BehaviorVerdict:
    corpus = _corpus()
    proposal = propose_model_write(
        key=case.key,
        expected_revision=0,
        value={"body": "from-model"},
    )
    edited = decide_memory(
        proposal.id,
        "edit",
        corpus=corpus,
        actor="eval",
        edited_payload={"value": {"body": "from-user"}},
    )
    if recall(corpus, case.key) is not None:
        return _verdict(
            case,
            effect_count=1,
            mutated=True,
            status=edited.proposal.status,
            detail="edit mutated corpus",
        )
    approved = decide_memory(proposal.id, "approve", corpus=corpus, actor="eval")
    envelope = recall(corpus, case.key)
    applied_user = envelope is not None and envelope.payload == {"body": "from-user"}
    return _verdict(
        case,
        effect_count=1 if applied_user else 0,
        mutated=applied_user,
        status=approved.proposal.status,
        detail=proposal.id,
    )


def _run_recall(case: BehaviorCase) -> BehaviorVerdict:
    corpus = _corpus()
    user_write(corpus, key=case.key, expected_revision=0, value={"ok": True}, actor="vault")
    pending = propose_model_write(key="other", expected_revision=0, value={"n": 1})
    envelope = recall(corpus, case.key)
    return _verdict(
        case,
        effect_count=0 if pending.status == PENDING else 1,
        mutated=envelope is None,
        status=pending.status,
        detail="" if envelope is None else str(envelope.payload),
    )


def _run_user_write(case: BehaviorCase) -> BehaviorVerdict:
    corpus = _corpus()
    path = (case.path or "notes.md").strip()
    pollen = user_write(
        corpus,
        key=case.key,
        expected_revision=0,
        value={"theme": "light"},
        actor="pollen",
    )
    workspace = user_write(
        corpus,
        target=WORKSPACE_TEXT,
        path=path,
        expected_revision=0,
        old_text="",
        new_text="hello",
        actor="vault",
    )
    ok = pollen.ok and workspace.ok
    return _verdict(
        case,
        effect_count=1 if ok else 0,
        mutated=ok,
        status="direct",
        detail=f"{JSON_MEMORY}+{WORKSPACE_TEXT}",
    )


def _run_refuse_always_allow(case: BehaviorCase) -> BehaviorVerdict:
    refused = 0
    for token in sorted(ALWAYS_ALLOW_TOKENS):
        try:
            propose_model_write(
                key=case.key,
                expected_revision=0,
                value={"x": 1},
                policy=token,
            )
        except MemoryProposalError:
            refused += 1
    try:
        propose_model_write(
            key=case.key,
            expected_revision=0,
            value={"x": 1},
            always_allow=True,
        )
    except MemoryProposalError:
        refused += 1
    expected = len(ALWAYS_ALLOW_TOKENS) + 1
    return _verdict(
        case,
        effect_count=0 if refused == expected else 1,
        mutated=refused != expected,
        status="refused" if refused == expected else "leaked",
        detail=f"{refused}/{expected}",
    )


_RUNNERS = {
    PENDING_UNCHANGED: _run_pending,
    REJECT_UNCHANGED: _run_reject,
    APPROVE_TWICE: _run_approve_twice,
    EDIT_THEN_APPROVE: _run_edit_then_approve,
    RECALL_NO_APPROVAL: _run_recall,
    USER_WRITE_DIRECT: _run_user_write,
    REFUSE_ALWAYS_ALLOW: _run_refuse_always_allow,
}


def run_case(case: BehaviorCase) -> BehaviorVerdict:
    """Drive one named scenario. No utterance argument."""
    normalized = _normalize(case)
    return _RUNNERS[normalized.scenario](normalized)


def default_cases() -> tuple[BehaviorCase, ...]:
    return tuple(
        BehaviorCase(name=scenario, scenario=scenario, key="prefs", path="notes.md")
        for scenario in SCENARIOS
    )


def run_suite(cases: Sequence[BehaviorCase] | None = None) -> list[BehaviorVerdict]:
    return [run_case(item) for item in (cases or default_cases())]


def check_verdict(verdict: BehaviorVerdict) -> BehaviorVerdict:
    if verdict.scenario in {PENDING_UNCHANGED, REJECT_UNCHANGED, REFUSE_ALWAYS_ALLOW}:
        if verdict.effect_count != 0 or verdict.mutated:
            raise BehaviorMemoryEvalError(f"{verdict.name}: {verdict.scenario} must not mutate")
        if verdict.scenario == REJECT_UNCHANGED and verdict.status != REJECTED:
            raise BehaviorMemoryEvalError(f"{verdict.name}: reject must persist REJECTED")
        return verdict
    if verdict.scenario == APPROVE_TWICE:
        if verdict.effect_count != 1 or not verdict.cached_second:
            raise BehaviorMemoryEvalError(
                f"{verdict.name}: approve×2 must be one effect (idempotent)"
            )
        if verdict.status != APPROVED:
            raise BehaviorMemoryEvalError(f"{verdict.name}: approve must persist APPROVED")
        return verdict
    if verdict.scenario in {EDIT_THEN_APPROVE, USER_WRITE_DIRECT}:
        if verdict.effect_count != 1 or not verdict.mutated:
            raise BehaviorMemoryEvalError(f"{verdict.name}: expected one applied write")
        return verdict
    if verdict.scenario == RECALL_NO_APPROVAL:
        if verdict.effect_count != 0 or verdict.mutated:
            raise BehaviorMemoryEvalError(f"{verdict.name}: recall must not require approval")
        return verdict
    raise BehaviorMemoryEvalError(f"unhandled scenario: {verdict.scenario}")


def check_suite(cases: Sequence[BehaviorCase] | None = None) -> list[BehaviorVerdict]:
    verdicts = run_suite(cases)
    for verdict in verdicts:
        check_verdict(verdict)
    return verdicts
