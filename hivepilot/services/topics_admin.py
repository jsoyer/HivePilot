"""Inspect and prune Telegram forum topics HivePilot may have created.

Why this takes explicit ids
---------------------------
The Telegram Bot API has **no endpoint that lists a forum's topics**, and no
safe existence probe either: ``deleteForumTopic`` destroys,
``editForumTopic``/``closeForumTopic`` mutate, and
``unpinAllForumTopicMessages`` would drop the operator's pins. A tool that
guessed an id and guessed *right* would destroy a live topic.

So discovery is the operator's half -- Telegram shows the topics, and a
topic's id is the last segment of its link -- and this tool's half is to
refuse the dangerous ones.

The one invariant
-----------------
An id the registry still points at is a LIVE topic and is never deleted, not
even with ``--yes``. Everything else is dry-run until someone says otherwise
in as many words.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PrunePlan:
    """What a prune would do, before it does anything."""

    deletable: list[int]
    protected: list[int]


@dataclass
class PruneResult:
    would_delete: list[int] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    protected: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)


def list_topics() -> dict[str, int]:
    """The registry: role key -> message_thread_id.

    The operator compares this against what Telegram shows. Anything in the
    group and not in this list is a candidate for pruning.
    """
    from hivepilot.services import notification_service

    return dict(notification_service._load_topics())


def plan_prune(thread_ids: list[int]) -> PrunePlan:
    """Split requested ids into deletable and protected, order preserved."""
    live = set(list_topics().values())
    deletable: list[int] = []
    protected: list[int] = []
    for thread_id in dict.fromkeys(thread_ids):  # collapse duplicates, keep order
        (protected if thread_id in live else deletable).append(thread_id)
    return PrunePlan(deletable=deletable, protected=protected)


def _delete_via_telegram(thread_id: int) -> None:
    """Delete one forum topic. Raises on any non-ok response."""
    import requests

    from hivepilot.config import settings

    token = settings.telegram_bot_token
    chat_id = settings.telegram_stream_chat_id
    if not token or not chat_id:
        raise RuntimeError("telegram_bot_token / telegram_stream_chat_id are not configured")

    response = requests.post(
        f"https://api.telegram.org/bot{token}/deleteForumTopic",
        json={"chat_id": chat_id, "message_thread_id": thread_id},
        timeout=10,
    )
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("description") or payload))


def prune(
    thread_ids: list[int],
    *,
    confirm: bool = False,
    delete: Callable[[int], None] | None = None,
) -> PruneResult:
    """Delete the requested topics, minus anything the registry protects.

    Dry-run unless *confirm*. A delete that fails is REPORTED, never
    swallowed: a topic that could not be removed must not read as removed.
    """
    plan = plan_prune(thread_ids)
    result = PruneResult(protected=plan.protected)

    if not confirm:
        result.would_delete = plan.deletable
        return result

    sink = delete or _delete_via_telegram
    for thread_id in plan.deletable:
        try:
            sink(thread_id)
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
            logger.warning("topics.prune_failed", message_thread_id=thread_id, error=str(exc))
            result.failed.append((thread_id, str(exc)))
            continue
        logger.info("topics.pruned", message_thread_id=thread_id)
        result.deleted.append(thread_id)

    return result
