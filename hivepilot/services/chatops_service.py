from __future__ import annotations

import os
import threading
import uuid
from typing import TYPE_CHECKING, Any

from hivepilot.config import settings
from hivepilot.orchestrator import Orchestrator
from hivepilot.services import state_service, token_service
from hivepilot.utils.logging import get_logger

if TYPE_CHECKING:
    from hivepilot.services.concierge_service import ConciergeDecision

logger = get_logger(__name__)

_orchestrator: Orchestrator | None = None
_orchestrator_lock = threading.Lock()

# Natural-language concierge (opt-in, `settings.chatops_concierge_enabled`):
# pending destructive route/action decisions awaiting a text "yes <token>" /
# "no" confirmation reply, keyed by `source` ("signal", "slack", ...) — the
# shared `_dispatch` signature has no per-sender identity to key on (Signal's
# `handle_signal` only ever passes `{"text": ...}`, see its docstring), so
# this is (by design, see sprint spec) one pending decision per SOURCE, not
# per sender. Value: (confirmation_token, decision).
_pending_concierge_text: dict[str, tuple[str, "ConciergeDecision"]] = {}


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
        f"run_id={r['run_id']} project={r['project']} task={r['task']} requested={r['requested_at']}"
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
            orch.run_approved(run_id=run_id, approve=approve, approver=source, reason=reason)
            return f"{'Approved' if approve else 'Denied'} run {run_id}"

    return "Nothing to do."


def _handle_concierge_decision(
    orch: Orchestrator, decision: "ConciergeDecision", source: str
) -> str:
    if decision.kind == "answer":
        return decision.answer_text or "I'm not sure how to help with that. Try /help."
    if not decision.destructive:
        # Every currently-known route/action kind IS destructive (see
        # concierge_service's hardcoded table) — this only guards a future
        # non-destructive action kind, never exercised today.
        return _execute_concierge_decision(orch, decision, source)
    token = uuid.uuid4().hex[:8]
    _pending_concierge_text[source] = (token, decision)
    summary = _summarize_concierge_decision(decision)
    return f"⚠️ This will {summary}. Reply 'yes {token}' to confirm or 'no' to cancel."


def _dispatch(command: str, args: list[str], source: str) -> str:
    """Common dispatch logic shared by all ChatOps sources."""
    orch = _get_orchestrator()

    # Concierge confirmation replies ("yes <token>" / "no") are checked FIRST,
    # before any command parsing — but only when a decision is actually
    # pending for this source AND the flag is on, so a plain "/yes" typed by
    # someone with nothing pending (or with the flag off) still falls through
    # unchanged to the normal dispatch below.
    if settings.chatops_concierge_enabled and command in ("yes", "no"):
        pending = _pending_concierge_text.get(source)
        if pending is not None:
            token, decision = pending
            if command == "no":
                del _pending_concierge_text[source]
                return "Cancelled."
            supplied_token = args[0] if args else None
            if supplied_token != token:
                return "Invalid or expired confirmation token."
            del _pending_concierge_text[source]
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
        orch.run_approved(run_id=run_id, approve=approve, approver=source, reason=reason)
        return f"{'Approved' if approve else 'Denied'} run {run_id}"

    if command == "status":
        _verify("run")
        runs = state_service.list_recent_runs(limit=5)
        if not runs:
            return "No recent runs."
        lines = [f"[{r['status']}] {r['project']} / {r['task']} — {r['started_at']}" for r in runs]
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
        return _handle_concierge_decision(orch, decision, source)

    return f"Unknown command: {command}"


# ---------------------------------------------------------------------------
# Source-specific handlers — parse platform command format, delegate to _dispatch
# ---------------------------------------------------------------------------


def handle_slack(payload: dict[str, str]) -> str:
    """Handle Slack slash command payload."""
    command = payload.get("command", "")
    text = payload.get("text", "").strip()
    args = text.split() if text else []
    # /hivepilot-run → run, /hivepilot-approvals → approvals, etc.
    action = command.lstrip("/").removeprefix("hivepilot-")
    return _dispatch(action, args, source="slack")


def handle_discord(payload: dict[str, str]) -> str:
    """Handle Discord message payload (prefix: !hp <command> [args])."""
    content = payload.get("content", "").strip()
    parts = content.split()
    # expect: !hp <command> [args…]
    if len(parts) < 2 or parts[0] != "!hp":
        return "Unknown command"
    return _dispatch(parts[1], parts[2:], source="discord")


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
    return _dispatch(command, parts[1:], source="telegram")


def handle_signal(payload: dict[str, str]) -> str:
    """Handle a Signal message body (commands: run, approvals, approve, deny, status).

    Signal has no cloud bot API / inbound webhook (it's E2E P2P) -- unlike
    handle_slack/handle_discord/handle_telegram above (all driven by an inbound
    HTTP webhook payload), this is called directly by `signal_bot.SignalBot`'s
    pull-only receive loop (signal-cli `receive` / signal-cli-rest-api polling)
    for each inbound message, not via a FastAPI route. The leading `/` is
    optional so both `/run acme deploy` and Signal's natural reply style
    (`approve 42`, `deny 42 not ready`) route the same way -- there are no
    inline buttons on Signal, so `approve <run_id>` / `deny <run_id>` is the
    only approval UX available.
    """
    text = payload.get("text", "").strip()
    if not text:
        return "Unknown command"
    parts = text.split()
    command = parts[0].lstrip("/")
    return _dispatch(command, parts[1:], source="signal")
