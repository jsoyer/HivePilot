"""HP-113 eval contract — 0 model queries, explicit cases only.

Coworker evals/tool-safety pattern, rewritten in Python. Does not vendor
TS, call an LLM, or keyword-route skills/tools from an utterance.

Gates (default PR CI):

- ``denied`` / ``traversal`` / ``pending`` → ``effect_count == 0``
- ``approve_twice`` → ``effect_count == 1`` via the HP-100
  ``idempotency_key`` (second approve is a cache hit, not a second write)

Surfaces are explicit: ``tool`` (HP-95/100), ``memory`` (HP-101),
``skill_evolution`` (HP-110). Callers pass ``token`` / ``path``; this
module never infers them from chat text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from hivepilot.checkpoints import (
    approve_and_resume,
    open_pending_tool,
    resume,
)
from hivepilot.memory_proposals import (
    JSON_MEMORY,
    MemoryCorpus,
    decide_memory,
    propose_model_write,
    recall,
)
from hivepilot.pass_store import inbox
from hivepilot.side_effects import COMPLETED, mint_key
from hivepilot.side_effects import get as get_effect
from hivepilot.skill_evolution import DERIVED, propose
from hivepilot.skill_evolution_validator import REJECT, validate
from hivepilot.tool_catalog import ToolCatalog, ToolEntry
from hivepilot.workspace_text import IsolatedJsonMemory

DENIED = "denied"
TRAVERSAL = "traversal"
PENDING = "pending"
APPROVE_TWICE = "approve_twice"
GATES: tuple[str, ...] = (DENIED, TRAVERSAL, PENDING, APPROVE_TWICE)

TOOL = "tool"
MEMORY = "memory"
SKILL_EVOLUTION = "skill_evolution"
SURFACES: tuple[str, ...] = (TOOL, MEMORY, SKILL_EVOLUTION)

# ContractCase fields a keyword router would need. None are accepted.
ROUTE_FIELDS: frozenset[str] = frozenset(
    {"query", "message", "utterance", "prompt", "chat", "text"}
)

TRAVERSAL_PATH = "../../etc/passwd"
UNKNOWN_TOKEN = "TotallyUnknownTool"
DENIED_TOKEN = "DeniedTool"
PENDING_TOKEN = "Write"
APPROVE_TOKEN = "Write"

ExecuteFn = Callable[[Any], Any]


class ContractError(ValueError):
    """Invalid contract case or a failed gate assertion."""


@dataclass(frozen=True)
class ContractCase:
    """One explicit eval case. Token/path are required per surface — never inferred."""

    name: str
    gate: str
    surface: str
    token: str = ""
    path: str = ""
    idempotency_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "gate": self.gate,
            "surface": self.surface,
            "token": self.token,
            "path": self.path,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class ContractVerdict:
    """Outcome of ``run_case``. ``effect_count`` is the only mutation tally."""

    name: str
    gate: str
    surface: str
    effect_count: int
    mutated: bool
    cached_second: bool = False
    status: str = ""
    detail: str = ""
    idempotency_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "gate": self.gate,
            "surface": self.surface,
            "effect_count": self.effect_count,
            "mutated": self.mutated,
            "cached_second": self.cached_second,
            "status": self.status,
            "detail": self.detail,
            "idempotency_key": self.idempotency_key,
        }


def default_catalog() -> ToolCatalog:
    """In-memory catalog for contract cases. No YAML, no shipped tokens required."""
    return ToolCatalog(
        entries=(
            ToolEntry(
                token="Read",
                risk="low",
                default_policy="allow",
                volatile=False,
                idempotent=True,
            ),
            ToolEntry(
                token=PENDING_TOKEN,
                risk="medium",
                default_policy="require_approval",
                volatile=False,
                idempotent=True,
            ),
            ToolEntry(
                token=DENIED_TOKEN,
                risk="high",
                default_policy="deny",
                volatile=False,
                idempotent=True,
            ),
        )
    )


def expected_effects(gate: str) -> int:
    cleaned = (gate or "").strip().lower()
    if cleaned not in GATES:
        raise ContractError(f"gate must be one of {list(GATES)}; got {gate!r}")
    return 1 if cleaned == APPROVE_TWICE else 0


def uses_keyword_routing() -> bool:
    """True only if a routing field leaked onto ``ContractCase`` (it must not)."""
    return bool(set(ContractCase.__dataclass_fields__) & ROUTE_FIELDS)


def _normalize_case(case: ContractCase) -> ContractCase:
    gate = (case.gate or "").strip().lower()
    surface = (case.surface or "").strip().lower()
    if gate not in GATES:
        raise ContractError(f"gate must be one of {list(GATES)}; got {case.gate!r}")
    if surface not in SURFACES:
        raise ContractError(f"surface must be one of {list(SURFACES)}; got {case.surface!r}")
    if set(ContractCase.__dataclass_fields__) & ROUTE_FIELDS:
        raise ContractError("eval harness must not accept utterance/query routing fields")
    return case


def _key(case: ContractCase) -> str:
    return case.idempotency_key or f"hp113-{case.gate}-{case.surface}-{mint_key()}"


def _verdict(
    case: ContractCase,
    *,
    effect_count: int,
    mutated: bool,
    cached_second: bool = False,
    status: str = "",
    detail: str = "",
    idempotency_key: str = "",
) -> ContractVerdict:
    return ContractVerdict(
        name=case.name,
        gate=case.gate,
        surface=case.surface,
        effect_count=effect_count,
        mutated=mutated,
        cached_second=cached_second,
        status=status,
        detail=detail,
        idempotency_key=idempotency_key,
    )


def _run_tool(case: ContractCase, catalog: ToolCatalog) -> ContractVerdict:
    token = (case.token or "").strip()
    if not token:
        raise ContractError("tool cases require an explicit token")
    key = _key(case)
    calls: list[str] = []

    def execute(proposal: Any) -> dict[str, Any]:
        calls.append(getattr(proposal, "id", ""))
        return {"ok": True}

    opened = open_pending_tool(
        idempotency_key=key,
        token=token,
        payload={"path": case.path or "notes.md", "text": "x"},
        catalog=catalog,
    )
    if case.gate == PENDING:
        waiting = resume(key, execute=execute)
        return _verdict(
            case,
            effect_count=len(calls),
            mutated=bool(calls),
            status=waiting.status,
            detail=opened.proposal_id,
            idempotency_key=key,
        )
    if case.gate == DENIED:
        denied = resume(key, execute=execute)
        effect = get_effect(key)
        completed = effect is not None and effect.status == COMPLETED
        return _verdict(
            case,
            effect_count=len(calls) + (1 if completed and not calls else 0),
            mutated=bool(calls) or completed,
            status=denied.status,
            detail=opened.proposal_id,
            idempotency_key=key,
        )
    first = approve_and_resume(key, actor="eval", execute=execute)
    second = approve_and_resume(key, actor="eval", execute=execute)
    return _verdict(
        case,
        effect_count=len(calls),
        mutated=bool(calls),
        cached_second=second.cached,
        status=first.status,
        detail=opened.proposal_id,
        idempotency_key=key,
    )


def _run_memory(case: ContractCase) -> ContractVerdict:
    corpus = MemoryCorpus(memory=IsolatedJsonMemory())
    path = (case.path or "prefs").strip()
    if not path:
        raise ContractError("memory cases require an explicit path/key")
    proposal = propose_model_write(
        target=JSON_MEMORY,
        key=path,
        expected_revision=0,
        value={"body": "from-model"},
    )
    if case.gate == PENDING:
        return _verdict(
            case,
            effect_count=0 if recall(corpus, path) is None else 1,
            mutated=recall(corpus, path) is not None,
            status=proposal.status,
            detail=proposal.id,
            idempotency_key=str(proposal.payload.get("idempotency_key") or ""),
        )
    if case.gate == DENIED:
        rejected = decide_memory(proposal.id, "reject", corpus=corpus, actor="eval")
        return _verdict(
            case,
            effect_count=0 if recall(corpus, path) is None else 1,
            mutated=recall(corpus, path) is not None,
            status=rejected.proposal.status,
            detail=proposal.id,
            idempotency_key=rejected.idempotency_key,
        )
    first = decide_memory(proposal.id, "approve", corpus=corpus, actor="eval")
    second = decide_memory(proposal.id, "approve", corpus=corpus, actor="eval")
    revision = 0
    if first.apply is not None:
        revision = first.apply.revision
    envelope = recall(corpus, path)
    if envelope is not None:
        revision = envelope.revision
    return _verdict(
        case,
        effect_count=revision,
        mutated=revision > 0,
        cached_second=second.cached,
        status=first.proposal.status,
        detail=proposal.id,
        idempotency_key=first.idempotency_key,
    )


def _run_skill_evolution(case: ContractCase) -> ContractVerdict:
    path = (case.path or TRAVERSAL_PATH).strip()
    if not path:
        raise ContractError("skill_evolution cases require an explicit path")
    files: Mapping[str, str] = {path: "# leak\n"}
    verdict = validate(files=files)
    before = len(inbox(kind=SKILL_EVOLUTION))
    draft = propose(
        evolution_type=DERIVED,
        name="eval-leaky",
        files=files,
        parent_logical_ids=("eval-parent",),
    )
    after = len(inbox(kind=SKILL_EVOLUTION))
    persisted = bool(draft.persisted) or after > before
    mutated = bool(verdict.mutated) or persisted
    return _verdict(
        case,
        effect_count=1 if mutated else 0,
        mutated=mutated,
        status=verdict.result,
        detail=draft.reason or verdict.reason,
    )


def run_case(
    case: ContractCase,
    *,
    catalog: ToolCatalog | None = None,
) -> ContractVerdict:
    """Drive one explicit case. No utterance argument, no skill keyword match."""
    normalized = _normalize_case(case)
    loaded = catalog if catalog is not None else default_catalog()
    if normalized.surface == TOOL:
        return _run_tool(normalized, loaded)
    if normalized.surface == MEMORY:
        return _run_memory(normalized)
    return _run_skill_evolution(normalized)


def default_cases() -> tuple[ContractCase, ...]:
    """Canonical 0-model suite. Token/path are literals, not routed."""
    return (
        ContractCase(
            name="tool-denied-unknown",
            gate=DENIED,
            surface=TOOL,
            token=UNKNOWN_TOKEN,
        ),
        ContractCase(
            name="tool-denied-policy",
            gate=DENIED,
            surface=TOOL,
            token=DENIED_TOKEN,
        ),
        ContractCase(
            name="tool-pending",
            gate=PENDING,
            surface=TOOL,
            token=PENDING_TOKEN,
        ),
        ContractCase(
            name="tool-approve-twice",
            gate=APPROVE_TWICE,
            surface=TOOL,
            token=APPROVE_TOKEN,
        ),
        ContractCase(
            name="memory-denied-reject",
            gate=DENIED,
            surface=MEMORY,
            path="prefs",
        ),
        ContractCase(
            name="memory-pending",
            gate=PENDING,
            surface=MEMORY,
            path="prefs",
        ),
        ContractCase(
            name="memory-approve-twice",
            gate=APPROVE_TWICE,
            surface=MEMORY,
            path="prefs",
        ),
        ContractCase(
            name="skill-traversal",
            gate=TRAVERSAL,
            surface=SKILL_EVOLUTION,
            path=TRAVERSAL_PATH,
        ),
    )


def run_suite(
    cases: Sequence[ContractCase] | None = None,
    *,
    catalog: ToolCatalog | None = None,
) -> list[ContractVerdict]:
    loaded = catalog if catalog is not None else default_catalog()
    return [run_case(item, catalog=loaded) for item in (cases or default_cases())]


def check_verdict(verdict: ContractVerdict) -> ContractVerdict:
    """Fail closed: wrong effect_count or an unexpected mutation."""
    expected = expected_effects(verdict.gate)
    if verdict.effect_count != expected:
        raise ContractError(
            f"{verdict.name}: expected {expected} effect(s), got {verdict.effect_count}"
        )
    if verdict.gate != APPROVE_TWICE and verdict.mutated:
        raise ContractError(f"{verdict.name}: {verdict.gate} must not mutate")
    if verdict.gate == APPROVE_TWICE and not verdict.cached_second:
        raise ContractError(f"{verdict.name}: second approve must be idempotent")
    if verdict.gate == TRAVERSAL and verdict.status != REJECT:
        raise ContractError(f"{verdict.name}: traversal must reject, got {verdict.status!r}")
    return verdict


def check_suite(
    cases: Sequence[ContractCase] | None = None,
    *,
    catalog: ToolCatalog | None = None,
) -> list[ContractVerdict]:
    verdicts = run_suite(cases, catalog=catalog)
    for verdict in verdicts:
        check_verdict(verdict)
    return verdicts
