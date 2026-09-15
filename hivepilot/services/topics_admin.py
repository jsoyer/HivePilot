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

from hivepilot.services.telegram_doors import telegram_multi_token_mode

logger = structlog.get_logger(__name__)

# Historical noxysdevbot forum doors (Inbox / Approvals / Runs / Alerts).
# Documented so operators can prune them after a multi-token cutover wipe.
# Never auto-deleted on deploy or restart — the Bot API cannot list topics.
LEGACY_FORUM_DOOR_THREAD_IDS: tuple[int, ...] = (2118, 2119, 2120, 2121)

CUTOVER_REFUSED_LEGACY = (
    "legacy single-token / STREAM_TOPICS path is still active; the forum "
    "registry is still live. Set four distinct door tokens, restart "
    "api+scheduler+telegram together, then re-run."
)


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


@dataclass
class BootstrapResult:
    """What ``topics bootstrap`` did, or would do."""

    minted: dict[str, int] = field(default_factory=dict)
    existing: dict[str, int] = field(default_factory=dict)
    skipped: bool = False
    skipped_multi_token: bool = False
    dry_run: bool = False


@dataclass
class WipeSyncResult:
    """What ``topics wipe-sync`` did, or would do."""

    cleared: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False
    multi_token: bool = False


@dataclass(frozen=True)
class CutoverPlan:
    """What a multi-token cutover wipe would do. Never calls Telegram."""

    multi_token: bool
    registry: dict[str, int]
    next_steps: tuple[str, ...]
    blocked_reason: str | None = None


@dataclass
class CutoverWipeResult:
    """Local registry wipe after door-bot cutover. Never calls Telegram."""

    multi_token: bool
    wiped: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False
    refused: bool = False
    reason: str | None = None


def wipe_followup_hint(*, multi_token: bool) -> str:
    """Operator hint after a local registry wipe."""
    if multi_token:
        ids = " ".join(str(i) for i in LEGACY_FORUM_DOOR_THREAD_IDS)
        return (
            "Doors are bots, not forum topics. Do not run `topics bootstrap`. "
            "After CoS GO, delete leftover topics in the Telegram client or "
            f"`hivepilot topics prune {ids} --yes`."
        )
    return "Mint doors with `topics bootstrap --yes`."


def cutover_next_steps(*, multi_token: bool) -> tuple[str, ...]:
    """Documented operator steps. No Bot API side effects."""
    if not multi_token:
        return (
            "Do not wipe yet — leftover ids still protect live forum doors.",
            "After Jerome's four BotFather tokens + CoS GO: set them in "
            "shared.env, restart api+scheduler+telegram together, then "
            "`hivepilot topics cutover --yes`.",
        )
    ids = " ".join(str(i) for i in LEGACY_FORUM_DOOR_THREAD_IDS)
    return (
        "`hivepilot topics cutover --yes` — JSON + SQLite only; no Telegram API.",
        "Read leftover topic ids from the Telegram topic link (last URL "
        f"segment). Historical noxysdevbot doors were {ids}.",
        "Delete in the Telegram client, or "
        f"`hivepilot topics prune {ids} --yes` (uses shared "
        "HIVEPILOT_TELEGRAM_BOT_TOKEN — the bot that minted them).",
        "Do not run `topics bootstrap` — doors are bots now.",
    )


def cutover_plan() -> CutoverPlan:
    """Describe HP-130e local cutover. Does not wipe or call Telegram."""
    multi_token = telegram_multi_token_mode()
    registry = list_topics()
    if not multi_token:
        return CutoverPlan(
            multi_token=False,
            registry=registry,
            next_steps=cutover_next_steps(multi_token=False),
            blocked_reason=CUTOVER_REFUSED_LEGACY,
        )
    return CutoverPlan(
        multi_token=True,
        registry=registry,
        next_steps=cutover_next_steps(multi_token=True),
    )


def cutover_wipe(*, confirm: bool = False) -> CutoverWipeResult:
    """Forget leftover forum topic ids after multi-token door-bot cutover.

    Safety rails (HP-130e):

    * Refuses unless ``telegram_multi_token_mode()`` is true — the legacy
      ``STREAM_TOPICS`` path still owns the registry.
    * Never calls Telegram (no ``deleteForumTopic``, no ``createForumTopic``).
    * Never bootstraps / remints doors.
    * Never runs on process start — the operator must invoke the CLI.
    """
    from hivepilot.services import notification_service

    plan = cutover_plan()
    if not plan.multi_token:
        return CutoverWipeResult(
            multi_token=False,
            wiped=dict(plan.registry),
            dry_run=not confirm,
            refused=True,
            reason=plan.blocked_reason,
        )
    if not confirm:
        return CutoverWipeResult(
            multi_token=True,
            wiped=dict(plan.registry),
            dry_run=True,
        )
    previous = notification_service.wipe_topic_registry()
    return CutoverWipeResult(multi_token=True, wiped=previous, dry_run=False)


def wipe_sync(*, confirm: bool = False) -> WipeSyncResult:
    """Clear JSON registry + SQLite mirror after an operator wipe.

    Does not delete Telegram topics — the operator already did that.
    Dry-run unless *confirm*.
    """
    from hivepilot.services import notification_service

    current = list_topics()
    multi_token = telegram_multi_token_mode()
    if not confirm:
        return WipeSyncResult(cleared=current, dry_run=True, multi_token=multi_token)
    notification_service.wipe_topic_registry()
    return WipeSyncResult(cleared=current, dry_run=False, multi_token=multi_token)


def bootstrap(*, confirm: bool = False) -> BootstrapResult:
    """Mint Inbox/Approvals/Runs/Alerts only when none of them exist.

    Dry-run unless *confirm*. A partial registry is a no-op. Multi-token
    mode never mints — doors are bots, not forum topics (HP-130e).
    """
    from hivepilot.services import notification_service

    if telegram_multi_token_mode():
        return BootstrapResult(
            skipped=True,
            skipped_multi_token=True,
            dry_run=not confirm,
        )
    existing = notification_service._existing_pollen_doors()
    if existing:
        return BootstrapResult(existing=existing, skipped=True, dry_run=not confirm)
    if not confirm:
        return BootstrapResult(dry_run=True)
    minted = notification_service.bootstrap_pollen_doors(confirm=True)
    return BootstrapResult(minted=minted, dry_run=False)


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
