from __future__ import annotations

import os
import threading
from typing import Any

from hivepilot.config import settings
from hivepilot.orchestrator import Orchestrator
from hivepilot.services import state_service, token_service
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_orchestrator: Orchestrator | None = None
_orchestrator_lock = threading.Lock()


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


def _dispatch(command: str, args: list[str], source: str) -> str:
    """Common dispatch logic shared by all ChatOps sources."""
    orch = _get_orchestrator()

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
