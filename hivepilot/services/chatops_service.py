from __future__ import annotations

import os
from typing import Dict

from hivepilot.config import settings
from hivepilot.orchestrator import Orchestrator
from hivepilot.services import state_service, token_service
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)
orchestrator = Orchestrator()


def _verify(required: str) -> None:
    token_value = settings.chatops_token or os.environ.get("HIVEPILOT_CHATOPS_TOKEN")
    if not token_value:
        raise RuntimeError("HIVEPILOT_CHATOPS_TOKEN not configured")
    entry = token_service.resolve_token(token_value)
    if not entry or token_service.role_rank(entry.role) < token_service.role_rank(required):
        raise RuntimeError("ChatOps token lacks permission")


def handle_slack(payload: Dict[str, str]) -> str:
    command = payload.get("command")
    text = payload.get("text", "").strip()
    if command == "/hivepilot-run":
        _verify("run")
        parts = text.split()
        if len(parts) < 2:
            return "Usage: /hivepilot-run <project> <task>"
        project, task = parts[:2]
        orchestrator.run_task(project_names=[project], task_name=task, extra_prompt=None, auto_git=False)
        return f"Triggered {task} on {project}"
    if command == "/hivepilot-approvals":
        _verify("run")
        pending = state_service.get_pending_approvals()
        if not pending:
            return "No pending approvals."
        lines = [
            f"run_id={row['run_id']} project={row['project']} task={row['task']} requested={row['requested_at']}"
            for row in pending
        ]
        return "\n".join(lines)
    if command == "/hivepilot-approve":
        _verify("approve")
        parts = text.split()
        if not parts:
            return "Usage: /hivepilot-approve <run_id>"
        run_id = int(parts[0])
        orchestrator.run_approved(run_id=run_id, approve=True, approver="slack")
        return f"Approved run {run_id}"
    if command == "/hivepilot-deny":
        _verify("approve")
        parts = text.split()
        if not parts:
            return "Usage: /hivepilot-deny <run_id>"
        run_id = int(parts[0])
        orchestrator.run_approved(run_id=run_id, approve=False, approver="slack", reason="Denied via Slack")
        return f"Denied run {run_id}"
    return "Unknown command"


def handle_discord(payload: Dict[str, str]) -> str:
    content = payload.get("content", "").strip()
    if content.startswith("!hp run"):
        _verify("run")
        parts = content.split()
        if len(parts) < 4:
            return "Usage: !hp run <project> <task>"
        project, task = parts[2:4]
        orchestrator.run_task(project_names=[project], task_name=task, extra_prompt=None, auto_git=False)
        return f"Triggered {task} on {project}"
    if content.startswith("!hp approvals"):
        _verify("run")
        pending = state_service.get_pending_approvals()
        if not pending:
            return "No pending approvals."
        lines = [
            f"run_id={row['run_id']} project={row['project']} task={row['task']} requested={row['requested_at']}"
            for row in pending
        ]
        return "\n".join(lines)
    if content.startswith("!hp approve"):
        _verify("approve")
        parts = content.split()
        if len(parts) < 3:
            return "Usage: !hp approve <run_id>"
        run_id = int(parts[2])
        orchestrator.run_approved(run_id=run_id, approve=True, approver="discord")
        return f"Approved run {run_id}"
    if content.startswith("!hp deny"):
        _verify("approve")
        parts = content.split()
        if len(parts) < 3:
            return "Usage: !hp deny <run_id>"
        run_id = int(parts[2])
        orchestrator.run_approved(run_id=run_id, approve=False, approver="discord", reason="Denied via Discord")
        return f"Denied run {run_id}"
    return "Unknown command"


def handle_telegram(update: Dict[str, Any]) -> str:
    message = update.get("message") or {}
    text = message.get("text", "").strip()
    if not text.startswith("/hp"):
        return "Unsupported command"
    if text.startswith("/hp_run"):
        _verify("run")
        parts = text.split()
        if len(parts) < 3:
            return "Usage: /hp_run <project> <task>"
        project, task = parts[1:3]
        orchestrator.run_task(project_names=[project], task_name=task, extra_prompt=None, auto_git=False)
        return f"Triggered {task} on {project}"
    if text.startswith("/hp_approvals"):
        _verify("run")
        pending = state_service.get_pending_approvals()
        if not pending:
            return "No pending approvals."
        lines = [
            f"run_id={row['run_id']} project={row['project']} task={row['task']} requested={row['requested_at']}"
            for row in pending
        ]
        return "\n".join(lines)
    if text.startswith("/hp_approve"):
        _verify("approve")
        parts = text.split()
        if len(parts) < 2:
            return "Usage: /hp_approve <run_id>"
        run_id = int(parts[1])
        orchestrator.run_approved(run_id=run_id, approve=True, approver="telegram")
        return f"Approved run {run_id}"
    if text.startswith("/hp_deny"):
        _verify("approve")
        parts = text.split()
        if len(parts) < 2:
            return "Usage: /hp_deny <run_id>"
        run_id = int(parts[1])
        orchestrator.run_approved(run_id=run_id, approve=False, approver="telegram", reason="Denied via Telegram")
        return f"Denied run {run_id}"
    return "Unknown command"
