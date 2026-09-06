"""HP-61: persistable per-action approval rules + policy hook.

Rules never invent a Disposition field. ``auto`` is ``approve`` or ``deny``.
No match → pending (human). Empty match fields are wildcards. More specific
rules win.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from typing import Any, Literal

from hivepilot.services import db, state_service

Auto = Literal["approve", "deny"]


class ApprovalRuleError(ValueError):
    """Invalid rule payload."""


@dataclass
class ApprovalRule:
    id: str
    project: str
    task: str
    action: str
    auto: Auto

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
                auto TEXT NOT NULL
            )
            """
        )


def _row(raw: Any) -> ApprovalRule:
    return ApprovalRule(
        id=str(raw["id"]),
        project=str(raw["project"] or ""),
        task=str(raw["task"] or ""),
        action=str(raw["action"] or ""),
        auto=raw["auto"],  # type: ignore[arg-type]
    )


def list_rules() -> list[ApprovalRule]:
    _ensure_table()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, project, task, action, auto FROM approval_action_rules ORDER BY id"
        ).fetchall()
    return [_row(row) for row in rows]


def _normalize(auto: str, project: str, task: str, action: str) -> tuple[Auto, str, str, str]:
    cleaned = (auto or "").strip().lower()
    if cleaned not in {"approve", "deny"}:
        raise ApprovalRuleError("auto must be approve or deny")
    return cleaned, (project or "").strip(), (task or "").strip(), (action or "").strip()


def replace_rules(payloads: list[dict[str, str]]) -> list[ApprovalRule]:
    _ensure_table()
    stored: list[ApprovalRule] = []
    with db.connect() as conn:
        conn.execute("DELETE FROM approval_action_rules")
        for item in payloads:
            auto, project, task, action = _normalize(
                str(item.get("auto") or ""),
                str(item.get("project") or ""),
                str(item.get("task") or ""),
                str(item.get("action") or ""),
            )
            rule_id = (item.get("id") or "").strip() or uuid.uuid4().hex
            conn.execute(
                db.ph(
                    "INSERT INTO approval_action_rules (id, project, task, action, auto) "
                    "VALUES (?, ?, ?, ?, ?)"
                ),
                (rule_id, project, task, action, auto),
            )
            stored.append(
                ApprovalRule(id=rule_id, project=project, task=task, action=action, auto=auto)
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
    """Return the winning rule's auto action, or None to wait for a human."""
    action = _action_token(metadata)
    hits = [
        rule for rule in list_rules() if _matches(rule, project=project, task=task, action=action)
    ]
    if not hits:
        return None
    hits.sort(key=_score, reverse=True)
    return hits[0].auto
