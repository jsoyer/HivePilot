from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import requests

from hivepilot.config import settings
from hivepilot.services.pending_confirmation import PendingConfirmationStore
from hivepilot.utils.logging import get_logger

if TYPE_CHECKING:
    from hivepilot.services.concierge_service import ConciergeDecision

logger = get_logger(__name__)

_DISCORD_API = "https://discord.com/api/v10"

# Natural-language concierge (opt-in, settings.chatops_concierge_enabled,
# gateway-mode only — HTTP-interactions mode can't receive plain messages):
# pending destructive route/action decisions awaiting a text "yes <token>" /
# "no" confirmation reply, keyed by channel_id. Value: (confirmation_token,
# decision) — same shape as chatops_service._pending_concierge_text /
# slack_bot._pending_concierge / telegram_bot._pending_concierge.
#
# Security fix (same bug class as `slack_bot._PendingChallenge` / F3, and as
# `concierge_service._PendingOffer`): this used to be a plain
# `dict[int, tuple[str, ConciergeDecision]]` with NO owner binding and NO
# TTL — ANY channel member typing "yes <token>" (or "no") resolved the
# pending decision regardless of who triggered it, and it never expired.
# Now backed by `PendingConfirmationStore`, which binds each entry to the
# requesting Discord user id and expires it after
# `_CONCIERGE_CONFIRM_TTL_SECONDS` -- see that module's docstring for the
# full fail-closed contract.
_CONCIERGE_CONFIRM_TTL_SECONDS = 5 * 60
"""TTL for a pending Yes/No concierge confirmation reply -- identical
constant/rationale to `slack_bot.py` / `telegram_bot.py`: shorter than both
`concierge_service._OFFER_TTL_SECONDS` (10 min, a one-word typed reply) and
Slack's `_CHALLENGE_TTL_SECONDS` (15 min, a composed follow-up question),
because resolving this prompt costs the operator no more than a short
"yes <token>"/"no" reply to a message they just received, and it gates a
destructive action directly."""

_pending_concierge: PendingConfirmationStore[tuple[str, "ConciergeDecision"]] = (
    PendingConfirmationStore(_CONCIERGE_CONFIRM_TTL_SECONDS)
)

# Discord's hard per-message character cap -- mirrors
# hivepilot.streaming.discord_channel._DISCORD_MAX_LEN. Used to split a long
# Challenge/Ask CoS response into multiple ordered followup messages instead
# of letting it silently get rejected/truncated.
_MAX_MSG_LEN = 2000

# F9 fix: the modal's `max_length: 4000` (see `_challenge_modal_response`)
# is enforced CLIENT-SIDE only by Discord's own UI -- a submission crafted
# outside that UI (or a future client bug) isn't bounded by it at all. Cap
# the operator-typed challenge text server-side before it's dispatched to
# the CoS / persisted into planning_context.
_CHALLENGE_TEXT_MAX_LEN = 4000

# Challenge/Ask (parity with Telegram's Challenge / Ask button) modal
# constants. Discord's HTTP-interactions mode never receives plain messages
# (the gateway-only, privileged-intent concierge text flow above doesn't
# apply here), so the follow-up text is captured via a MODAL opened
# synchronously from the button press instead. The run_id is encoded
# directly in the modal's custom_id -- no server-side pending-state dict is
# needed (and none could safely span the stateless HTTP webhook process).
_CHALLENGE_MODAL_PREFIX = "challenge_modal:"
_CHALLENGE_TEXT_CUSTOM_ID = "challenge_text"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _token() -> str:
    token = settings.discord_bot_token
    if not token:
        raise RuntimeError("Discord bot token not configured. Set HIVEPILOT_DISCORD_BOT_TOKEN.")
    return token


def _is_allowed(guild_id: int | None, channel_id: int | None) -> bool:
    """Return True when the request originates from an allowed guild/channel.

    Guild whitelist: open to all when discord_allowed_guild_ids is empty.
    Channel whitelist: open to all when discord_allowed_channel_ids is empty.
    """
    allowed_guilds = settings.discord_allowed_guild_ids
    if allowed_guilds and guild_id not in allowed_guilds:
        return False
    allowed_channels = settings.discord_allowed_channel_ids
    if allowed_channels and channel_id not in allowed_channels:
        return False
    return True


def _get_orch():
    from hivepilot.services.chatops_service import _get_orchestrator

    return _get_orchestrator()


def _format_results(results) -> str:
    lines = [
        ("+ " if r.success else "- ")
        + f"{r.project} -> {r.target}"
        + (f"\n  {r.detail}" if r.detail else "")
        for r in results
    ]
    return "\n".join(lines) or "Done."


# ---------------------------------------------------------------------------
# Discord REST helpers
# ---------------------------------------------------------------------------


def _bot_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bot {_token()}",
        "Content-Type": "application/json",
    }


def _post_message(channel_id: int, payload: dict[str, Any]) -> None:
    url = f"{_DISCORD_API}/channels/{channel_id}/messages"
    resp = requests.post(url, headers=_bot_headers(), json=payload, timeout=10)
    resp.raise_for_status()


def _followup_message(application_id: str, interaction_token: str, payload: dict[str, Any]) -> None:
    url = f"{_DISCORD_API}/webhooks/{application_id}/{interaction_token}"
    resp = requests.post(
        url, headers={"Content-Type": "application/json"}, json=payload, timeout=10
    )
    resp.raise_for_status()


def _interaction_callback_url(interaction_id: str, interaction_token: str) -> str:
    return f"{_DISCORD_API}/interactions/{interaction_id}/{interaction_token}/callback"


def _send_interaction_response(
    interaction_id: str, interaction_token: str, payload: dict[str, Any]
) -> None:
    """POST an interaction-response payload to Discord's interaction-callback
    endpoint. In HTTP-interactions mode, `_dispatch_interaction`'s returned
    dict is returned directly as the webhook's HTTP response body instead --
    gateway mode (`run_gateway`'s `on_interaction`) has no such return
    channel, so it must deliver that SAME dict explicitly via this call. No
    `Authorization: Bot ...` header is sent: the interaction token embedded
    in the URL is itself the one-time credential for this specific
    interaction (this is Discord's documented interaction-callback auth
    model, distinct from the bot-token-authenticated REST calls above)."""
    url = _interaction_callback_url(interaction_id, interaction_token)
    resp = requests.post(
        url, headers={"Content-Type": "application/json"}, json=payload, timeout=10
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Signature verification (Ed25519 via PyNaCl)
# ---------------------------------------------------------------------------


def verify_signature(body: bytes, signature: str, timestamp: str) -> bool:
    """Verify the Ed25519 signature sent by Discord on every interaction."""
    try:
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey
    except ImportError:
        raise RuntimeError("PyNaCl required: pip install hivepilot[discord]")
    if not settings.discord_public_key:
        raise RuntimeError("Discord public key not configured. Set HIVEPILOT_DISCORD_PUBLIC_KEY.")
    key = VerifyKey(bytes.fromhex(settings.discord_public_key))
    try:
        key.verify((timestamp + body.decode()).encode(), bytes.fromhex(signature))
        return True
    except BadSignatureError:
        return False


# ---------------------------------------------------------------------------
# Command logic (shared between gateway and HTTP modes)
# ---------------------------------------------------------------------------


def _exec_run(project: str, task: str, extra: str | None) -> str:
    try:
        results = _get_orch().run_task(
            project_names=[project],
            task_name=task,
            extra_prompt=extra,
            auto_git=True,
        )
        return _format_results(results)
    except Exception as exc:
        logger.error("discord.cmd_run.error", error=str(exc))
        return f"Error: {exc}"


def _exec_approvals() -> str:
    from hivepilot.services import state_service

    try:
        pending = state_service.get_pending_approvals()
    except Exception as exc:
        return f"Error: {exc}"
    if not pending:
        return "No pending approvals."
    lines = [f"#{r['run_id']} — {r['project']} / {r['task']}" for r in pending]
    return "Pending approvals:\n" + "\n".join(lines)


def _exec_approve(run_id: int) -> str:
    try:
        result = _get_orch().approve_run(run_id=run_id, approve=True, approver="discord")
        status = "succeeded" if result.success else "failed"
        return f"Run #{run_id} approved — {status}."
    except Exception as exc:
        return f"Error: {exc}"


def _exec_deny(run_id: int, reason: str) -> str:
    try:
        _get_orch().approve_run(run_id=run_id, approve=False, approver="discord", reason=reason)
        return f"Run #{run_id} denied."
    except Exception as exc:
        return f"Error: {exc}"


def _exec_status() -> str:
    from hivepilot.services import state_service
    from hivepilot.utils import display_time

    try:
        runs = state_service.list_recent_runs(limit=5)
    except Exception as exc:
        return f"Error: {exc}"
    if not runs:
        return "No recent runs."
    lines = [
        f"[{r['status']}] {r['project']} / {r['task']} — {display_time.to_display(r['started_at'])}"
        for r in runs
    ]
    return "Recent runs:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Natural-language concierge (opt-in, settings.chatops_concierge_enabled) —
# gateway-mode only (`run_gateway`'s `on_message`). HTTP-interactions mode
# never receives plain messages, so there's nothing to hook there. A
# text-reply "yes <token>" / "no" confirmation is used (mirrors Signal's
# chatops_service flow) rather than a discord.ui View/Button, keeping this
# consistent with the rest of the module's dict/JSON-driven, no-SDK-object
# testing style. Execution re-uses `chatops_service._execute_concierge_decision`
# so the SAME ChatOps-token permission check as the native `/run`/`/approve`
# slash commands applies — the confirmation step never bypasses existing
# authorization.
# ---------------------------------------------------------------------------


def _no_mentions() -> Any:
    """`discord.AllowedMentions.none()` — suppresses `@everyone`/`@here`/role
    pings. Every concierge-originated `message.channel.send(...)` passes this
    (answer text and destructive-decision summaries are attacker-influenced —
    LLM-classified free text an unprivileged channel member typed — so a
    crafted "@everyone ..." must never actually ping). Lazily imported: this
    module only lazily imports `discord` inside `run_gateway` (optional
    dependency), and these concierge helpers are only ever invoked from
    within that gateway-mode code path."""
    import discord

    return discord.AllowedMentions.none()


async def _execute_concierge_discord(
    decision: "ConciergeDecision", channel_id: int, message: Any
) -> None:
    """Execute an already-confirmed concierge decision via the SAME
    auth-checked entrypoint the Signal/generic ChatOps text-confirm path
    uses."""
    import asyncio

    from hivepilot.services import chatops_service

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: chatops_service._execute_concierge_decision(
                _get_orch(), decision, f"discord:{channel_id}"
            ),
        )
        await message.channel.send(result, allowed_mentions=_no_mentions())
    except Exception as exc:
        logger.error("discord.concierge.execute_error", error=str(exc))
        await message.channel.send(f"Error: {exc}", allowed_mentions=_no_mentions())


async def _handle_concierge_decision_discord(
    decision: "ConciergeDecision", channel_id: int, owner_user_id: str | None, message: Any
) -> None:
    """Answer directly, execute a non-destructive decision, or mint a
    confirmation token and store the pending decision for a destructive one.
    Every currently-known route/action kind IS destructive (see
    `concierge_service`'s hardcoded table) — the non-destructive branch only
    guards a future kind, never exercised today.

    *owner_user_id* is the Discord id of whoever sent the message that
    produced *decision* — the ONLY id whose later "yes <token>"/"no" reply
    may resolve it (see `PendingConfirmationStore`). A missing id (fail
    closed) means `store()` records nothing, so the prompt is sent but can
    never be resolved to execution by anyone."""
    if decision.kind == "answer":
        await message.channel.send(
            decision.answer_text or "I'm not sure how to help with that. Try /help.",
            allowed_mentions=_no_mentions(),
        )
        return
    if not decision.destructive:
        await _execute_concierge_discord(decision, channel_id, message)
        return

    from hivepilot.services.chatops_service import _summarize_concierge_decision

    token = uuid.uuid4().hex[:8]
    _pending_concierge.store(channel_id, owner_user_id, (token, decision))
    summary = _summarize_concierge_decision(decision)
    await message.channel.send(
        f"⚠️ This will {summary}. Reply 'yes {token}' to confirm or 'no' to cancel.",
        allowed_mentions=_no_mentions(),
    )


async def _handle_concierge_confirmation_discord(
    content: str, channel_id: int, message: Any
) -> None:
    """Handle a "yes <token>" / "no" reply to a pending destructive concierge
    decision. A wrong/stale token is rejected WITHOUT executing or clearing
    the still-valid pending entry — mirrors `chatops_service._dispatch`'s
    `yes <token>` / `no` handling exactly.

    Owner binding + TTL: only the Discord user who triggered the decision
    may resolve it (Yes OR No), and only within
    `_CONCIERGE_CONFIRM_TTL_SECONDS` of it being posted — see
    `PendingConfirmationStore`. A missing/unresolvable author id can never
    resolve anything either (fail closed). The caller only reaches this
    function when `channel_id in _pending_concierge`, so a `None` result
    here means either the entry just expired or this reply came from
    someone other than the owner -- both get the SAME generic response as
    Slack/Telegram, never a hint about which case it was."""
    author_id = getattr(getattr(message, "author", None), "id", None)
    requester_id = str(author_id) if author_id is not None else None
    pending = _pending_concierge.resolve(channel_id, requester_id)
    if pending is None:
        await message.channel.send(
            "This confirmation has expired.", allowed_mentions=_no_mentions()
        )
        return
    if content.strip().lower() == "no":
        _pending_concierge.discard(channel_id)
        await message.channel.send("Cancelled.", allowed_mentions=_no_mentions())
        return
    supplied_token = content.split(None, 1)[1].strip() if " " in content else ""
    stored_token, decision = pending
    if supplied_token != stored_token:
        await message.channel.send(
            "⚠️ This confirmation has expired — please re-send your request.",
            allowed_mentions=_no_mentions(),
        )
        return
    _pending_concierge.discard(channel_id)
    await _execute_concierge_discord(decision, channel_id, message)


# ---------------------------------------------------------------------------
# HTTP interactions mode
# ---------------------------------------------------------------------------


def _handle_component(
    interaction: dict[str, Any], application_id: str, interaction_token: str
) -> None:
    """Process a button component interaction in a background thread."""
    custom_id = interaction.get("data", {}).get("custom_id", "")
    try:
        action, raw_id = custom_id.split(":", 1)
        run_id = int(raw_id)
    except (ValueError, AttributeError):
        _followup_message(
            application_id, interaction_token, {"content": f"Invalid component id: {custom_id!r}"}
        )
        return

    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    approver = user.get("username") or str(user.get("id", "discord"))

    if action == "approve":
        msg = _exec_approve(run_id)
    elif action == "deny":
        msg = _exec_deny(run_id, f"Denied via Discord button by {approver}")
    else:
        msg = f"Unknown action: {action!r}"

    _followup_message(application_id, interaction_token, {"content": msg})


def _approval_components(run_id: int) -> list[dict[str, Any]]:
    """Approve/Deny/Challenge button row -- shared by `notify_approval_required`
    (the initial approval message) and the post-challenge followup (so the
    operator can act again after a Challenge/Ask round-trip), keeping the
    button layout defined in exactly ONE place."""
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 3,
                    "label": "Approve",
                    "custom_id": f"approve:{run_id}",
                },
                {
                    "type": 2,
                    "style": 4,
                    "label": "Deny",
                    "custom_id": f"deny:{run_id}",
                },
                {
                    "type": 2,
                    "style": 2,
                    "label": "Challenge / Ask",
                    "custom_id": f"challenge:{run_id}",
                },
            ],
        }
    ]


def _challenge_modal_response(run_id: int) -> dict[str, Any]:
    """Build the MODAL (response type 9) for the Challenge/Ask button.

    A MODAL response MUST be the immediate, synchronous reply to the
    component interaction -- unlike Approve/Deny it can never be deferred
    and answered later on a background thread. The run_id is encoded in the
    modal's own custom_id (`_CHALLENGE_MODAL_PREFIX`) so no server-side
    pending-state dict is needed between the button press and the submit.
    """
    return {
        "type": 9,  # MODAL
        "data": {
            "custom_id": f"{_CHALLENGE_MODAL_PREFIX}{run_id}",
            "title": f"Challenge / Ask — run #{run_id}"[:45],  # Discord's modal title cap
            "components": [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 4,  # TEXT_INPUT
                            "custom_id": _CHALLENGE_TEXT_CUSTOM_ID,
                            "style": 2,  # PARAGRAPH
                            "label": "Your challenge or question",
                            "required": True,
                            "max_length": 4000,
                        }
                    ],
                }
            ],
        },
    }


def _extract_modal_text(data_block: dict[str, Any], target_custom_id: str) -> str | None:
    """Pull a text-input value out of a MODAL_SUBMIT interaction's nested
    `components` (Discord nests each field inside its own action-row).
    Returns None if the field is missing or its value isn't a string --
    callers must treat that as a malformed/rejected submission, never an
    empty-but-valid answer."""
    for row in data_block.get("components") or []:
        for component in row.get("components") or []:
            if component.get("custom_id") == target_custom_id:
                value = component.get("value")
                return value if isinstance(value, str) else None
    return None


def _handle_challenge_modal_submit(
    run_id: int,
    approver: str,
    challenge_text: str,
    application_id: str,
    interaction_token: str,
) -> None:
    """Dispatch a submitted Challenge/Ask modal in a background thread, via
    the SAME channel-agnostic `Orchestrator.human_challenge()` entrypoint
    Telegram/Slack use — keeps the CoS role-resolution + prompt-dispatch
    logic in ONE place instead of a Discord-specific copy.
    """
    challenge_text = challenge_text[:_CHALLENGE_TEXT_MAX_LEN]
    try:
        cos_response = _get_orch().human_challenge(run_id, challenge_text, approver)
    except Exception as exc:  # noqa: BLE001
        # F4 fix: the full exception (including any runner stderr it may
        # carry -- RunResult.detail reaches this choke-point unredacted) is
        # logged server-side ONLY. Chat gets the exception TYPE name alone,
        # matching repo-wide exception-disclosure discipline -- never the
        # raw message, which could leak a token/path into a shared channel.
        logger.error("discord.challenge.failed", run_id=run_id, error=str(exc))
        _followup_message(
            application_id,
            interaction_token,
            {
                "content": f"⚠️ Challenge error for run #{run_id}: {type(exc).__name__}",
                "allowed_mentions": {"parse": []},
            },
        )
        return

    logger.info("discord.challenge.dispatched", run_id=run_id)

    from hivepilot.streaming.base import split_for

    body = f"🗣 Human → Jules\n{challenge_text}\n\n🛡️ Jules → Human\n{cos_response}"
    chunks = split_for(body, _MAX_MSG_LEN, entity_aware=False)
    for i, chunk in enumerate(chunks):
        # allowed_mentions={"parse": []} on EVERY chunk -- both challenge_text
        # (operator-typed) and cos_response (LLM-generated) are untrusted
        # text that must never trigger an @everyone/@here/role ping.
        payload: dict[str, Any] = {"content": chunk, "allowed_mentions": {"parse": []}}
        if i == len(chunks) - 1:
            # Re-attach Approve/Deny/Challenge buttons to the LAST chunk so
            # the operator can act again -- lands in the SAME channel the
            # interaction happened in via the followup webhook, no
            # dependency on a separately-configured notification channel.
            payload["components"] = _approval_components(run_id)
        _followup_message(application_id, interaction_token, payload)


def handle_interaction(body: bytes, signature: str, timestamp: str) -> dict[str, Any]:
    """
    Process a raw Discord interaction from the FastAPI webhook endpoint.
    Returns a JSON-serialisable dict for FastAPI to return directly.
    """
    data: dict[str, Any] = json.loads(body)
    return _dispatch_interaction(data)


def _dispatch_interaction(data: dict[str, Any]) -> dict[str, Any]:
    """Webhook-transport entrypoint: compute the interaction response and start
    any background work IMMEDIATELY, then return the response dict.

    On the HTTP-interactions transport the ack IS this returned dict, written
    back on the still-open request connection, so starting the worker before
    returning is safe and is exactly the pre-existing ordering -- preserved
    byte-for-byte here. The gateway transport must NOT use this wrapper: see
    `_compute_interaction_response` and `run_gateway`'s `on_interaction`.
    """
    response, dispatch_work = _compute_interaction_response(data)
    if dispatch_work is not None:
        dispatch_work()
    return response


def _compute_interaction_response(
    data: dict[str, Any],
) -> tuple[dict[str, Any], Callable[[], None] | None]:
    """Shared, PURE interaction dispatcher -- computes the Discord
    interaction-response dict for a MESSAGE_COMPONENT (button), MODAL_SUBMIT, or
    APPLICATION_COMMAND (slash command) interaction, regardless of the transport
    it arrived over, and returns it alongside an OPTIONAL `dispatch_work`
    callable that starts the background worker thread.

    Performs no I/O and starts no thread itself -- the caller decides WHEN the
    work begins relative to acknowledging the interaction. That ordering choice
    is transport-critical:

      * `_dispatch_interaction` (HTTP-interactions webhook mode) invokes
        `dispatch_work()` right away -- the returned dict IS the HTTP response
        body FastAPI writes back on the open connection, so the ack is
        effectively already in flight.
      * `run_gateway`'s `on_interaction` (gateway/WebSocket mode) has no HTTP
        response channel: BOTH the ack and the worker's followup are fresh
        outbound HTTPS POSTs. A followup webhook is only valid once the
        interaction has been acknowledged, so a fast-failing worker (e.g.
        `_exec_approve` on a non-pending run -- `run_approved` raises in
        microseconds) would race its followup ahead of the ack and get
        `404 Unknown Webhook`: the operator sees NOTHING while the mutation was
        already attempted. That caller therefore awaits the ack first and only
        then calls `dispatch_work()`.

    The `_is_allowed` guild/channel allow-list check below is the SINGLE choke
    point for interactions and runs IDENTICALLY for both transports -- gateway
    mode is never a weaker second door onto Approve/Deny/Challenge (this fixes
    the previously-dead gateway-mode buttons: no interaction handler was
    registered there at all).
    """
    interaction_type = data.get("type", 0)

    # PING — Discord health check
    if interaction_type == 1:
        return {"type": 1}, None

    application_id = str(data.get("application_id", ""))
    interaction_token = data.get("token", "")

    guild_id = int(data["guild_id"]) if data.get("guild_id") else None
    channel_id = int(data["channel_id"]) if data.get("channel_id") else None

    if not _is_allowed(guild_id, channel_id):
        logger.warning("discord.unauthorized", guild_id=guild_id, channel_id=channel_id)
        return {
            "type": 4,  # CHANNEL_MESSAGE_WITH_SOURCE
            "data": {"content": "Unauthorized.", "flags": 64},
        }, None

    # MESSAGE_COMPONENT (button)
    if interaction_type == 3:
        custom_id = data.get("data", {}).get("custom_id", "")
        action, _, raw_id = custom_id.partition(":")
        if action == "challenge":
            # The Challenge/Ask button opens a MODAL -- a synchronous
            # response, never threaded/deferred like approve/deny.
            try:
                run_id = int(raw_id)
            except ValueError:
                return {
                    "type": 4,
                    "data": {"content": f"Invalid component id: {custom_id!r}", "flags": 64},
                }, None
            logger.info("discord.challenge.requested", run_id=run_id)
            return _challenge_modal_response(run_id), None

        def _start_component_worker() -> None:
            threading.Thread(
                target=_handle_component,
                args=(data, application_id, interaction_token),
                daemon=True,
            ).start()

        return {"type": 5}, _start_component_worker  # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE

    # MODAL_SUBMIT (Challenge/Ask follow-up text)
    if interaction_type == 5:
        modal_custom_id = data.get("data", {}).get("custom_id", "")
        if not modal_custom_id.startswith(_CHALLENGE_MODAL_PREFIX):
            return {
                "type": 4,
                "data": {"content": f"Unknown modal: {modal_custom_id!r}", "flags": 64},
            }, None
        raw_id = modal_custom_id[len(_CHALLENGE_MODAL_PREFIX) :]
        try:
            run_id = int(raw_id)
        except ValueError:
            return {
                "type": 4,
                "data": {"content": f"Invalid modal id: {modal_custom_id!r}", "flags": 64},
            }, None

        challenge_text = _extract_modal_text(data.get("data", {}), _CHALLENGE_TEXT_CUSTOM_ID)
        if not challenge_text or not challenge_text.strip():
            # Fail-closed: an empty/whitespace/missing/non-string submission
            # must NEVER be forwarded to human_challenge as a valid answer.
            return {
                "type": 4,
                "data": {"content": "Challenge/question text cannot be empty.", "flags": 64},
            }, None

        member = data.get("member") or {}
        user = member.get("user") or data.get("user") or {}
        approver = user.get("username") or str(user.get("id", "discord"))
        modal_run_id = run_id
        modal_text = challenge_text.strip()

        def _start_modal_worker() -> None:
            threading.Thread(
                target=_handle_challenge_modal_submit,
                args=(
                    modal_run_id,
                    f"discord:{approver}",
                    modal_text,
                    application_id,
                    interaction_token,
                ),
                daemon=True,
            ).start()

        return {"type": 5}, _start_modal_worker  # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE

    # APPLICATION_COMMAND
    if interaction_type == 2:
        cmd_data = data.get("data", {})
        cmd_name = cmd_data.get("name", "")
        options: dict[str, Any] = {
            opt["name"]: opt.get("value") for opt in cmd_data.get("options", [])
        }

        def _dispatch() -> None:
            try:
                if cmd_name == "run":
                    project = options.get("project", "")
                    task = options.get("task", "")
                    instructions = options.get("instructions")
                    msg = _exec_run(project, task, instructions)
                elif cmd_name == "approvals":
                    msg = _exec_approvals()
                elif cmd_name == "approve":
                    try:
                        run_id = int(options.get("run_id", 0))
                    except (TypeError, ValueError):
                        msg = "Invalid run_id."
                    else:
                        msg = _exec_approve(run_id)
                elif cmd_name == "deny":
                    try:
                        run_id = int(options.get("run_id", 0))
                    except (TypeError, ValueError):
                        msg = "Invalid run_id."
                    else:
                        reason = options.get("reason") or "Denied via Discord"
                        msg = _exec_deny(run_id, reason)
                elif cmd_name == "status":
                    msg = _exec_status()
                else:
                    msg = f"Unknown command: {cmd_name!r}"
            except Exception as exc:
                logger.error("discord.http.dispatch.error", cmd=cmd_name, error=str(exc))
                msg = f"Error: {exc}"
            _followup_message(application_id, interaction_token, {"content": msg})

        def _start_command_worker() -> None:
            threading.Thread(target=_dispatch, daemon=True).start()

        return {"type": 5}, _start_command_worker  # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE

    return {"type": 4, "data": {"content": "Unsupported interaction type.", "flags": 64}}, None


# ---------------------------------------------------------------------------
# Proactive notifications (REST, synchronous)
# ---------------------------------------------------------------------------


def notify_approval_required(*, run_id: int, project: str, task: str) -> None:
    """Post an approval embed with Approve/Deny/Challenge buttons to the
    notification channel."""
    channel_id = settings.discord_notification_channel_id
    if not channel_id:
        raise RuntimeError("No Discord notification channel_id configured")

    payload: dict[str, Any] = {
        "embeds": [
            {
                "title": "Approval required",
                "description": f"**Run #{run_id}**\nProject: `{project}`\nTask: `{task}`",
                "color": 0xFFA500,
            }
        ],
        "components": _approval_components(run_id),
    }
    _post_message(channel_id, payload)


def notify(message: str) -> None:
    """Send a plain text message to the notification channel via REST."""
    channel_id = settings.discord_notification_channel_id
    if not channel_id:
        raise RuntimeError("No Discord notification channel_id configured")
    _post_message(channel_id, {"content": message})


# ---------------------------------------------------------------------------
# Gateway mode  (discord.py — blocking, no public URL needed)
# ---------------------------------------------------------------------------


def run_gateway() -> None:
    """Start the bot in gateway (WebSocket) mode. Blocking. No public URL required."""
    try:
        import discord
        from discord import app_commands
    except ImportError as exc:
        raise RuntimeError("discord.py required: pip install hivepilot[discord]") from exc

    token = _token()

    intents = discord.Intents.default()
    if settings.chatops_concierge_enabled:
        # Plain messages (`on_message`) require the privileged Message
        # Content intent — only requested when the concierge is opt-in
        # enabled. The operator must also enable "Message Content Intent"
        # for the bot in the Discord developer portal, or Discord will
        # silently withhold message content regardless of this flag.
        intents.message_content = True
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    def _guild_check(interaction: discord.Interaction) -> bool:
        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        return _is_allowed(guild_id, channel_id)

    @tree.command(name="run", description="Trigger a HivePilot task")
    @app_commands.describe(
        project="Project name",
        task="Task name",
        instructions="Optional extra instructions",
    )
    async def cmd_run(
        interaction: discord.Interaction,
        project: str,
        task: str,
        instructions: str | None = None,
    ) -> None:
        if not _guild_check(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        await interaction.response.defer()
        import asyncio

        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(None, lambda: _exec_run(project, task, instructions))
        await interaction.followup.send(msg)

    @tree.command(name="approvals", description="List pending approvals")
    async def cmd_approvals(interaction: discord.Interaction) -> None:
        if not _guild_check(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        await interaction.response.defer()
        import asyncio

        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(None, _exec_approvals)
        await interaction.followup.send(msg)

    @tree.command(name="approve", description="Approve a pending run")
    @app_commands.describe(run_id="Run ID to approve")
    async def cmd_approve(interaction: discord.Interaction, run_id: int) -> None:
        if not _guild_check(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        await interaction.response.defer()
        import asyncio

        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(None, lambda: _exec_approve(run_id))
        await interaction.followup.send(msg)

    @tree.command(name="deny", description="Deny a pending run")
    @app_commands.describe(run_id="Run ID to deny", reason="Optional reason")
    async def cmd_deny(
        interaction: discord.Interaction,
        run_id: int,
        reason: str | None = None,
    ) -> None:
        if not _guild_check(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        await interaction.response.defer()
        import asyncio

        loop = asyncio.get_event_loop()
        effective_reason = reason or "Denied via Discord"
        msg = await loop.run_in_executor(None, lambda: _exec_deny(run_id, effective_reason))
        await interaction.followup.send(msg)

    @tree.command(name="status", description="Show last 5 runs")
    async def cmd_status(interaction: discord.Interaction) -> None:
        if not _guild_check(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        await interaction.response.defer()
        import asyncio

        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(None, _exec_status)
        await interaction.followup.send(msg)

    @client.event
    async def on_ready() -> None:
        await tree.sync()
        logger.info("discord.gateway.ready", user=str(client.user))

    # NOTE (dependency assumption): the "no double-response" reasoning in
    # `on_interaction` below -- that APPLICATION_COMMAND interactions must NOT
    # be routed through it because `app_commands.CommandTree` already
    # auto-dispatches them, and that no persistent `discord.ui.View` is
    # registered for the Approve/Deny/Challenge buttons (they are raw component
    # dicts built by `_approval_components`, so discord.py has no View callback
    # to fire in addition to this handler) -- is specific to **discord.py 2.x**
    # (`discord.py>=2.3` per the optional `discord` extra in pyproject.toml).
    # If that major version ever changes, re-verify both properties before
    # trusting this handler: a 3.x dispatch-model change could double-invoke.
    @client.event
    async def on_interaction(interaction: discord.Interaction) -> None:
        """F1 fix (live production bug): route MESSAGE_COMPONENT (Approve/
        Deny/Challenge buttons) and MODAL_SUBMIT (Challenge/Ask follow-up
        text) gateway interactions into the SAME `_dispatch_interaction`
        dict-driven dispatcher the HTTP-interactions webhook path
        (`handle_interaction`) uses -- never a gateway-specific copy.

        Before this handler existed, gateway mode registered slash commands
        + `on_ready` + `on_message` only. A button/modal interaction was
        therefore NEVER acknowledged within Discord's 3-second deadline --
        Discord showed "This interaction failed" for every Approve/Deny/
        Challenge press on a gateway-mode bot (the CLI default and the mode
        documented for boxes with no public IP), with no server-side log
        line at all.

        APPLICATION_COMMAND (slash command) interactions are deliberately
        NOT routed here: the `app_commands.CommandTree` constructed above
        already auto-dispatches those via discord.py's internal wiring --
        routing them here too would double-invoke every slash command.
        """
        itype = int(getattr(interaction.type, "value", interaction.type))
        if itype not in (3, 5):  # MESSAGE_COMPONENT, MODAL_SUBMIT
            return

        raw_data = interaction.data
        if not isinstance(raw_data, dict):
            raw_data = dict(raw_data) if raw_data else {}

        user = interaction.user
        user_dict = (
            {"username": getattr(user, "name", None), "id": getattr(user, "id", None)}
            if user is not None
            else {}
        )

        # Reconstruct the SAME raw interaction-dict shape `_dispatch_interaction`
        # already parses from the HTTP webhook's JSON body -- guild_id/
        # channel_id are already ints (or None) on a discord.py Interaction,
        # matching what `_is_allowed` expects; no dict-shape fork.
        raw: dict[str, Any] = {
            "type": itype,
            "application_id": str(interaction.application_id),
            "token": interaction.token,
            "guild_id": interaction.guild_id,
            "channel_id": interaction.channel_id,
            "data": raw_data,
            "user": user_dict,
        }
        # ORDERING IS LOAD-BEARING on this transport. `_compute_interaction_response`
        # is pure -- it does NOT start the worker -- so the ack below goes out
        # FIRST and the worker is only started once that ack has landed.
        # Unlike the webhook transport (where the ack is written back on the
        # already-open HTTP request), gateway mode sends BOTH the ack and the
        # worker's followup as fresh outbound HTTPS POSTs. Discord rejects a
        # followup webhook that arrives before its interaction is acknowledged
        # with `404 Unknown Webhook`, and a fast-failing worker (`_exec_approve`
        # on a non-pending run raises from `run_approved` in microseconds) will
        # win that race -- the operator would see nothing at all while the
        # mutation had already been attempted.
        response, dispatch_work = _compute_interaction_response(raw)

        import asyncio

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: _send_interaction_response(
                    str(interaction.id), interaction.token, response
                ),
            )
        except Exception as exc:  # noqa: BLE001
            # Without this guard a `raise_for_status()` failure escapes into
            # discord.py's event-dispatch loop: the operator is left with
            # "This interaction failed" and no server-side signal. Fail CLOSED
            # -- the worker is deliberately NOT started, because its followup
            # webhook is unusable without a delivered ack, so running it would
            # mutate a run the operator can never be told about.
            logger.error(
                "discord.gateway.ack_failed",
                interaction_type=itype,
                error=str(exc),
            )
            return

        if dispatch_work is not None:
            dispatch_work()

    @client.event
    async def on_message(message: Any) -> None:
        """Natural-language concierge (opt-in, gateway-mode only). First line
        guarantees byte-identical (no-op) behaviour when the flag is off."""
        if not settings.chatops_concierge_enabled:
            return
        # Ignore the bot's own messages to avoid loops.
        if message.author == client.user:
            return
        guild = getattr(message, "guild", None)
        guild_id = guild.id if guild is not None else None
        channel = getattr(message, "channel", None)
        channel_id = channel.id if channel is not None else None
        if not _is_allowed(guild_id, channel_id):
            return
        content = (message.content or "").strip()
        if not content:
            return

        lowered = content.lower()
        if channel_id in _pending_concierge and (lowered == "no" or lowered.startswith("yes ")):
            await _handle_concierge_confirmation_discord(content, channel_id, message)
            return

        import asyncio

        from hivepilot.services import concierge_service

        # `conversation_id` + `user_id` enable pending follow-up offers (see
        # concierge_service's "Pending follow-up offers" section) so a bare
        # "yes"/"oui" answering an offer the concierge just made is honoured
        # — bound to this channel AND to the person who was asked, so a
        # colleague's unrelated "yes" can never fire it. A missing channel or
        # author disables offers for this message (fail closed).
        author_id = getattr(getattr(message, "author", None), "id", None)
        owner_user_id = str(author_id) if author_id is not None else None
        loop = asyncio.get_event_loop()
        decision = await loop.run_in_executor(
            None,
            lambda: concierge_service.route(
                content,
                default_role=settings.chatops_default_role,
                default_target=settings.default_target,
                conversation_id=f"discord:{channel_id}" if channel_id is not None else None,
                user_id=owner_user_id,
            ),
        )
        await _handle_concierge_decision_discord(decision, channel_id, owner_user_id, message)

    logger.info("discord.gateway.start")
    client.run(token)
