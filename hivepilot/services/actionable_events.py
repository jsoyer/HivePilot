"""HP-85 spike: zero-token actionable-event consumer on the HP-40 bus.

Idle sleeper that tails `events.subscribe()` / `change_log` and classifies
each row as wake or sleep with a **static allowlist**. A model is never
consulted — not to classify, not to decide whether to wake, not as a
fallback when the kind is unknown.

This is the Firstmate-shaped watcher from ADR
`docs/adr/2026-09-11-firstmate-patterns.md`, crossed with the pieces we
already own:

- HP-40 (`events.py`) — durable log is the source of truth; NOTIFY is only
  a wakeup. A missed notify or a dead consumer delays a wake; it does not
  drop the fact.
- HP-50 (`nudge_engine.py`) — a nudge must never start an LLM reply loop
  or change a gate. This consumer inherits the same fail-safe: a broken
  handler must not change a gate, start a model, or erase `change_log`.

Unknown kind = sleep (fail closed). Wake does **not** start a run, a
nudge, or a model — it only yields a classification. Wiring a downstream
(orchestrator / human page) is a later ticket.

Spike, not a production daemon: no watermark persistence, no systemd unit.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

from hivepilot.services import events
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

Action = Literal["wake", "sleep"]

#: Always-actionable kinds. `nudge.posted` is emitted by HP-50;
#: `approval.requested` is emitted by `state_service.record_approval_request`
#: (named here because the bus had no approval kind before this spike).
ALWAYS_ACTIONABLE_KINDS: frozenset[str] = frozenset(
    {
        "nudge.posted",
        "approval.requested",
    }
)

#: Existing terminal-run kind. There is no `run.failed` row — `complete_run`
#: always emits `run.completed` with `payload.status`. Wake only on the
#: failure bucket shared with `analytics_service._FAILED_STATUSES`.
RUN_COMPLETED_KIND = "run.completed"

FAILED_RUN_STATUSES: frozenset[str] = frozenset(
    {
        "failed",
        "denied",
        "rate_limit",
        "auth_expired",
        "test_failure",
        "security_blocker",
    }
)


@dataclass(frozen=True)
class Classification:
    """Pure wake/sleep decision for one `change_log` row."""

    action: Action
    kind: str
    reason: str
    change_id: int | None = None
    entity_type: str = ""
    entity_id: str = ""
    tenant: str = ""


def _payload_dict(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("payload")
    return raw if isinstance(raw, dict) else {}


def _change_id(row: dict[str, Any]) -> int | None:
    raw = row.get("id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _base(row: dict[str, Any], action: Action, reason: str) -> Classification:
    return Classification(
        action=action,
        kind=str(row.get("kind") or ""),
        reason=reason,
        change_id=_change_id(row),
        entity_type=str(row.get("entity_type") or ""),
        entity_id=str(row.get("entity_id") or ""),
        tenant=str(row.get("tenant") or ""),
    )


def classify(row: dict[str, Any] | None) -> Classification:
    """Return wake or sleep for one change row. No I/O, no model.

    Payload text is never interpreted. An unknown kind whose payload screams
    "WAKE NOW" still sleeps. Only the kind (and, for `run.completed`, the
    structured `payload.status`) may wake the sleeper.
    """
    if not isinstance(row, dict):
        return Classification(action="sleep", kind="", reason="invalid_row")
    kind = str(row.get("kind") or "")
    if not kind:
        return Classification(
            action="sleep",
            kind="",
            reason="missing_kind",
            change_id=_change_id(row),
        )
    if kind in ALWAYS_ACTIONABLE_KINDS:
        return _base(row, "wake", "allowlisted")
    if kind == RUN_COMPLETED_KIND:
        status = str(_payload_dict(row).get("status") or "").strip().lower()
        if status in FAILED_RUN_STATUSES:
            return _base(row, "wake", "run_failed")
        return _base(row, "sleep", "run_not_failed")
    return _base(row, "sleep", "not_allowlisted")


def render_classification(decision: Classification) -> str:
    """One-line, pipeable rendering for the CLI smoke check."""
    cid = "" if decision.change_id is None else str(decision.change_id)
    return (
        f"{decision.action}\t{decision.kind}\t{decision.reason}"
        f"\tid={cid}\t{decision.entity_type}:{decision.entity_id}"
    )


def consume(
    after_id: int | None = None,
    *,
    channel: str = events.CHANNEL,
    poll_interval: float = 0.5,
    stop: Any | None = None,
    idle_timeout: float | None = None,
    on_wake: Callable[[Classification], None] | None = None,
) -> Iterator[Classification]:
    """Tail the HP-40 bus and yield wake classifications only.

    Sleeps are discarded — the consumer stays idle. `on_wake`, if provided,
    is a side-effect hook for a later ticket; it must not start a model
    from this module. A broken classify, hook, or subscribe is logged and
    swallowed so a dead consumer cannot change a gate or drop durable facts
    (the row stays in `change_log` for the next catch-up).
    """
    try:
        stream = events.subscribe(
            after_id,
            channel=channel,
            poll_interval=poll_interval,
            stop=stop,
            idle_timeout=idle_timeout,
        )
        for row in stream:
            try:
                decision = classify(row)
            except Exception as exc:  # noqa: BLE001 — fail-safe: never break the bus
                logger.warning(
                    "actionable_events.classify_failed",
                    error=str(exc),
                    change_id=row.get("id") if isinstance(row, dict) else None,
                )
                continue
            if decision.action != "wake":
                continue
            if on_wake is not None:
                try:
                    on_wake(decision)
                except Exception as exc:  # noqa: BLE001 — hook must not kill the sleeper
                    logger.warning(
                        "actionable_events.on_wake_failed",
                        error=str(exc),
                        change_id=decision.change_id,
                        kind=decision.kind,
                    )
            yield decision
    except Exception as exc:  # noqa: BLE001 — broken subscribe must not propagate
        logger.warning("actionable_events.consume_failed", error=str(exc))
        return
