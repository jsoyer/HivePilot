from __future__ import annotations

import os
import threading
import uuid
from typing import TYPE_CHECKING, Any

from hivepilot.config import settings
from hivepilot.orchestrator import Orchestrator
from hivepilot.services import state_service, token_service
from hivepilot.services.pending_confirmation import PendingConfirmationStore
from hivepilot.utils import display_time
from hivepilot.utils.logging import get_logger

if TYPE_CHECKING:
    from hivepilot.services.concierge_service import ConciergeDecision

logger = get_logger(__name__)

_orchestrator: Orchestrator | None = None
_orchestrator_lock = threading.Lock()

# Natural-language concierge (opt-in, `settings.chatops_concierge_enabled`):
# pending destructive route/action decisions awaiting a text "yes <token>" /
# "no" confirmation reply, keyed by `source` ("signal", "slack", ...).
#
# Security fix (SAME bug class as `slack_bot._PendingChallenge` / F3,
# `concierge_service._PendingOffer`, and the bot modules' `_pending_concierge`
# fixed via `PendingConfirmationStore` in PR #365): this used to be a plain
# `dict[str, tuple[str, ConciergeDecision]]` — keyed by SOURCE ONLY, which is
# actually WORSE than the other instances of this bug: not merely
# per-conversation-not-per-sender, but shared by every caller of that
# source's `/chatops/*` webhook, with no per-sender identity check and no
# expiry. Any Slack user hitting `/chatops/slack` could resolve/cancel ANY
# other Slack user's pending destructive decision on this endpoint, and an
# abandoned confirmation never expired. Now backed by `PendingConfirmationStore`
# and bound to a per-source *requester_id*, threaded in from each `handle_*`
# entry point's own payload (Slack's `user_id`, Discord's `author.id`,
# Telegram's `message.from.id`, Signal's `sender` phone number — see the
# `_dispatch(..., requester_id=...)` call sites below). A source that cannot
# supply a requester_id for a given call gets a missing owner: `store()`
# records nothing (fail closed) rather than silently falling back to the old
# "one shared pending decision per source" behaviour.
_CONCIERGE_TEXT_CONFIRM_TTL_SECONDS = 10 * 60
"""TTL for a pending typed "yes <token>" / "no" concierge confirmation reply.

Same operator-effort class as `concierge_service._OFFER_TTL_SECONDS` (10
min): resolving this costs strictly more than a button tap
(`_CONCIERGE_CONFIRM_TTL_SECONDS` in the bot modules, 5 min — nothing to
type, no token to find) since the operator must type a reply that includes
the confirmation token, but it is not a composed follow-up sentence either
(`_CHALLENGE_TTL_SECONDS`, 15 min) — it's a short, mostly-copy-pasted typed
reply, the same effort shape `_OFFER_TTL_SECONDS` was chosen for."""

_pending_concierge_text: PendingConfirmationStore[tuple[str, "ConciergeDecision"]] = (
    PendingConfirmationStore(_CONCIERGE_TEXT_CONFIRM_TTL_SECONDS)
)


def _get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        with _orchestrator_lock:
            if _orchestrator is None:
                _orchestrator = Orchestrator()
    return _orchestrator


def _verify(required: str) -> None:
    token_value = settings.chatops_token or os.environ.get("HIVEPILOT_CHATOPS_TOKEN")
    if not token_value:
        raise RuntimeError("HIVEPILOT_CHATOPS_TOKEN not configured")
    entry = token_service.resolve_token(token_value)
    if not entry or token_service.role_rank(entry.role) < token_service.role_rank(required):
        raise RuntimeError("ChatOps token lacks permission")


def _format_approvals(pending: list[dict]) -> str:
    if not pending:
        return "No pending approvals."
    return "\n".join(
        f"run_id={r['run_id']} project={r['project']} task={r['task']} "
        f"requested={display_time.to_display(r['requested_at'])}"
        for r in pending
    )


# ---------------------------------------------------------------------------
# Natural-language concierge integration (opt-in — settings.chatops_concierge_enabled)
# ---------------------------------------------------------------------------


def _summarize_concierge_decision(decision: "ConciergeDecision") -> str:
    """Short human-readable summary of a destructive decision, for the
    text-only Signal/chatops confirmation prompt."""
    if decision.kind == "route":
        target = decision.target or "the default project"
        order = f": {decision.order}" if decision.order else ""
        return f"ask {decision.role_key} to work on {target}{order}"
    if decision.kind == "action":
        if decision.action in ("approve", "deny"):
            run_id = (decision.params or {}).get("run_id")
            return f"{decision.action} run {run_id}"
        target = decision.target or "the default project"
        return f"{decision.action} on {target}"
    return "perform this action"


def _execute_concierge_decision(
    orch: Orchestrator, decision: "ConciergeDecision", source: str
) -> str:
    """Execute an already-confirmed destructive route/action decision.

    Re-verifies against the ChatOps token at the SAME permission level the
    equivalent explicit command would require (`run` for route/run/
    run_pipeline, `approve` for approve/deny) — the confirmation step does
    not bypass the existing token-based authorization model.
    """
    if decision.kind == "route":
        _verify("run")
        from hivepilot.roles import get_role

        try:
            role = get_role(decision.role_key or "")
        except Exception:
            return f"Role {decision.role_key!r} is not configured."
        task_name = role.command_task
        if not task_name:
            return f"{decision.role_key} has no direct-command task configured."
        target = decision.target or settings.default_target
        orch.run_task(
            project_names=[target],
            task_name=task_name,
            extra_prompt=decision.order or None,
            auto_git=True,
        )
        return f"Triggered {task_name} on {target}"

    if decision.kind == "action":
        params = decision.params or {}

        if decision.action == "run":
            _verify("run")
            task = params.get("task")
            if not task:
                return "Missing task name — cannot run."
            target = decision.target or settings.default_target
            extra = params.get("order") or params.get("extra_prompt")
            orch.run_task(project_names=[target], task_name=task, extra_prompt=extra, auto_git=True)
            return f"Triggered {task} on {target}"

        if decision.action == "run_pipeline":
            _verify("run")
            target = decision.target or settings.default_target
            pipeline = params.get("pipeline") or settings.default_pipeline
            orch.run_pipeline(
                project_names=[target],
                pipeline_name=pipeline,
                extra_prompt=params.get("order"),
                auto_git=True,
            )
            return f"Triggered pipeline {pipeline} on {target}"

        if decision.action in ("approve", "deny"):
            _verify("approve")
            try:
                run_id = int(params.get("run_id"))
            except (TypeError, ValueError):
                return "Invalid run id."
            approve = decision.action == "approve"
            reason = None if approve else f"Denied via {source.title()} concierge"
            try:
                orch.approve_run(run_id=run_id, approve=approve, approver=source, reason=reason)
            except ValueError as exc:
                # Not pending / unknown run -- surface a clean message instead
                # of letting this bubble up through _dispatch -> handle_* ->
                # the /chatops/* endpoint as an unhandled 500 (same posture as
                # api_service.handle_approval's ValueError -> 400).
                return str(exc)
            return f"{'Approved' if approve else 'Denied'} run {run_id}"

    return "Nothing to do."


def _handle_concierge_decision(
    orch: Orchestrator, decision: "ConciergeDecision", source: str, requester_id: str | None
) -> str:
    if decision.kind == "answer":
        return decision.answer_text or "I'm not sure how to help with that. Try /help."
    if not decision.destructive:
        # Every currently-known route/action kind IS destructive (see
        # concierge_service's hardcoded table) — this only guards a future
        # non-destructive action kind, never exercised today.
        return _execute_concierge_decision(orch, decision, source)
    token = uuid.uuid4().hex[:8]
    # Owner binding: only *requester_id* (the sender this decision was
    # classified from — see each `handle_*` entry point) may later resolve
    # this with "yes <token>"/"no". A missing id (fail closed) means
    # `store()` records nothing, so this confirmation prompt is sent but can
    # never be resolved to execution by anyone.
    _pending_concierge_text.store(source, requester_id, (token, decision))
    summary = _summarize_concierge_decision(decision)
    return f"⚠️ This will {summary}. Reply 'yes {token}' to confirm or 'no' to cancel."


def _dispatch(command: str, args: list[str], source: str, requester_id: str | None = None) -> str:
    """Common dispatch logic shared by all ChatOps sources.

    *requester_id* identifies the sender within *source* (Slack's `user_id`,
    Discord's `author.id`, Telegram's `message.from.id`, Signal's E.164
    `sender`) — see `_pending_concierge_text`'s docstring. Optional and
    defaults to None so existing internal callers/tests that don't care
    about ownership (e.g. non-concierge commands) are unaffected; a caller
    that omits it can never store OR resolve a concierge confirmation
    (fail closed), never "whoever asks first".
    """
    orch = _get_orchestrator()

    # Concierge confirmation replies ("yes <token>" / "no") are checked FIRST,
    # before any command parsing — but only when a decision is actually
    # pending for this source AND the flag is on, so a plain "/yes" typed by
    # someone with nothing pending (or with the flag off) still falls through
    # unchanged to the normal dispatch below. `resolve()` folds "no entry",
    # "expired", "wrong requester", and "no requester id" into the same
    # `None` — from THIS requester's point of view there is nothing pending,
    # so "yes"/"no" falls through and gets classified as ordinary text below,
    # exactly as if they had never asked. It never distinguishes those cases
    # to the caller because none of them may ever execute or cancel anything.
    if settings.chatops_concierge_enabled and command in ("yes", "no"):
        pending = _pending_concierge_text.resolve(source, requester_id)
        if pending is not None:
            token, decision = pending
            if command == "no":
                _pending_concierge_text.discard(source)
                return "Cancelled."
            supplied_token = args[0] if args else None
            if supplied_token != token:
                return "Invalid or expired confirmation token."
            _pending_concierge_text.discard(source)
            return _execute_concierge_decision(orch, decision, source)

    if command == "run":
        _verify("run")
        if len(args) < 2:
            return "Usage: run <project> <task>"
        project, task = args[0], args[1]
        extra = " ".join(args[2:]) if len(args) > 2 else None
        orch.run_task(project_names=[project], task_name=task, extra_prompt=extra, auto_git=True)
        return f"Triggered {task} on {project}"

    if command == "approvals":
        _verify("run")
        return _format_approvals(state_service.get_pending_approvals())

    if command in ("approve", "deny"):
        _verify("approve")
        if not args:
            return f"Usage: {command} <run_id>"
        try:
            run_id = int(args[0])
        except ValueError:
            return f"Invalid run_id: {args[0]!r}"
        approve = command == "approve"
        reason = None if approve else f"Denied via {source.title()}"
        try:
            orch.approve_run(run_id=run_id, approve=approve, approver=source, reason=reason)
        except ValueError as exc:
            # Not pending / unknown run -- surface a clean message instead of
            # letting this bubble up through handle_* -> the /chatops/*
            # endpoint as an unhandled 500 (same posture as
            # api_service.handle_approval's ValueError -> 400).
            return str(exc)
        return f"{'Approved' if approve else 'Denied'} run {run_id}"

    if command == "status":
        _verify("run")
        runs = state_service.list_recent_runs(limit=5)
        if not runs:
            return "No recent runs."
        lines = [
            f"[{r['status']}] {r['project']} / {r['task']} — "
            f"{display_time.to_display(r['started_at'])}"
            for r in runs
        ]
        return "Recent runs:\n" + "\n".join(lines)

    if settings.chatops_concierge_enabled:
        _verify("run")
        from hivepilot.services import concierge_service

        text = " ".join([command, *args]).strip()
        decision = concierge_service.route(
            text,
            default_role=settings.chatops_default_role,
            default_target=settings.default_target,
        )
        return _handle_concierge_decision(orch, decision, source, requester_id)

    return f"Unknown command: {command}"


# ---------------------------------------------------------------------------
# Source-specific handlers — parse platform command format, delegate to _dispatch
# ---------------------------------------------------------------------------


def _slack_requester_id(payload: dict[str, Any]) -> str | None:
    """Slack slash-command payloads carry the invoking user's id as
    `user_id` (standard Slack field). Missing/falsy -> None (fail closed for
    `_pending_concierge_text`'s owner binding, not a crash)."""
    user_id = payload.get("user_id")
    return str(user_id) if user_id else None


def _discord_requester_id(payload: dict[str, Any]) -> str | None:
    """A Discord message payload's standard `author.id` field. Tolerates a
    malformed/missing `author` (not a dict, or no `id`) by returning None
    rather than raising — this endpoint must never crash on an adversarial
    payload (see test_pentest.py's injection fuzz tests)."""
    author = payload.get("author")
    if not isinstance(author, dict):
        return None
    user_id = author.get("id")
    return str(user_id) if user_id else None


def _telegram_requester_id(message: dict[str, Any]) -> str | None:
    """A Telegram Bot API `Message` object's standard `from.id` field —
    same field `telegram_bot.py`'s `_concierge_user_id` reads off the live
    SDK object. Tolerates a malformed/missing `from` (e.g. an anonymous
    channel post carries none) by returning None."""
    sender = message.get("from")
    if not isinstance(sender, dict):
        return None
    user_id = sender.get("id")
    return str(user_id) if user_id is not None else None


def _signal_requester_id(payload: dict[str, Any]) -> str | None:
    """The E.164 phone number `signal_bot._handle_envelope` already resolves
    per inbound message, threaded in via the optional `sender` key (see
    `signal_bot._dispatch_text`). Missing (e.g. an older/alternate caller
    that only ever sends `{"text": ...}`) -> None, fail closed."""
    sender = payload.get("sender")
    return str(sender) if sender else None


def handle_slack(payload: dict[str, Any]) -> str:
    """Handle Slack slash command payload."""
    command = payload.get("command", "")
    text = payload.get("text", "").strip()
    args = text.split() if text else []
    # /hivepilot-run → run, /hivepilot-approvals → approvals, etc.
    action = command.lstrip("/").removeprefix("hivepilot-")
    return _dispatch(action, args, source="slack", requester_id=_slack_requester_id(payload))


def handle_discord(payload: dict[str, Any]) -> str:
    """Handle Discord message payload (prefix: !hp <command> [args])."""
    content = payload.get("content", "").strip()
    parts = content.split()
    # expect: !hp <command> [args…]
    if len(parts) < 2 or parts[0] != "!hp":
        return "Unknown command"
    return _dispatch(
        parts[1], parts[2:], source="discord", requester_id=_discord_requester_id(payload)
    )


def handle_telegram(update: dict[str, Any]) -> str:
    """Handle Telegram update payload (commands: /hp_run, /hp_approvals, etc.)."""
    message = update.get("message") or {}
    text = message.get("text", "").strip()
    if not text.startswith("/hp"):
        return "Unsupported command"
    parts = text.split()
    # /hp_run project task → command=run, args=[project, task]
    raw_command = parts[0].lstrip("/")  # hp_run
    command = raw_command.removeprefix("hp_").removeprefix("hp")  # run
    return _dispatch(
        command, parts[1:], source="telegram", requester_id=_telegram_requester_id(message)
    )


def handle_signal(payload: dict[str, Any]) -> str:
    """Handle a Signal message body (commands: run, approvals, approve, deny, status).

    Signal has no cloud bot API / inbound webhook (it's E2E P2P) -- unlike
    handle_slack/handle_discord/handle_telegram above (all driven by an inbound
    HTTP webhook payload), this is called directly by `signal_bot.SignalBot`'s
    pull-only receive loop (signal-cli `receive` / signal-cli-rest-api polling)
    for each inbound message, not via a FastAPI route. The leading `/` is
    optional so both `/run acme deploy` and Signal's natural reply style
    (`approve 42`, `deny 42 not ready`) route the same way -- there are no
    inline buttons on Signal, so `approve <run_id>` / `deny <run_id>` is the
    only approval UX available. *payload* may carry an optional `sender`
    key (the E.164 number `signal_bot._handle_envelope` already resolved per
    message, threaded in by `_dispatch_text`) -- used as the owner id for
    `_pending_concierge_text`; omitted, it's None (fail closed, never
    "whoever answers first").
    """
    text = payload.get("text", "").strip()
    if not text:
        return "Unknown command"
    parts = text.split()
    command = parts[0].lstrip("/")
    return _dispatch(
        command, parts[1:], source="signal", requester_id=_signal_requester_id(payload)
    )
