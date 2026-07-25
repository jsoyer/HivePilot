"""Swarm Phase 1 -- the engine service tying `hivepilot.swarm` together:
`publish_event` (sign + dedupe), `claim_next` (verify + tenant-scope +
exactly-once claim), `process_claimed_event`/`dispatch_claimed_event`
(handler idempotency-by-`event.id`), and the ONE end-to-end Phase 1 event
type, `pr_ready` (`publish_pr_ready` + `handle_pr_ready`).

SECURITY (fail-closed):
  * Every published event is HMAC-signed (`get_signing_key` -- resolved via
    the existing `${secret:NAME}` mechanism, masked, never logged).
    Unconfigured/unresolvable key -> `publish_event` degrades to a
    best-effort no-op (never breaks the caller's run) and `claim_next`
    refuses to claim anything.
  * `claim_next` verifies EVERY candidate event's signature
    (`hivepilot.swarm.models.verify_event`) BEFORE it is ever considered for
    claiming -- a bad/missing signature is marked `skipped` (permanently
    rejected, see `state_service.mark_swarm_event_skipped`) and NEVER
    reaches a handler.
  * Tenant isolation: a candidate whose `tenant` isn't in this instance's
    `swarm_served_tenants` is left untouched (still `pending`) for another,
    correctly-scoped instance -- never claimed, never executed.
  * Handler idempotency: `process_claimed_event` only invokes the handler
    (and marks the row `done`) the FIRST time it's called for an event still
    in `claimed` status -- a duplicate/redelivered call for an already-`done`
    event is a safe no-op.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from hivepilot.config import Settings
from hivepilot.config import settings as _default_settings
from hivepilot.services import state_service
from hivepilot.services.config_provenance import register_secret_value
from hivepilot.services.secret_refs import SecretReferenceError, has_secret_ref, resolve_secret_refs
from hivepilot.swarm.models import Event, compute_event_id, sign_event, verify_event
from hivepilot.swarm.transport import resolve_transport
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


class PublishStatus(str, Enum):
    PUBLISHED = "published"
    DEDUPED = "deduped"
    # No signing key configured/resolvable, or an unexpected internal error
    # (state DB, transport construction, ...) -- best-effort: the CALLER's
    # run/PR/step must never fail because the swarm bus is unavailable.
    SKIPPED = "skipped"


class ClaimStatus(str, Enum):
    CLAIMED = "claimed"
    # This instance's atomic claim lost the race -- another instance already
    # owns this event.
    DEDUPED = "deduped"
    # At least one candidate was inspected and rejected (wrong tenant / bad
    # signature) but nothing was ultimately claimable.
    SKIPPED = "skipped"
    # No candidates at all (or no signing key / no transport configured).
    NONE = "none"


@dataclass(frozen=True)
class PublishResult:
    status: PublishStatus
    event_id: str


@dataclass(frozen=True)
class ClaimResult:
    status: ClaimStatus
    event: Event | None = None


def get_instance_id(settings: Settings | None = None) -> str:
    """This deployment's stable swarm instance id (see
    `Settings.swarm_instance_id`)."""
    s = settings or _default_settings
    return s.swarm_instance_id


def get_signing_key(settings: Settings | None = None) -> str | None:
    """Resolve the fleet-wide HMAC signing key, or `None` when unconfigured
    or unresolvable.

    Never raises: a `${secret:NAME}` reference that fails to resolve (a
    misconfigured catalog, a backend error, ...) degrades to `None` rather
    than propagating -- callers (`publish_event`/`claim_next`) already treat
    `None` as "swarm signing is unavailable right now", which is the correct,
    fail-closed response to a bad reference too. A resolved value is always
    registered for masking (`register_secret_value`) before it's returned,
    whether it came from a literal or a `${secret:NAME}` reference.
    """
    s = settings or _default_settings
    raw = s.swarm_key
    if not raw:
        return None
    if has_secret_ref(raw):
        try:
            resolved = resolve_secret_refs({"swarm_key": raw}, catalog=s.swarm_secrets)
        except SecretReferenceError:
            logger.warning("swarm.signing_key_resolution_failed")
            return None
        return resolved.get("swarm_key")
    register_secret_value(raw)
    return raw


def publish_event(
    event_type: str,
    payload: dict[str, Any],
    tenant: str = "default",
    *,
    dedupe_key: str,
    settings: Settings | None = None,
) -> PublishResult:
    """Publish *event_type* with *payload* for *tenant*, deduped by
    `compute_event_id(event_type, dedupe_key)`.

    Returns `PUBLISHED` for a genuinely new event, `DEDUPED` when an event
    with this id already exists (a re-publish of identical content is never
    a second unit of work), or `SKIPPED` when signing isn't currently
    possible or an unexpected internal error occurs -- NEVER raises, so a
    caller wiring this into a git/forge action (see `publish_pr_ready`) can
    treat the swarm bus as strictly best-effort.
    """
    s = settings or _default_settings
    event_id = compute_event_id(event_type, dedupe_key)
    try:
        key = get_signing_key(s)
        if key is None:
            logger.warning(
                "swarm.publish_skipped_no_signing_key", event_type=event_type, event_id=event_id
            )
            return PublishResult(PublishStatus.SKIPPED, event_id)

        instance_id = get_instance_id(s)
        event = Event(
            id=event_id,
            type=event_type,
            payload=payload,
            tenant=tenant,
            origin_instance=instance_id,
            ts=time.time(),
        )
        event = event.model_copy(update={"sig": sign_event(event, key)})

        inserted = state_service.insert_swarm_event(event)
        if not inserted:
            logger.info("swarm.publish_deduped", event_id=event_id, type=event_type)
            return PublishResult(PublishStatus.DEDUPED, event_id)

        try:
            transport = resolve_transport(s.swarm_transport, instance_id=instance_id)
            transport.publish(event)
        except Exception:  # noqa: BLE001 — broker hand-off failure must never break the caller
            logger.warning(
                "swarm.publish_transport_error", event_id=event_id, type=event_type, exc_info=True
            )
        return PublishResult(PublishStatus.PUBLISHED, event_id)
    except Exception:  # noqa: BLE001 — belt-and-suspenders: publish is ALWAYS best-effort
        logger.warning("swarm.publish_unexpected_error", event_id=event_id, exc_info=True)
        return PublishResult(PublishStatus.SKIPPED, event_id)


def claim_next(
    types: list[str],
    *,
    settings: Settings | None = None,
    instance_id: str | None = None,
) -> ClaimResult:
    """Attempt to claim the next eligible event of one of *types* for this
    instance.

    Walks candidates from `transport.subscribe(types)` in order:
      * a candidate for a tenant this instance doesn't serve is left
        untouched (still `pending`) and skipped;
      * a candidate that fails signature verification is marked `skipped`
        (permanently, fleet-wide) and NEVER reaches a handler;
      * the first candidate that passes both checks is claimed atomically
        (`transport.claim`) -- `CLAIMED` on success, `DEDUPED` if another
        instance won the race first (this call returns immediately rather
        than trying further candidates).

    `NONE` when there was nothing to inspect at all (or no signing key/
    transport is configured); `SKIPPED` when at least one candidate was
    inspected and rejected but none was ultimately claimable.
    """
    s = settings or _default_settings
    inst = instance_id or get_instance_id(s)

    key = get_signing_key(s)
    if key is None:
        logger.warning("swarm.claim_next_no_signing_key")
        return ClaimResult(ClaimStatus.NONE, None)

    try:
        transport = resolve_transport(s.swarm_transport, instance_id=inst)
    except Exception:  # noqa: BLE001 — an unavailable transport degrades to "nothing to claim"
        logger.warning("swarm.claim_next_transport_unavailable", exc_info=True)
        return ClaimResult(ClaimStatus.NONE, None)

    served_tenants = set(s.swarm_served_tenants)
    skipped_any = False
    for event in transport.subscribe(types):
        if event.tenant not in served_tenants:
            skipped_any = True
            continue
        if not verify_event(event, key):
            logger.warning("swarm.event_rejected_bad_signature", event_id=event.id, type=event.type)
            state_service.mark_swarm_event_skipped(event.id)
            skipped_any = True
            continue
        if transport.claim(event.id):
            return ClaimResult(ClaimStatus.CLAIMED, event)
        return ClaimResult(ClaimStatus.DEDUPED, event)

    return ClaimResult(ClaimStatus.SKIPPED if skipped_any else ClaimStatus.NONE, None)


def process_claimed_event(event: Event, handler: Callable[[Event], None]) -> bool:
    """Idempotent-by-`event.id` handler invocation.

    Only invokes *handler* (and transitions the row `claimed` -> `done`) the
    FIRST time this is called for an event whose persisted row is still
    `claimed`. A duplicate/redelivered call for an event that's already
    `done` -- or one that was never actually claimed (`pending`, or unknown)
    -- is a safe no-op that returns `False` WITHOUT invoking *handler*.
    """
    row = state_service.get_swarm_event(event.id)
    if row is None or row["status"] != "claimed":
        return False
    handler(event)
    state_service.mark_swarm_event_done(event.id)
    return True


def handle_pr_ready(event: Event, orchestrator: Any) -> None:
    """Phase 1's ONE end-to-end handler: given a claimed `pr_ready` event,
    trigger the review pipeline for that PR's project via the SAME
    `orchestrator.run_pipeline(...)` seam `hivepilot.services.
    autopilot_queue.drain_one` already uses to dispatch an externally-
    triggered pipeline run -- this is the "clean seam" the PRD asks for, NOT
    a rewire of the review pipeline itself.
    """
    payload = event.payload
    project_name = payload.get("project")
    if not project_name:
        raise ValueError(f"pr_ready event {event.id!r} payload is missing 'project'")
    if orchestrator is None:
        raise RuntimeError(
            f"handle_pr_ready({event.id!r}) requires an orchestrator instance "
            "to trigger the review pipeline"
        )
    logger.info(
        "swarm.pr_ready_handled",
        event_id=event.id,
        project=project_name,
        branch=payload.get("branch"),
    )
    orchestrator.run_pipeline(
        project_names=[project_name],
        pipeline_name=payload.get("pipeline") or "review",
        extra_prompt=None,
        auto_git=False,
    )


_EVENT_HANDLERS: dict[str, Callable[[Event, Any], None]] = {
    "pr_ready": handle_pr_ready,
}


def dispatch_claimed_event(event: Event, orchestrator: Any = None) -> bool:
    """Route a CLAIMED *event* to its registered handler by `event.type`,
    through the idempotent `process_claimed_event` wrapper.

    An `event.type` with no registered handler is a safe no-op (`False`,
    logged) rather than a crash -- forward-compatible with future event
    types a later phase can add without touching this function.
    """
    handler = _EVENT_HANDLERS.get(event.type)
    if handler is None:
        logger.info("swarm.no_handler_for_event_type", event_id=event.id, type=event.type)
        return False
    return process_claimed_event(event, lambda e: handler(e, orchestrator))


def publish_pr_ready(
    *,
    project_name: str,
    owner_repo: str | None,
    branch: str,
    sha: str,
    tenant: str = "default",
    kind: str = "opened",
    pipeline: str | None = None,
    settings: Settings | None = None,
) -> PublishResult:
    """Publish a `pr_ready` event for a PR just opened/promoted.

    Deduped by `f"{owner_repo or project_name}:{branch}:{sha}"` -- a retried
    step that re-opens/re-promotes the SAME branch at the SAME commit
    collides on the same event id (a DEDUPE, never a second review trigger).
    `branch` stands in for a PR number here: `hivepilot.forges.provider.
    ForgeProvider.open_pr`/`promote_pr` don't return the forge's PR number
    without an extra API round-trip, and `branch` is already this
    project/task's own stable, unique PR identifier (see
    `hivepilot.services.git_service.perform_git_actions`'s
    `branch_prefix/project_name` construction) -- see this sprint's Agent
    Notes for the full rationale.

    ALWAYS best-effort: wraps the whole call in a broad except so a bus
    failure of ANY kind can never break the git/forge action that triggered
    it (`hivepilot.services.git_service.create_pr`/`promote_pr`).
    """
    dedupe_key = f"{owner_repo or project_name}:{branch}:{sha}"
    try:
        return publish_event(
            "pr_ready",
            {
                "project": project_name,
                "repo": owner_repo,
                "branch": branch,
                "sha": sha,
                "kind": kind,
                "pipeline": pipeline,
            },
            tenant,
            dedupe_key=dedupe_key,
            settings=settings,
        )
    except Exception:  # noqa: BLE001 — best-effort: never break the caller's PR/run
        logger.warning(
            "swarm.publish_pr_ready_failed", project=project_name, branch=branch, exc_info=True
        )
        return PublishResult(PublishStatus.SKIPPED, compute_event_id("pr_ready", dedupe_key))
