"""Espaces async reply loop — the dépose/relève transport (HP-46, P2).

When a human deposits a message in a space that has role participants, the POST
returns immediately and a background worker "relève" it: for each role it emits
a `space.typing` battement, asks the pluggable reply GENERATOR for that role's
response, posts it as a `space.message`, then emits `space.typing_stop`. So the
UI shows "<role> is working…" and then the reply appears live over SSE (HP-41).

The generator (a role's actual voice) is injected by the orchestrator (HP-49)
via `register_reply_generator`; with none registered this is a graceful no-op —
the transport lives here, the intelligence plugs into it.

Loop-safe: only a HUMAN message triggers a reply (the endpoint gates on
`sender_type`), so a role's own reply never triggers another.
"""

from __future__ import annotations

from typing import Callable

from hivepilot.config import settings
from hivepilot.services import events, state_service
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

#: (space, role_id, thread_messages) -> the role's reply, or None to stay
#: silent. The reply is either plain text, or a dict `{"body": str, "actions":
#: [...]}` carrying a collapsible tool-action trace (HP-47).
ReplyGenerator = Callable[[dict, str, list[dict]], "str | dict | None"]


def _split_reply(result: "str | dict | None") -> "tuple[str | None, list[dict] | None]":
    if isinstance(result, dict):
        body = result.get("body")
        actions = result.get("actions")
        return (
            body if isinstance(body, str) else None,
            actions if isinstance(actions, list) else None,
        )
    if isinstance(result, str):
        return result, None
    return None, None


_generator: ReplyGenerator | None = None


def register_reply_generator(fn: ReplyGenerator | None) -> None:
    """Install (or clear, with `None`) the generator that voices a role's reply.
    The orchestrator (HP-49) registers the real one; tests register a stub."""
    global _generator
    _generator = fn


def _role_participants(space: dict) -> list[str]:
    out: list[str] = []
    for participant in space.get("participants") or []:
        if (
            isinstance(participant, dict)
            and participant.get("type") == "role"
            and participant.get("id")
        ):
            out.append(str(participant["id"]))
    return out


def respond_in_space(
    space_id: int, tenant: str = "default", *, only_role: str | None = None
) -> None:
    """Background worker: for each role in the space, emit a typing battement,
    generate a reply, post it, and stop typing. Fail-safe per role — one role's
    failure never blocks the others nor crashes the worker. `only_role` (HP-48
    handoff) restricts the reply to a single role."""
    space = state_service.get_space(space_id, tenant=tenant)
    if space is None:
        return
    roles = _role_participants(space)
    if only_role is not None:
        roles = [r for r in roles if r == only_role]
    if not roles:
        return
    generator = _generator
    thread = state_service.list_space_messages(space_id, tenant=tenant)
    for role in roles:
        try:
            events.emit(
                "space.typing",
                "space",
                space_id,
                tenant=tenant,
                payload={"space_id": space_id, "role": role},
            )
            body, actions = _split_reply(generator(space, role, thread) if generator else None)
            if body and body.strip():
                msg_id = state_service.add_space_message(
                    space_id, "role", body, sender_id=role, tenant=tenant, actions=actions
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
                        "sender_id": role,
                    },
                )
        except Exception as exc:  # noqa: BLE001 — one role must not break the rest
            logger.warning("spaces.reply_failed", space_id=space_id, role=role, error=str(exc))
        finally:
            events.emit(
                "space.typing_stop",
                "space",
                space_id,
                tenant=tenant,
                payload={"space_id": space_id, "role": role},
            )


def dispatch_reply(space_id: int, tenant: str = "default", *, only_role: str | None = None) -> None:
    """Deposit side: fire the background reply loop when auto-reply is on and the
    space has role participants. `only_role` restricts it to a single role
    (HP-48 handoff). Returns immediately — the POST never blocks on the agent's
    work (that is the whole point of dépose/relève)."""
    if not settings.spaces_auto_reply:
        return
    space = state_service.get_space(space_id, tenant=tenant)
    if space is None or not _role_participants(space):
        return
    from hivepilot.services import async_run_service

    async_run_service.submit_background(
        lambda: respond_in_space(space_id, tenant, only_role=only_role)
    )
