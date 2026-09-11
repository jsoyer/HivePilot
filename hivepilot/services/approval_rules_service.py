"""HP-61: persistable per-action approval rules + policy hook.

Rules never invent a Disposition field. ``auto`` is ``approve`` or ``deny``.
No match → pending (human). Empty match fields are wildcards. More specific
rules win.

HP-86: optional ``change_class`` on the same rule. ``auto=approve`` only
fires when the winning rule is ``mechanical``. Unknown / contested /
human-gate classes fail closed to pending. Watcher wake is not a class.
``auto=deny`` is unchanged.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from typing import Any, Literal

from hivepilot.services import db, state_service

Auto = Literal["approve", "deny"]
ChangeClass = Literal["mechanical", "product_fork", "security", "destructive", "unknown"]

MECHANICAL: ChangeClass = "mechanical"
PRODUCT_FORK: ChangeClass = "product_fork"
SECURITY: ChangeClass = "security"
DESTRUCTIVE: ChangeClass = "destructive"
UNKNOWN: ChangeClass = "unknown"

CHANGE_CLASSES: frozenset[str] = frozenset(
    {MECHANICAL, PRODUCT_FORK, SECURITY, DESTRUCTIVE, UNKNOWN}
)
HUMAN_GATE_CLASSES: frozenset[str] = CHANGE_CLASSES - {MECHANICAL}

# Metadata keys that must never be treated as a change class (HP-86).
# ``kind`` is already the HP-61 action token (e.g. pipeline_checkpoint).
_NOT_A_CLASS_KEYS: frozenset[str] = frozenset(
    {"source", "watcher", "woke", "wake", "event", "bus_kind", "kind", "action", "step_name"}
)
_CLAIM_KEYS: tuple[str, ...] = ("change_class", "class")
_TRUTHY = frozenset({"true", "1", "yes", "contested"})


class ApprovalRuleError(ValueError):
    """Invalid rule payload."""


@dataclass
class ApprovalRule:
    id: str
    project: str
    task: str
    action: str
    auto: Auto
    change_class: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _ensure_table() -> None:
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approval_action_rules (
                id TEXT PRIMARY KEY,
                project TEXT NOT NULL DEFAULT '',
                task TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                auto TEXT NOT NULL,
                change_class TEXT NOT NULL DEFAULT ''
            )
            """
        )
        state_service._add_column_if_missing(
            conn, "approval_action_rules", "change_class TEXT NOT NULL DEFAULT ''"
        )


def _row(raw: Any) -> ApprovalRule:
    try:
        stored_class = str(raw["change_class"] or "")
    except (KeyError, IndexError):
        stored_class = ""
    return ApprovalRule(
        id=str(raw["id"]),
        project=str(raw["project"] or ""),
        task=str(raw["task"] or ""),
        action=str(raw["action"] or ""),
        auto=raw["auto"],  # type: ignore[arg-type]
        change_class=stored_class,
    )


def list_rules() -> list[ApprovalRule]:
    _ensure_table()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, project, task, action, auto, change_class "
            "FROM approval_action_rules ORDER BY id"
        ).fetchall()
    return [_row(row) for row in rows]


def normalize_change_class(raw: str, *, persist: bool = False) -> str:
    """Canonicalize a class token.

    Empty stays empty on persist (optional field) and resolves to ``unknown``
    at match time. Unrecognized values raise when ``persist`` is true so a
    typo cannot silently store a mechanical label; request metadata coerces
    to ``unknown`` instead.
    """
    cleaned = (raw or "").strip().lower().replace("-", "_")
    if not cleaned:
        return "" if persist else UNKNOWN
    if cleaned == "contested":
        return UNKNOWN
    if cleaned == "productfork":
        cleaned = PRODUCT_FORK
    if cleaned in CHANGE_CLASSES:
        return cleaned
    if persist:
        raise ApprovalRuleError(
            "change_class must be mechanical, product_fork, security, "
            "destructive, unknown, or empty"
        )
    return UNKNOWN


def _normalize(
    auto: str, project: str, task: str, action: str, change_class: str
) -> tuple[Auto, str, str, str, str]:
    cleaned = (auto or "").strip().lower()
    if cleaned not in {"approve", "deny"}:
        raise ApprovalRuleError("auto must be approve or deny")
    resolved: Auto = "approve" if cleaned == "approve" else "deny"
    return (
        resolved,
        (project or "").strip(),
        (task or "").strip(),
        (action or "").strip(),
        normalize_change_class(change_class, persist=True),
    )


def replace_rules(payloads: list[dict[str, str]]) -> list[ApprovalRule]:
    _ensure_table()
    stored: list[ApprovalRule] = []
    with db.connect() as conn:
        conn.execute("DELETE FROM approval_action_rules")
        for item in payloads:
            auto, project, task, action, change_class = _normalize(
                str(item.get("auto") or ""),
                str(item.get("project") or ""),
                str(item.get("task") or ""),
                str(item.get("action") or ""),
                str(item.get("change_class") or ""),
            )
            rule_id = (item.get("id") or "").strip() or uuid.uuid4().hex
            conn.execute(
                db.ph(
                    "INSERT INTO approval_action_rules "
                    "(id, project, task, action, auto, change_class) "
                    "VALUES (?, ?, ?, ?, ?, ?)"
                ),
                (rule_id, project, task, action, auto, change_class),
            )
            stored.append(
                ApprovalRule(
                    id=rule_id,
                    project=project,
                    task=task,
                    action=action,
                    auto=auto,
                    change_class=change_class,
                )
            )
    return stored


def _action_token(metadata: dict[str, Any] | None) -> str:
    if not metadata:
        return ""
    for key in ("kind", "action", "step_name"):
        value = metadata.get(key)
        if value:
            return str(value)
    return ""


def _is_truthy(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    return str(value).strip().lower() in _TRUTHY


def claimed_change_class(metadata: dict[str, Any] | None) -> str:
    """Class claimed by this change.

    Only ``change_class``, ``class``, and ``contested`` count. Watcher / wake
    / bus keys are ignored so a wake cannot invent ``mechanical``.
    """
    if not metadata:
        return ""
    if _is_truthy(metadata.get("contested")):
        return UNKNOWN
    for key in _CLAIM_KEYS:
        if key in _NOT_A_CLASS_KEYS:
            continue
        if key not in metadata:
            continue
        value = metadata[key]
        if value in (None, ""):
            continue
        return normalize_change_class(str(value), persist=False)
    return ""


def rule_change_class(rule: ApprovalRule) -> ChangeClass:
    resolved = normalize_change_class(rule.change_class, persist=False)
    return resolved if resolved in CHANGE_CLASSES else UNKNOWN


def _allows_auto_approve(rule: ApprovalRule, metadata: dict[str, Any] | None) -> bool:
    if rule_change_class(rule) != MECHANICAL:
        return False
    claimed = claimed_change_class(metadata)
    if claimed and claimed != MECHANICAL:
        return False
    return True


def _matches(rule: ApprovalRule, *, project: str, task: str, action: str) -> bool:
    if rule.project and rule.project != project:
        return False
    if rule.task and rule.task != task:
        return False
    if rule.action and rule.action != action:
        return False
    return True


def _score(rule: ApprovalRule) -> int:
    return bool(rule.project) + bool(rule.task) + bool(rule.action)


def match_auto(*, project: str, task: str, metadata: dict[str, Any] | None = None) -> Auto | None:
    """Return the winning rule's auto action, or None to wait for a human.

    ``auto=approve`` is gated by change class (HP-86): only a mechanical
    rule may auto-approve. A claimed non-mechanical / unknown / contested
    class in metadata vetoes approve. Watcher/wake metadata is not a class.
    ``auto=deny`` is unchanged. A class veto does not fall through to a
    less-specific rule.
    """
    action = _action_token(metadata)
    hits = [
        rule for rule in list_rules() if _matches(rule, project=project, task=task, action=action)
    ]
    if not hits:
        return None
    hits.sort(key=_score, reverse=True)
    winner = hits[0]
    if winner.auto == "deny":
        return "deny"
    if not _allows_auto_approve(winner, metadata):
        return None
    return "approve"
