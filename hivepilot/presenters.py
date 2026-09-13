"""HP-102 presenter parity — Pollen cards ↔ Telegram Approvals door.

Coworker telegram-bridge + workspace-text-approval patterns, rewritten
in Python. Does not vendor TS.

One shared ``decide_approval()``. Pollen cards and Telegram keyboards
carry the same ``approval_id`` and produce a single resume. Cards and
keyboards are legal only on the Approvals door. Owner + TTL are enforced
via ``pending_confirmation``. Four Telegram doors stay
inbox | approvals | runs | alerts. No WhatsApp (HP-129), no HP-67, no
fifth topic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from hivepilot.checkpoints import (
    ExecuteFn,
    ResumeResult,
    approve_and_resume,
    get_checkpoint_by_proposal,
    resume,
)
from hivepilot.memory_proposals import MemoryCorpus, MemoryDecision, decide_memory
from hivepilot.pass_store import (
    PENDING,
    PassProposal,
    PassStoreError,
    decide,
    inbox,
)
from hivepilot.pass_store import (
    get as get_proposal,
)
from hivepilot.services.pending_confirmation import PendingConfirmationStore
from hivepilot.services.telegram_doors import (
    APPROVALS,
    PERSISTENT_DOORS,
    approval_actions_allowed,
)

# Surfaces that share one approval_id. WhatsApp is HP-129 DEFER.
POLLEN = "pollen"
TELEGRAM = "telegram"
SURFACES: tuple[str, ...] = (POLLEN, TELEGRAM)

# Re-export the four doors so callers cannot invent a fifth topic.
TELEGRAM_DOORS: tuple[str, ...] = PERSISTENT_DOORS
APPROVAL_DOOR = APPROVALS

DECISIONS: tuple[str, ...] = ("approve", "reject", "edit")
CALLBACK_PREFIX = "pass"

# Same order of magnitude as Telegram challenge TTL (15 min): a presented
# card is an operator action, not an abandoned keyboard that should live
# forever.
DEFAULT_TTL_SECONDS = 15 * 60

MEMORY_KIND = "memory"
TOOL_KIND = "tool"


class PresenterError(ValueError):
    """Invalid present / decide / door / owner+TTL failure."""


@dataclass(frozen=True)
class TelegramKeyboard:
    """Inline keyboard for the Approvals door. ``approval_id`` matches Pollen."""

    approval_id: str
    door: str
    text: str
    buttons: tuple[tuple[dict[str, str], ...], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "door": self.door,
            "text": self.text,
            "buttons": [list(row) for row in self.buttons],
        }


@dataclass(frozen=True)
class PresenterCard:
    """One card shown on Pollen and Telegram. Same ``approval_id`` on both."""

    approval_id: str
    kind: str
    status: str
    project: str
    task: str
    action: str
    title: str
    summary: str
    owner_id: str
    door: str = APPROVAL_DOOR
    surfaces: tuple[str, ...] = SURFACES

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "kind": self.kind,
            "status": self.status,
            "project": self.project,
            "task": self.task,
            "action": self.action,
            "title": self.title,
            "summary": self.summary,
            "owner_id": self.owner_id,
            "door": self.door,
            "surfaces": list(self.surfaces),
        }


@dataclass(frozen=True)
class ApprovalDecision:
    """Outcome of ``decide_approval``. ``approval_id`` is the presented id."""

    approval_id: str
    decision: str
    surface: str
    proposal: PassProposal
    resumed: bool
    resume: ResumeResult | MemoryDecision | None = None
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        resume_payload: dict[str, Any] | None
        if self.resume is None:
            resume_payload = None
        else:
            resume_payload = self.resume.to_dict()
        return {
            "approval_id": self.approval_id,
            "decision": self.decision,
            "surface": self.surface,
            "proposal": self.proposal.to_dict(),
            "resumed": self.resumed,
            "resume": resume_payload,
            "cached": self.cached,
        }


_pending: PendingConfirmationStore[PresenterCard] = PendingConfirmationStore(DEFAULT_TTL_SECONDS)
_memory_corpus = MemoryCorpus()


def reset_pending() -> None:
    """Test helper: drop every presented card and the process corpus."""
    global _memory_corpus
    _pending.clear()
    _memory_corpus = MemoryCorpus()


def memory_corpus() -> MemoryCorpus:
    """Process-local IsolatedJsonMemory + workspace snapshots for apply."""
    return _memory_corpus


def normalize_surface(raw: str) -> str:
    cleaned = (raw or "").strip().lower()
    if cleaned not in SURFACES:
        raise PresenterError(f"surface must be one of {list(SURFACES)}; got {raw!r}")
    return cleaned


def normalize_decision(raw: str) -> str:
    cleaned = (raw or "").strip().lower()
    aliases = {
        "approve": "approve",
        "approved": "approve",
        "reject": "reject",
        "rejected": "reject",
        "deny": "reject",
        "denied": "reject",
        "edit": "edit",
        "edited": "edit",
    }
    token = aliases.get(cleaned)
    if token is None:
        raise PresenterError(f"decision must be one of {list(DECISIONS)}; got {raw!r}")
    return token


def callback_data(decision: str, approval_id: str) -> str:
    """Telegram callback_data — same ``approval_id`` Pollen POSTs."""
    token = normalize_decision(decision)
    if token == "reject":
        token = "deny"
    cleaned_id = (approval_id or "").strip()
    if not cleaned_id:
        raise PresenterError("approval_id is required")
    return f"{CALLBACK_PREFIX}:{token}:{cleaned_id}"


def parse_callback(data: str) -> tuple[str, str] | None:
    """Parse ``pass:<approve|deny|edit>:<approval_id>``. None if foreign."""
    raw = (data or "").strip()
    parts = raw.split(":", 2)
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    try:
        decision = normalize_decision(parts[1])
    except PresenterError:
        return None
    approval_id = parts[2].strip()
    if not approval_id:
        return None
    return decision, approval_id


def _summary(proposal: PassProposal) -> str:
    payload = proposal.payload
    if proposal.kind == MEMORY_KIND:
        target = str(payload.get("target") or "")
        key = str(payload.get("key") or payload.get("path") or "")
        return " ".join(part for part in (target, key) if part).strip() or proposal.action
    token = proposal.action or str(payload.get("token") or payload.get("action") or "")
    return token or proposal.kind


def _title(proposal: PassProposal) -> str:
    kind = proposal.kind
    if kind == MEMORY_KIND:
        return "Memory write"
    if kind == TOOL_KIND:
        return "Tool call"
    if kind == "skill_evolution":
        return "Skill evolution"
    if kind == "partition":
        return "Partition"
    return kind


def _card_from_proposal(proposal: PassProposal, owner_id: str) -> PresenterCard:
    return PresenterCard(
        approval_id=proposal.id,
        kind=proposal.kind,
        status=proposal.status,
        project=proposal.project,
        task=proposal.task,
        action=proposal.action,
        title=_title(proposal),
        summary=_summary(proposal),
        owner_id=owner_id,
        door=APPROVAL_DOOR,
        surfaces=SURFACES,
    )


def present(proposal: PassProposal, *, owner_id: str) -> PresenterCard:
    """Bind owner+TTL and return the card for both surfaces.

    ``approval_id`` is the PASS proposal id. A second ``present`` for the
    same id is idempotent when the owner matches; a different owner cannot
    steal the pending card.
    """
    if not owner_id:
        raise PresenterError("owner_id is required")
    if proposal.status != PENDING:
        raise PresenterError(f"proposal {proposal.id} is {proposal.status}, not PENDING")
    existing = _pending.resolve(proposal.id, owner_id)
    if existing is not None:
        return existing
    if proposal.id in _pending:
        raise PresenterError(f"approval {proposal.id} is owned by someone else")
    card = _card_from_proposal(proposal, owner_id)
    _pending.store(proposal.id, owner_id, card)
    if proposal.id not in _pending:
        raise PresenterError("failed to store pending confirmation")
    return card


def pollen_card(card: PresenterCard) -> dict[str, Any]:
    """Pollen Approvals-door payload. Same ``approval_id`` as Telegram."""
    if not approval_actions_allowed(card.door):
        raise PresenterError("Pollen cards are only legal on the Approvals door")
    body = card.to_dict()
    body["surface"] = POLLEN
    return body


def telegram_keyboard(card: PresenterCard, *, door: str) -> TelegramKeyboard | None:
    """Keyboard only on the Approvals door. Inbox/Runs/Alerts get nothing."""
    if door != APPROVAL_DOOR or not approval_actions_allowed(door):
        return None
    approve = callback_data("approve", card.approval_id)
    deny = callback_data("deny", card.approval_id)
    buttons: list[tuple[dict[str, str], ...]] = [
        (
            {"text": "✅ Approve", "callback_data": approve},
            {"text": "❌ Deny", "callback_data": deny},
        )
    ]
    if card.kind == MEMORY_KIND:
        buttons.append(
            ({"text": "✏️ Edit", "callback_data": callback_data("edit", card.approval_id)},)
        )
    text = f"{card.title}\n{card.summary}\n{card.project} / {card.task}".strip()
    return TelegramKeyboard(
        approval_id=card.approval_id,
        door=APPROVAL_DOOR,
        text=text,
        buttons=tuple(buttons),
    )


def _tool_checkpoint_key(proposal: PassProposal) -> str:
    checkpoint = get_checkpoint_by_proposal(proposal.id)
    if checkpoint is None:
        return ""
    return checkpoint.idempotency_key


def _resume_tool(
    proposal: PassProposal,
    *,
    decision: str,
    actor: str,
    reason: str,
    execute: ExecuteFn | None,
) -> tuple[PassProposal, ResumeResult | None, bool]:
    key = _tool_checkpoint_key(proposal)
    if decision == "approve" and key:
        result = approve_and_resume(key, actor=actor, reason=reason, execute=execute)
        stored = get_proposal(proposal.id) or proposal
        cached = bool(result.cached)
        return stored, result, cached
    if decision == "approve" and not key:
        stored = decide(proposal.id, "approve", actor=actor, reason=reason)
        return stored, None, False
    stored = decide(proposal.id, "reject", actor=actor, reason=reason)
    if key:
        result = resume(key, execute=None)
        return stored, result, bool(result.cached)
    return stored, None, False


def decide_approval(
    approval_id: str,
    decision: str,
    *,
    owner_id: str,
    surface: str,
    actor: str = "",
    reason: str = "",
    corpus: MemoryCorpus | None = None,
    edited_payload: Mapping[str, Any] | None = None,
    execute: ExecuteFn | None = None,
) -> ApprovalDecision:
    """Single decide/resume path for Pollen and Telegram.

    Owner + TTL are checked first (fail closed). The PASS status is
    persisted before any resume. A second surface calling the same
    ``approval_id`` cannot start a second resume.
    """
    cleaned_id = (approval_id or "").strip()
    if not cleaned_id:
        raise PresenterError("approval_id is required")
    surface_name = normalize_surface(surface)
    token = normalize_decision(decision)
    card = _pending.resolve(cleaned_id, owner_id)
    if card is None:
        raise PresenterError("approval is expired, missing, or not owned by this actor")
    proposal = get_proposal(cleaned_id)
    if proposal is None:
        raise PassStoreError(f"proposal not found: {cleaned_id}")

    decided_by = (actor or owner_id).strip()
    resume_payload: ResumeResult | MemoryDecision | None = None
    resumed = False
    cached = False

    if proposal.kind == MEMORY_KIND:
        if corpus is None:
            raise PresenterError("memory decisions require a corpus")
        memory = decide_memory(
            cleaned_id,
            token,
            corpus=corpus,
            actor=decided_by,
            reason=reason,
            edited_payload=edited_payload,
        )
        resume_payload = memory
        resumed = memory.applied
        cached = memory.cached
        stored = memory.proposal
    elif token == "edit":
        raise PresenterError(f"edit is only valid for kind=memory; got {proposal.kind}")
    elif proposal.kind == TOOL_KIND:
        stored, resume_payload, cached = _resume_tool(
            proposal,
            decision=token,
            actor=decided_by,
            reason=reason,
            execute=execute,
        )
        resumed = bool(resume_payload is not None and getattr(resume_payload, "executed", False))
        if resume_payload is not None and getattr(resume_payload, "cached", False):
            cached = True
    else:
        stored = decide(
            cleaned_id,
            token,
            actor=decided_by,
            reason=reason,
            edited_payload=edited_payload if token == "edit" else None,
        )

    if stored.status != PENDING:
        _pending.discard(cleaned_id)
    return ApprovalDecision(
        approval_id=cleaned_id,
        decision=token,
        surface=surface_name,
        proposal=stored,
        resumed=resumed,
        resume=resume_payload,
        cached=cached,
    )


def pending_cards(*, tenant: str | None = None) -> list[PassProposal]:
    """PASS inbox rows the Approvals door can present."""
    return inbox(status=PENDING, tenant=tenant)


def present_pending(
    *,
    owner_id: str,
    tenant: str | None = None,
) -> list[PresenterCard]:
    """Present every PENDING inbox row to both surfaces."""
    cards: list[PresenterCard] = []
    for proposal in pending_cards(tenant=tenant):
        try:
            cards.append(present(proposal, owner_id=owner_id))
        except PresenterError:
            existing = _pending.resolve(proposal.id, owner_id)
            if existing is not None:
                cards.append(existing)
    return cards
