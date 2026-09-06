"""Delegation primitives (HP-48, Cycle 1 · P2) — the vocabulary the orchestrator
(HP-49) uses to make agents collaborate. Four primitives, one clear contract
each:

- `run_subagent(role, prompt)` — a turn-scoped ephemeral helper: run a role
  ONCE against a prompt and return its text; no persistent thread/space.
- `spawn_peer(project, task, role)` — start a real BACKGROUND run for a peer
  role (a durable unit of work), returning its run id.
- `message_role(space_id, text)` — deposit a message in a space and trigger the
  agents' async replies (reuses the HP-46 dépose/relève loop).
- `handoff(space_id, from_role, to_role, text, hops)` — hand the conversation to
  another role, BOUNDED by a hop limit so a handoff chain can never loop
  forever.

Execution intelligence is INJECTED (HP-49 registers the executors); with none
registered the primitives degrade gracefully. The collaboration vocabulary and
the hop-limit enforcement live here and are fully testable now.
"""

from __future__ import annotations

from typing import Callable

from hivepilot.config import settings
from hivepilot.services import events, state_service
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

#: (role, prompt) -> the subagent's text (or None to stay silent).
SubagentExecutor = Callable[[str, str], "str | None"]
#: (run_id, role) -> None — runs a spawned peer run to completion.
PeerExecutor = Callable[[int, str], None]

_subagent_executor: SubagentExecutor | None = None
_peer_executor: PeerExecutor | None = None


def register_subagent_executor(fn: SubagentExecutor | None) -> None:
    global _subagent_executor
    _subagent_executor = fn


def register_peer_executor(fn: PeerExecutor | None) -> None:
    global _peer_executor
    _peer_executor = fn


def run_subagent(role: str, prompt: str) -> str | None:
    """Turn-scoped ephemeral helper: run `role` once against `prompt` and return
    its text. No persistent thread/space (that's `message_role`/`spawn_peer`).
    Execution via the registered subagent executor (HP-49); `None` when
    unregistered. Never raises — a helper failure must not break the caller."""
    executor = _subagent_executor
    if executor is None:
        return None
    try:
        return executor(role, prompt)
    except Exception as exc:  # noqa: BLE001 — a helper failure is not the caller's failure
        logger.warning("delegation.subagent_failed", role=role, error=str(exc))
        return None


def message_role(
    space_id: int,
    text: str,
    *,
    sender_type: str = "human",
    sender_id: str | None = None,
    tenant: str = "default",
) -> int:
    """Async message to a space's agents: append the message and fire the
    dépose/relève reply loop (HP-46). Returns the message id."""
    msg_id = state_service.add_space_message(
        space_id, sender_type, text, sender_id=sender_id, tenant=tenant
    )
    events.emit(
        "space.message",
        "space",
        space_id,
        tenant=tenant,
        payload={"space_id": space_id, "message_id": msg_id, "sender_type": sender_type},
    )
    from hivepilot.services import spaces_responder

    spaces_responder.dispatch_reply(space_id, tenant=tenant)
    return msg_id


def spawn_peer(project: str, task: str, role: str, *, tenant: str = "default") -> int:
    """Spawn a real BACKGROUND run for a peer role. Returns the run id. Execution
    via the registered peer executor (HP-49); with none registered the run row
    is created and left for a worker to pick up (never blocks)."""
    run_id = state_service.record_run_start(project, task, tenant=tenant)
    executor = _peer_executor
    if executor is not None:
        from hivepilot.services import async_run_service

        async_run_service.submit_background(lambda: executor(run_id, role))
    return run_id


def handoff(
    space_id: int,
    from_role: str,
    to_role: str,
    text: str,
    *,
    hops: int = 0,
    tenant: str = "default",
) -> dict:
    """Hand the conversation to `to_role`, BOUNDED by `settings.delegation_max_
    hops`. At/past the limit it posts a 'limit reached' system note and does NOT
    dispatch further (prevents a runaway A→B→A… loop). Otherwise it ensures
    `to_role` is a participant, posts the handoff message (from `from_role`),
    and dispatches ONLY that role's reply with `hops` incremented. Returns a
    status dict (`dispatched` | `limit_reached`)."""
    max_hops = settings.delegation_max_hops
    if hops >= max_hops:
        state_service.add_space_message(
            space_id,
            "system",
            f"Handoff limit reached ({max_hops} hops); stopping the chain.",
            tenant=tenant,
        )
        events.emit(
            "space.message",
            "space",
            space_id,
            tenant=tenant,
            payload={"space_id": space_id, "sender_type": "system"},
        )
        logger.warning("delegation.handoff_limit", space_id=space_id, hops=hops, to=to_role)
        return {"status": "limit_reached", "hops": hops}

    state_service.add_space_participant(space_id, {"type": "role", "id": to_role}, tenant=tenant)
    msg_id = state_service.add_space_message(
        space_id,
        "role",
        text,
        sender_id=from_role,
        tenant=tenant,
        actions=[{"label": f"handoff → {to_role}"}],
    )
    events.emit(
        "space.message",
        "space",
        space_id,
        tenant=tenant,
        payload={
            "space_id": space_id,
            "message_id": msg_id,
            "sender_type": "role",
            "sender_id": from_role,
        },
    )
    from hivepilot.services import spaces_responder

    spaces_responder.dispatch_reply(space_id, tenant=tenant, only_role=to_role)
    return {"status": "dispatched", "to": to_role, "hops": hops + 1}
