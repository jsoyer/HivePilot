from __future__ import annotations

import asyncio
import os
import subprocess
import unicodedata
from typing import Any

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Lazily-initialised Application instance (used by webhook/FastAPI mode)
_app_instance = None

# pending challenge state: {chat_id: (run_id, approver_username)}
_pending_challenges: dict[int, tuple[int, str]] = {}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _token() -> str:
    token = settings.telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Telegram bot token not configured. "
            "Set HIVEPILOT_TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN."
        )
    return token


def _is_allowed(chat_id: int) -> bool:
    """Return True if chat_id is whitelisted (open to all when list is empty)."""
    allowed = settings.telegram_allowed_chat_ids
    if not allowed:
        return True
    return chat_id in allowed


def _require_allowed(chat_id: int) -> bool:
    if not _is_allowed(chat_id):
        logger.warning("telegram.unauthorized", chat_id=chat_id)
        return False
    return True


def _get_orch():
    from hivepilot.services.chatops_service import _get_orchestrator

    return _get_orchestrator()


def _notification_chat_id() -> int | None:
    """Return the chat_id to use for proactive notifications."""
    if settings.telegram_notification_chat_id:
        return settings.telegram_notification_chat_id
    if settings.telegram_allowed_chat_ids:
        return settings.telegram_allowed_chat_ids[0]
    return None


def _format_results(results) -> str:
    lines = [
        f"{'✓' if r.success else '✗'} {r.project} → {r.target}"
        + (f"\n  {r.detail}" if r.detail else "")
        for r in results
    ]
    return "\n".join(lines) or "Done."


# ---------------------------------------------------------------------------
# Agent registry — source of truth for direct agent commands
# ---------------------------------------------------------------------------


# Each entry: role_key -> {task, display, aliases (ascii-lowercase only)}
# Tasks are sourced from role.command_task so no hardcoded task-name literals live in code.
def _build_agent_registry() -> dict[str, dict[str, Any]]:
    from hivepilot.roles import ROLES

    _aliases_by_role: dict[str, list[str]] = {
        "ceo": ["ceo", "alienor"],
        "chief_of_staff": ["cos", "jules"],
        "cto": ["cto", "blaise"],
        "developer": ["dev", "developer", "gustave"],
        "reviewer": ["review", "reviewer", "victor"],
        "ciso": ["ciso", "hugo"],
        "qa": ["qa", "marie"],
        "documentation": ["docs", "documentation", "theo"],
        "auditor": ["audit", "henri"],
    }

    registry: dict[str, dict[str, Any]] = {}
    for role_key, aliases in _aliases_by_role.items():
        role = ROLES.get(role_key)
        if role is not None:
            title = role.title
            display_name = role.display_name or role.name
            display = f"{display_name} ({title})"
            task = role.command_task  # None for auditor
        else:
            # auditor or unknown — keep entry with no task
            display = role_key.replace("_", " ").title()
            task = None
        registry[role_key] = {
            "task": task,
            "display": display,
            "aliases": aliases,
        }
    return registry


_AGENT_REGISTRY: dict[str, dict[str, Any]] = _build_agent_registry()

# Build reverse lookup: normalised alias -> role_key
_ALIAS_TO_ROLE: dict[str, str] = {}
for _role_key, _entry in _AGENT_REGISTRY.items():
    for _alias in _entry["aliases"]:
        _ALIAS_TO_ROLE[_alias] = _role_key


def _normalise(text: str) -> str:
    """Strip accents and lowercase — used for accent-insensitive alias matching."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _resolve_agent(token: str) -> str | None:
    """Resolve a user-supplied token to a role_key, or None if unknown.

    Accepts: role key, any registered alias (ascii or accented), case-insensitive.
    """
    normalised = _normalise(token)
    if normalised in _ALIAS_TO_ROLE:
        return _ALIAS_TO_ROLE[normalised]
    # Also check role keys directly
    if normalised in _AGENT_REGISTRY:
        return normalised
    return None


def _parse_ask_args(args: list[str], default_target: str) -> tuple[str | None, str, str]:
    """Parse args for /ask: [<agent> [@target] <order...>].

    Returns (role_key_or_None, target, order).
    role_key_or_None is None when the agent token doesn't resolve.
    target defaults to default_target when no @target is given.
    order is the remaining text joined with spaces.
    """
    if not args:
        return (None, default_target, "")

    agent_token = args[0]
    role_key = _resolve_agent(agent_token)
    rest = args[1:]

    # Optional @target
    target = default_target
    if rest and rest[0].startswith("@"):
        target = rest[0][1:]
        rest = rest[1:]

    order = " ".join(rest)
    return (role_key, target, order)


def _parse_alias_args(args: list[str], default_target: str) -> tuple[str, str]:
    """Parse args for a pre-bound alias command: [[@target] <order...>].

    Returns (target, order).
    """
    if not args:
        return (default_target, "")

    target = default_target
    rest = list(args)
    if rest and rest[0].startswith("@"):
        target = rest[0][1:]
        rest = rest[1:]

    order = " ".join(rest)
    return (target, order)


def _parse_mention(
    text: str,
    *,
    groups: dict,
    agents_known: set,
    projects_known: set,
) -> tuple[str, str, str]:
    """Parse a free-text @mention message.

    Returns (kind, name, rest) where kind is one of:
      "none"    — text doesn't start with @
      "group"   — first token matched a group name
      "agent"   — first token resolved via _resolve_agent
      "project" — first token matched a project name
      "unknown" — @ present but token didn't match anything
    """
    import re as _re

    text = text.strip()
    if not text.startswith("@"):
        return ("none", "", "")

    # Extract first token after @: letters/digits/_/- until whitespace
    m = _re.match(r"@([\w\-]+)(.*)", text, _re.DOTALL)
    if not m:
        return ("unknown", "", "")

    raw_token = m.group(1)
    rest = m.group(2).strip()
    normalised = _normalise(raw_token)

    # Resolution priority: group > agent > project > unknown
    norm_groups = {_normalise(g): g for g in groups}
    if normalised in norm_groups:
        return ("group", norm_groups[normalised], rest)

    role_key = _resolve_agent(normalised)
    if role_key is not None:
        return ("agent", role_key, rest)

    norm_projects = {_normalise(p): p for p in projects_known}
    if normalised in norm_projects:
        return ("project", norm_projects[normalised], rest)

    return ("unknown", "", rest)


async def _run_agent_order(update: Any, role_key: str, target: str, order: str) -> None:
    """Shared coroutine: run a single agent task with the 60-s heartbeat pattern."""
    entry = _AGENT_REGISTRY[role_key]
    display = entry["display"]
    task_name = entry["task"]

    # Special case: auditor has no ad-hoc entrypoint
    if task_name is None:
        await update.message.reply_text(
            "Henri (Auditor) runs automatically after each cycle; ad-hoc audit not wired yet."
        )
        return

    ack = await update.message.reply_text(
        f"⏳ Asking {display} on `{target}`…", parse_mode="Markdown"
    )

    loop = asyncio.get_event_loop()
    future = loop.run_in_executor(
        None,
        lambda: _get_orch().run_task(
            project_names=[target],
            task_name=task_name,
            extra_prompt=order or None,
            auto_git=True,
        ),
    )

    heartbeat_interval = 60
    elapsed = 0
    try:
        while True:
            try:
                results = await asyncio.wait_for(asyncio.shield(future), timeout=heartbeat_interval)
                break
            except asyncio.TimeoutError:
                elapsed += heartbeat_interval
                await update.message.reply_text(f"⏳ Still running… ({elapsed}s)")
    except Exception as exc:
        logger.error("telegram.cmd_ask.error", role=role_key, error=str(exc))
        await ack.delete()
        await update.message.reply_text(f"❌ Error: {exc}")
        return

    await ack.delete()
    await update.message.reply_text(_format_results(results))


async def _cmd_ask(update: Any, context: Any) -> None:
    """/ask <agent> [@target] <order...> — address one agent directly."""
    if not _require_allowed(update.effective_chat.id):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /ask <agent> [@target] <order…>\n"
            "Example: /ask gustave @acme-api add unit tests"
        )
        return

    role_key, target, order = _parse_ask_args(args, settings.default_target)

    if role_key is None:
        await update.message.reply_text(
            f"Unknown agent: {args[0]!r}. Use /help to see the list of agents and their aliases."
        )
        return
    if not order:
        await update.message.reply_text(
            f"Please provide an order for {_AGENT_REGISTRY[role_key]['display']}."
        )
        return

    await _run_agent_order(update, role_key, target, order)


async def _cmd_mention(update: Any, context: Any) -> None:
    """Handle free-text @mention messages (non-command)."""
    # Check for pending challenge FIRST
    chat_id = update.message.chat.id
    if chat_id in _pending_challenges and update.message.text:
        run_id, approver = _pending_challenges.pop(chat_id)
        challenge_text = update.message.text
        try:
            cos_response = _get_orch().human_challenge(run_id, challenge_text, approver)
        except Exception as exc:
            logger.error("telegram.challenge.error", run_id=run_id, error=str(exc))
            await update.message.reply_text(f"⚠️ Challenge error for run #{run_id}: {exc}")
            return
        # Show CoS response
        await update.message.reply_text(
            f"🗣 Human → Jules\n{challenge_text}\n\n🛡️ Jules → Human\n{cos_response}"
        )
        # Re-send approval keyboard so user can approve/deny/challenge again
        try:
            from hivepilot.services import state_service as _ss

            row = _ss.get_approval(run_id)
            if row:
                await _send_approval_keyboard_message(
                    context.bot,
                    chat_id=chat_id,
                    run_id=run_id,
                    project=row.get("project", ""),
                    task=row.get("task", ""),
                    details=cos_response[:500] if cos_response else None,
                )
        except Exception as exc:
            logger.warning("telegram.challenge.resend_keyboard_error", error=str(exc))
        return

    if not _require_allowed(update.effective_chat.id):
        return

    text = (update.message.text or "").strip()

    # Load resolution tables
    from hivepilot.services.project_service import load_groups, load_projects

    try:
        grp_file = load_groups()
        proj_file = load_projects()
    except Exception:
        grp_file = type("G", (), {"groups": {}})()
        proj_file = type("P", (), {"projects": {}})()

    groups = grp_file.groups  # dict name -> GroupEntry
    projects_known = set(proj_file.projects.keys())
    agents_known = set(_ALIAS_TO_ROLE.keys())

    kind, name, rest = _parse_mention(
        text,
        groups=groups,
        agents_known=agents_known,
        projects_known=projects_known,
    )

    if kind == "none":
        return  # silently ignore non-@ messages

    if kind == "unknown":
        # Extract token for error message (just the part after @)
        tok = text[1:].split()[0] if text.startswith("@") else text
        await update.message.reply_text(
            f"Unknown @target '{tok}'. Use @<agent> <order> or @acme <request>."
        )
        return

    if kind == "agent":
        # Parse optional @target from rest (same convention as /ask)
        rest_parts = rest.split()
        target = settings.default_target
        order_parts = rest_parts
        if rest_parts and rest_parts[0].startswith("@"):
            target = rest_parts[0][1:]
            order_parts = rest_parts[1:]
        order = " ".join(order_parts)
        if not order:
            await update.message.reply_text(
                f"Please provide an order for {_AGENT_REGISTRY[name]['display']}."
            )
            return
        await _run_agent_order(update, name, target, order)
        return

    # kind == "group" or kind == "project"
    if not rest:
        await update.message.reply_text(f"Please provide a request after @{name}.")
        return

    ack = await update.message.reply_text(
        f"⏳ Launching company pipeline on `{name}`…", parse_mode="Markdown"
    )

    loop = asyncio.get_event_loop()

    if kind == "group":
        grp = groups[name]
        hub = grp.hub or name
        future = loop.run_in_executor(
            None,
            lambda: _get_orch().run_pipeline(
                project_names=[hub],
                pipeline_name=settings.default_pipeline,
                extra_prompt=rest,
                auto_git=True,
                hub=hub,
                components=grp.components,
                dry_run=False,
            ),
        )
    else:  # project
        future = loop.run_in_executor(
            None,
            lambda: _get_orch().run_pipeline(
                project_names=[name],
                pipeline_name=settings.default_pipeline,
                extra_prompt=rest,
                auto_git=True,
            ),
        )

    heartbeat_interval = 60
    elapsed = 0
    try:
        while True:
            try:
                results = await asyncio.wait_for(asyncio.shield(future), timeout=heartbeat_interval)
                break
            except asyncio.TimeoutError:
                elapsed += heartbeat_interval
                await update.message.reply_text(f"⏳ Still running… ({elapsed}s)")
    except Exception as exc:
        logger.error("telegram.cmd_mention.error", kind=kind, name=name, error=str(exc))
        await ack.delete()
        await update.message.reply_text(f"❌ Error: {exc}")
        return

    await ack.delete()
    await update.message.reply_text(_format_results(results))


def _make_alias_handler(role_key: str):
    """Factory: return an async handler pre-bound to role_key."""

    async def _handler(update: Any, context: Any) -> None:
        if not _require_allowed(update.effective_chat.id):
            return
        args = context.args or []
        target, order = _parse_alias_args(args, settings.default_target)
        if not order:
            entry = _AGENT_REGISTRY[role_key]
            await update.message.reply_text(f"Usage: /{entry['aliases'][0]} [@target] <order…>")
            return
        await _run_agent_order(update, role_key, target, order)

    _handler.__name__ = f"_cmd_{role_key}"
    return _handler


# Build alias-handler map: alias -> coroutine function (one per alias, all unique)
_ALIAS_HANDLERS: dict[str, Any] = {}
for _role_key, _entry in _AGENT_REGISTRY.items():
    _h = _make_alias_handler(_role_key)
    for _alias in _entry["aliases"]:
        _ALIAS_HANDLERS[_alias] = _h


def fetch_recent_chats() -> list[dict[str, Any]]:
    """Return unique chats that recently messaged the bot (via getUpdates).

    DM the bot first, then call this to discover your chat id.
    """
    import requests

    url = f"https://api.telegram.org/bot{_token()}/getUpdates"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    seen: dict[int, str] = {}
    for upd in resp.json().get("result", []):
        msg = upd.get("message") or upd.get("edited_message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None or cid in seen:
            continue
        name = (
            chat.get("title")
            or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            or chat.get("username")
            or chat.get("type", "")
        )
        seen[cid] = name
    return [{"id": cid, "name": name} for cid, name in seen.items()]


# ---------------------------------------------------------------------------
# Command handlers  (all async — python-telegram-bot v20+)
# ---------------------------------------------------------------------------


async def _cmd_help(update, context) -> None:
    if not _require_allowed(update.effective_chat.id):
        return
    text = (
        "*HivePilot Bot*\n\n"
        "/run `<project> <task> [instructions]` — trigger a task\n"
        "/diff `<project>` — show last commit changes\n"
        "/rollback `<project>` — revert last commit\n"
        "/approvals — list pending approvals\n"
        "/approve `<run_id>` — approve a run\n"
        "/deny `<run_id>` \\[reason\\] — deny a run\n"
        "/status — last 5 runs\n"
        "/interactions `[limit]` — recent agent interactions\n"
        "/runpipeline `<project> <pipeline> [simulate]` — run a pipeline\n"
        "/debate `<project> <topic>` — CEO dual\\-model debate\n"
        "/steps `<run_id>` — what the agents did in a run\n"
        "/pipelines — list pipelines\n"
        "/projects — list projects\n"
        "/tasks — list tasks\n"
        "/help — this message\n"
        "\n"
        "*Ask an agent directly*\n\n"
        "/ask `<agent> [@target] <order>` — address one agent \\(no full pipeline\\)\n"
        "`@target` overrides the default project/group \\(default: `acme`\\)\n"
        "Orders run with auto\\-git \\(commit/push/PR\\); humans merge\\.\n\n"
        "Agent aliases:\n"
        "\u2022 Aliénor \\(CEO\\) — `/ceo`, `/alienor`\n"
        "\u2022 Jules \\(Chief of Staff\\) — `/cos`, `/jules`\n"
        "\u2022 Blaise \\(CTO\\) — `/cto`, `/blaise`\n"
        "\u2022 Gustave \\(Developer\\) — `/dev`, `/developer`, `/gustave`\n"
        "\u2022 Victor \\(Reviewer\\) — `/review`, `/reviewer`, `/victor`\n"
        "\u2022 Hugo \\(CISO\\) — `/ciso`, `/hugo`\n"
        "\u2022 Marie \\(QA\\) — `/qa`, `/marie`\n"
        "\u2022 Théo \\(Documentation\\) — `/docs`, `/documentation`, `/theo`\n"
        "\u2022 Henri \\(Auditor\\) — `/audit`, `/henri`\n"
        "\n"
        "*Mentions \\(no slash needed\\)*\n\n"
        "@<agent> `<order>` \\— address one agent \\(same as /ask\\)\n"
        "  e\\.g\\\\. `@gustave fix the auth bug`\n"
        "@<group\\/project> `<request>` \\— full company\\-v2 pipeline\n"
        "  e\\.g\\\\. `@acme ship device\\\\-fleet API`\n"
        "⚠️ In groups: BotFather privacy mode must be Disabled \\(/setprivacy\\) to receive plain messages\\."
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def _cmd_run(update, context) -> None:
    if not _require_allowed(update.effective_chat.id):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /run <project> <task> [instructions]")
        return
    project, task = args[0], args[1]
    extra = " ".join(args[2:]) if len(args) > 2 else None

    ack = await update.message.reply_text(
        f"⏳ Triggering `{task}` on `{project}`…", parse_mode="Markdown"
    )

    # Run the task in a thread executor so we can send progress heartbeats
    loop = asyncio.get_event_loop()
    future = loop.run_in_executor(
        None,
        lambda: _get_orch().run_task(
            project_names=[project],
            task_name=task,
            extra_prompt=extra,
            auto_git=True,
        ),
    )

    # Send a heartbeat every 60 s while the task runs
    heartbeat_interval = 60
    elapsed = 0
    try:
        while True:
            try:
                results = await asyncio.wait_for(asyncio.shield(future), timeout=heartbeat_interval)
                break
            except asyncio.TimeoutError:
                elapsed += heartbeat_interval
                await update.message.reply_text(f"⏳ Still running… ({elapsed}s)")
    except Exception as exc:
        logger.error("telegram.cmd_run.error", error=str(exc))
        await ack.delete()
        await update.message.reply_text(f"❌ Error: {exc}")
        return

    await ack.delete()
    await update.message.reply_text(_format_results(results))


async def _cmd_diff(update, context) -> None:
    if not _require_allowed(update.effective_chat.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /diff <project>")
        return
    project_name = args[0]
    try:
        from hivepilot.services.project_service import load_projects

        projects = load_projects()
        project = projects.projects.get(project_name)
        if not project:
            await update.message.reply_text(f"Unknown project: {project_name}")
            return
        result = subprocess.run(
            ["git", "diff", "HEAD~1", "HEAD", "--stat"],
            cwd=str(project.path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = result.stdout.strip() or "(no changes)"
        await update.message.reply_text(
            f"*{project_name}* — last commit:\n```\n{output}\n```", parse_mode="Markdown"
        )
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def _cmd_rollback(update, context) -> None:
    if not _require_allowed(update.effective_chat.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /rollback <project>")
        return
    project_name = args[0]
    try:
        from hivepilot.services.project_service import load_projects

        projects = load_projects()
        project = projects.projects.get(project_name)
        if not project:
            await update.message.reply_text(f"Unknown project: {project_name}")
            return
        # Show what will be reverted first
        log = subprocess.run(
            ["git", "log", "-1", "--oneline"],
            cwd=str(project.path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        commit_line = log.stdout.strip()
        await update.message.reply_text(f"⏳ Reverting: `{commit_line}`", parse_mode="Markdown")
        subprocess.run(
            ["git", "revert", "HEAD", "--no-edit"],
            cwd=str(project.path),
            check=True,
            timeout=30,
        )
        await update.message.reply_text(f"✓ Rolled back `{project_name}`.", parse_mode="Markdown")
    except subprocess.CalledProcessError as exc:
        await update.message.reply_text(f"❌ Rollback failed: {exc}")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def _cmd_approvals(update, context) -> None:
    if not _require_allowed(update.effective_chat.id):
        return
    from hivepilot.services import state_service

    try:
        pending = state_service.get_pending_approvals()
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")
        return
    if not pending:
        await update.message.reply_text("No pending approvals.")
        return
    for row in pending:
        await _send_approval_keyboard_message(
            context.bot,
            chat_id=update.effective_chat.id,
            run_id=row["run_id"],
            project=row["project"],
            task=row["task"],
        )


def _dispatch_approval(run_id: int, approve: bool, approver: str, reason: str | None = None):
    """Route an approve/deny to the right orchestrator entrypoint.

    Pipeline-checkpoint approvals resume the parked pipeline; everything else is a
    single-task approval.
    """
    import json

    from hivepilot.services import state_service

    appr = state_service.get_approval(run_id)
    meta = json.loads(appr.get("metadata") or "{}") if appr else {}
    if meta.get("kind") == "pipeline_checkpoint":
        return _get_orch().resume_pipeline(run_id=run_id, approve=approve, approver=approver)
    return _get_orch().run_approved(
        run_id=run_id, approve=approve, approver=approver, reason=reason
    )


async def _cmd_approve(update, context) -> None:
    if not _require_allowed(update.effective_chat.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /approve <run_id>")
        return
    try:
        run_id = int(args[0])
    except ValueError:
        await update.message.reply_text(f"Invalid run_id: {args[0]!r}")
        return
    await update.message.reply_text(f"⏳ Running approved task #{run_id}…")
    try:
        result = _dispatch_approval(run_id, approve=True, approver="telegram")
        status = "succeeded" if result.success else "failed"
        await update.message.reply_text(f"Run #{run_id} approved — {status}.")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def _cmd_deny(update, context) -> None:
    if not _require_allowed(update.effective_chat.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /deny <run_id> [reason]")
        return
    try:
        run_id = int(args[0])
    except ValueError:
        await update.message.reply_text(f"Invalid run_id: {args[0]!r}")
        return
    reason = " ".join(args[1:]) if len(args) > 1 else "Denied via Telegram"
    try:
        _dispatch_approval(run_id, approve=False, approver="telegram", reason=reason)
        await update.message.reply_text(f"Run #{run_id} denied.")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def _cmd_interactions(update, context) -> None:
    if not _require_allowed(update.effective_chat.id):
        return
    from hivepilot.services import state_service

    limit = 10
    run_id = None
    args = context.args or []
    if args and args[0].isdigit():
        limit = int(args[0])
    try:
        rows = state_service.list_recent_interactions(limit=limit, run_id=run_id)
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")
        return
    if not rows:
        await update.message.reply_text("No interactions logged yet.")
        return
    lines = [
        f"[#{i['run_id'] if i['run_id'] is not None else '-'}] "
        f"{i['actor']} → {i['action']} → {i['target'] or 'all'}: {i['summary']}"
        for i in rows
    ]
    await update.message.reply_text("Recent interactions:\n" + "\n".join(lines))


async def _cmd_status(update, context) -> None:
    if not _require_allowed(update.effective_chat.id):
        return
    from hivepilot.services import state_service

    try:
        runs = state_service.list_recent_runs(limit=5)
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")
        return
    if not runs:
        await update.message.reply_text("No recent runs.")
        return
    lines = [f"[{r['status']}] {r['project']} / {r['task']} — {r['started_at']}" for r in runs]
    await update.message.reply_text("Recent runs:\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Inline keyboard — approval flow
# ---------------------------------------------------------------------------


_TELEGRAM_MAX_MSG = 3500  # Telegram hard limit is ~4096; keep a safety margin


def _truncate_md(text: str, max_len: int = _TELEGRAM_MAX_MSG) -> str:
    """Truncate *text* to *max_len* chars without splitting mid-word."""
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit("\n", 1)[0]
    return cut + "\n…"


async def _send_approval_keyboard_message(
    bot, *, chat_id: int, run_id: int, project: str, task: str, details: str | None = None
) -> None:
    """Send a message with ✅ Approve / ❌ Deny inline buttons."""
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError as exc:
        raise RuntimeError(
            "python-telegram-bot required: pip install hivepilot[notifications]"
        ) from exc

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{run_id}"),
                InlineKeyboardButton("❌ Deny", callback_data=f"deny:{run_id}"),
            ],
            [
                InlineKeyboardButton("🗣 Challenge / Ask", callback_data=f"challenge:{run_id}"),
            ],
        ]
    )
    header = f"*Approval required* — run #{run_id}\nProject: `{project}`\nTask: `{task}`"
    body = f"\n\n{details}" if details else ""
    text = _truncate_md(header + body)
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def _callback_approval(update, context) -> None:
    """Handle ✅ Approve / ❌ Deny button presses."""
    query = update.callback_query
    await query.answer()  # acknowledge immediately to remove the loading indicator

    if not _require_allowed(query.message.chat.id):
        await query.edit_message_text("Unauthorized.")
        return

    data = query.data  # e.g. "approve:42" or "deny:42"
    try:
        action, raw_id = data.split(":", 1)
        run_id = int(raw_id)
    except (ValueError, AttributeError):
        await query.edit_message_text(f"Invalid callback data: {data!r}")
        return

    if action == "challenge":
        approver = query.from_user.username or str(query.from_user.id)
        chat_id = query.message.chat.id
        _pending_challenges[chat_id] = (run_id, f"telegram:{approver}")
        await query.message.reply_text(
            f"✍️ Send your challenge or question for run #{run_id} as your next message"
            " — the Chief of Staff will respond and may revise the plan."
            " The run stays paused."
        )
        return

    approve = action == "approve"
    approver = query.from_user.username or str(query.from_user.id)

    try:
        result = _dispatch_approval(
            run_id,
            approve=approve,
            approver=f"telegram:{approver}",
            reason=None if approve else "Denied via Telegram button",
        )
        if approve:
            outcome = "succeeded" if result.success else "failed"
            icon = "✅" if result.success else "❌"
            text = f"{icon} Run #{run_id} approved by @{approver} — {outcome}."
        else:
            text = f"❌ Run #{run_id} denied by @{approver}."
        await query.edit_message_text(text)
    except Exception as exc:
        logger.error("telegram.callback_approval.error", run_id=run_id, error=str(exc))
        await query.edit_message_text(f"Error processing run #{run_id}: {exc}")


def notify_approval_required(
    *, run_id: int, project: str, task: str, details: str | None = None
) -> None:
    """
    Send an approval keyboard to the notification chat (sync, fire-and-forget).
    Called from notification_service — safe to call from non-async context.
    """
    chat_id = _notification_chat_id()
    if not chat_id:
        raise RuntimeError("No Telegram notification chat_id configured")

    token = _token()

    async def _send():
        from telegram import Bot

        async with Bot(token) as bot:
            await _send_approval_keyboard_message(
                bot, chat_id=chat_id, run_id=run_id, project=project, task=task, details=details
            )

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Inside an existing event loop (FastAPI / webhook mode) — schedule as a task
            loop.create_task(_send())
        else:
            loop.run_until_complete(_send())
    except RuntimeError:
        asyncio.run(_send())


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


async def _await_with_heartbeat(update, future, interval: int = 60):
    elapsed = 0
    while True:
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=interval)
        except asyncio.TimeoutError:
            elapsed += interval
            await update.message.reply_text(f"\u23f3 Still running\u2026 ({elapsed}s)")


async def _cmd_pipelines(update, context) -> None:
    if not _require_allowed(update.effective_chat.id):
        return
    pipes = _get_orch().pipelines.pipelines
    if not pipes:
        await update.message.reply_text("No pipelines configured.")
        return
    lines = [f"\u2022 {name}: {(p.description or '').strip()[:80]}" for name, p in pipes.items()]
    await update.message.reply_text("Pipelines:\n" + "\n".join(lines))


async def _cmd_projects(update, context) -> None:
    if not _require_allowed(update.effective_chat.id):
        return
    projs = _get_orch().projects.projects
    if not projs:
        await update.message.reply_text("No projects configured.")
        return
    await update.message.reply_text("Projects:\n" + "\n".join(f"\u2022 {n}" for n in projs))


async def _cmd_tasks(update, context) -> None:
    if not _require_allowed(update.effective_chat.id):
        return
    tasks = _get_orch().tasks.tasks
    if not tasks:
        await update.message.reply_text("No tasks configured.")
        return
    await update.message.reply_text("Tasks:\n" + "\n".join(f"\u2022 {n}" for n in tasks))


async def _cmd_run_pipeline(update, context) -> None:
    if not _require_allowed(update.effective_chat.id):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /runpipeline <project> <pipeline> [simulate]")
        return
    project, pipeline = args[0], args[1]
    simulate = "simulate" in args[2:]
    suffix = " (simulate)" if simulate else ""
    await update.message.reply_text(
        f"\u23f3 Pipeline `{pipeline}` on `{project}`{suffix}\u2026", parse_mode="Markdown"
    )
    loop = asyncio.get_event_loop()
    future = loop.run_in_executor(
        None,
        lambda: _get_orch().run_pipeline(
            project_names=[project],
            pipeline_name=pipeline,
            extra_prompt=None,
            auto_git=False,
            dry_run=True,
            simulate=simulate,
        ),
    )
    try:
        results = await _await_with_heartbeat(update, future)
    except Exception as exc:
        logger.error("telegram.cmd_run_pipeline.error", error=str(exc))
        await update.message.reply_text(f"\u274c Error: {exc}")
        return
    await update.message.reply_text(_format_results(results))


async def _cmd_debate(update, context) -> None:
    if not _require_allowed(update.effective_chat.id):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /debate <project> <topic...>")
        return
    project = args[0]
    topic = " ".join(args[1:])
    await update.message.reply_text(
        f"\u23f3 CEO debate on `{project}`\u2026", parse_mode="Markdown"
    )
    loop = asyncio.get_event_loop()
    future = loop.run_in_executor(
        None,
        lambda: _get_orch().run_debate(
            project_name=project, role_name="ceo", topic=topic, dry_run=True
        ),
    )
    try:
        adr = await _await_with_heartbeat(update, future)
    except Exception as exc:
        logger.error("telegram.cmd_debate.error", error=str(exc))
        await update.message.reply_text(f"\u274c Error: {exc}")
        return
    if adr is None:
        await update.message.reply_text("Debate complete \u2014 no vault configured.")
    else:
        prefix = "(dry-run) " if adr.get("dry_run") else ""
        await update.message.reply_text(f"\u2705 Debate ADR {prefix}\u2192 {adr.get('path')}")


async def _cmd_steps(update, context) -> None:
    if not _require_allowed(update.effective_chat.id):
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /steps <run_id>")
        return
    from hivepilot.services import state_service

    steps = state_service.get_steps_for_run(int(args[0]))
    if not steps:
        await update.message.reply_text(f"No steps for run {args[0]}.")
        return
    lines = []
    for s in steps:
        line = f"[{s['status']}] {s['step']} \u2014 {s.get('timestamp', '')}"
        if s.get("detail"):
            line += f"\n  {str(s['detail'])[:120]}"
        lines.append(line)
    await update.message.reply_text(f"Run {args[0]} steps:\n" + "\n".join(lines))


def _build_application(token: str):
    try:
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler
    except ImportError as exc:
        raise RuntimeError(
            "python-telegram-bot is required: pip install hivepilot[notifications]"
        ) from exc

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", _cmd_help))
    app.add_handler(CommandHandler("help", _cmd_help))
    app.add_handler(CommandHandler("run", _cmd_run))
    app.add_handler(CommandHandler("diff", _cmd_diff))
    app.add_handler(CommandHandler("rollback", _cmd_rollback))
    app.add_handler(CommandHandler("approvals", _cmd_approvals))
    app.add_handler(CommandHandler("approve", _cmd_approve))
    app.add_handler(CommandHandler("deny", _cmd_deny))
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CommandHandler("interactions", _cmd_interactions))
    app.add_handler(CommandHandler("pipelines", _cmd_pipelines))
    app.add_handler(CommandHandler("projects", _cmd_projects))
    app.add_handler(CommandHandler("tasks", _cmd_tasks))
    app.add_handler(CommandHandler("runpipeline", _cmd_run_pipeline))
    app.add_handler(CommandHandler("debate", _cmd_debate))
    app.add_handler(CommandHandler("steps", _cmd_steps))
    app.add_handler(CommandHandler("ask", _cmd_ask))
    app.add_handler(CommandHandler("ceo", _ALIAS_HANDLERS["ceo"]))
    app.add_handler(CommandHandler("alienor", _ALIAS_HANDLERS["alienor"]))
    app.add_handler(CommandHandler("cos", _ALIAS_HANDLERS["cos"]))
    app.add_handler(CommandHandler("jules", _ALIAS_HANDLERS["jules"]))
    app.add_handler(CommandHandler("cto", _ALIAS_HANDLERS["cto"]))
    app.add_handler(CommandHandler("blaise", _ALIAS_HANDLERS["blaise"]))
    app.add_handler(CommandHandler("dev", _ALIAS_HANDLERS["dev"]))
    app.add_handler(CommandHandler("developer", _ALIAS_HANDLERS["developer"]))
    app.add_handler(CommandHandler("gustave", _ALIAS_HANDLERS["gustave"]))
    app.add_handler(CommandHandler("review", _ALIAS_HANDLERS["review"]))
    app.add_handler(CommandHandler("reviewer", _ALIAS_HANDLERS["reviewer"]))
    app.add_handler(CommandHandler("victor", _ALIAS_HANDLERS["victor"]))
    app.add_handler(CommandHandler("ciso", _ALIAS_HANDLERS["ciso"]))
    app.add_handler(CommandHandler("hugo", _ALIAS_HANDLERS["hugo"]))
    app.add_handler(CommandHandler("qa", _ALIAS_HANDLERS["qa"]))
    app.add_handler(CommandHandler("marie", _ALIAS_HANDLERS["marie"]))
    app.add_handler(CommandHandler("docs", _ALIAS_HANDLERS["docs"]))
    app.add_handler(CommandHandler("documentation", _ALIAS_HANDLERS["documentation"]))
    app.add_handler(CommandHandler("theo", _ALIAS_HANDLERS["theo"]))
    app.add_handler(CommandHandler("audit", _ALIAS_HANDLERS["audit"]))
    app.add_handler(CommandHandler("henri", _ALIAS_HANDLERS["henri"]))
    from telegram.ext import MessageHandler, filters

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _cmd_mention))
    app.add_handler(
        CallbackQueryHandler(_callback_approval, pattern=r"^(approve|deny|challenge):\d+$")
    )
    return app


# ---------------------------------------------------------------------------
# Polling mode  (RPI / NAT — no public URL needed)
# ---------------------------------------------------------------------------


def _quiet_http_logging() -> None:
    """Silence libraries that log full request URLs (which embed the bot token)."""
    import logging

    for _name in ("httpx", "httpcore", "telegram", "telegram.ext", "telegram.bot"):
        logging.getLogger(_name).setLevel(logging.WARNING)


def run_polling() -> None:
    """Start the bot in long-polling mode. Blocking. No public URL required."""
    _quiet_http_logging()
    token = _token()
    logger.info("telegram.polling.start")
    app = _build_application(token)
    app.run_polling(drop_pending_updates=True)


# ---------------------------------------------------------------------------
# Webhook mode — built-in server  (VPS with public HTTPS)
# ---------------------------------------------------------------------------


def run_webhook(
    webhook_url: str,
    port: int | None = None,
    secret: str | None = None,
) -> None:
    """
    Start the bot with python-telegram-bot's built-in webhook server.
    Blocking. Requires a public HTTPS URL (direct or via reverse proxy).

    webhook_url : public base URL, e.g. https://myserver.com
    port        : local port to listen on (default: settings.telegram_webhook_port)
    secret      : X-Telegram-Bot-Api-Secret-Token (recommended)
    """
    _quiet_http_logging()
    token = _token()
    effective_port = port or settings.telegram_webhook_port
    effective_secret = secret or settings.telegram_webhook_secret
    url_path = token.split(":")[1]
    full_url = f"{webhook_url.rstrip('/')}/{url_path}"

    logger.info("telegram.webhook.start", base=webhook_url, port=effective_port)
    app = _build_application(token)
    app.run_webhook(
        listen="0.0.0.0",
        port=effective_port,
        secret_token=effective_secret,
        url_path=url_path,
        webhook_url=full_url,
        drop_pending_updates=True,
    )


# ---------------------------------------------------------------------------
# Webhook mode — FastAPI-integrated  (share port with the API server)
# ---------------------------------------------------------------------------


async def _get_or_init_app():
    """Lazily initialise the Application for use inside an existing event loop."""
    global _app_instance
    if _app_instance is None:
        token = _token()
        _app_instance = _build_application(token)
        await _app_instance.initialize()
        await _app_instance.start()
    return _app_instance


async def process_update(data: dict[str, Any]) -> None:
    """
    Process a raw Telegram update dict from the FastAPI webhook endpoint.
    The Application is lazily started on first call.
    """
    from telegram import Update

    app = await _get_or_init_app()
    update = Update.de_json(data, app.bot)
    await app.process_update(update)


async def shutdown() -> None:
    """Gracefully stop the lazily-started Application (call on FastAPI shutdown)."""
    global _app_instance
    if _app_instance is not None:
        await _app_instance.stop()
        await _app_instance.shutdown()
        _app_instance = None


# ---------------------------------------------------------------------------
# Webhook registration helpers  (one-shot, non-blocking)
# ---------------------------------------------------------------------------


def set_webhook(webhook_url: str, secret: str | None = None) -> str:
    """Register the webhook URL with Telegram. Returns the registered URL."""
    token = _token()
    effective_secret = secret or settings.telegram_webhook_secret
    url_path = token.split(":")[1]
    full_url = f"{webhook_url.rstrip('/')}/{url_path}"

    async def _set():
        from telegram import Bot

        async with Bot(token) as bot:
            await bot.set_webhook(
                url=full_url,
                secret_token=effective_secret,
                drop_pending_updates=True,
            )
            info = await bot.get_webhook_info()
            return info.url

    registered_url = asyncio.run(_set())
    logger.info("telegram.webhook.registered", url=registered_url)
    return registered_url


def delete_webhook() -> None:
    """Unregister the webhook from Telegram (switches back to polling)."""
    token = _token()

    async def _delete():
        from telegram import Bot

        async with Bot(token) as bot:
            await bot.delete_webhook(drop_pending_updates=True)

    asyncio.run(_delete())
    logger.info("telegram.webhook.deleted")


def get_webhook_info() -> dict[str, Any]:
    """Return current webhook info from Telegram."""
    token = _token()

    async def _info():
        from telegram import Bot

        async with Bot(token) as bot:
            info = await bot.get_webhook_info()
            return {
                "url": info.url,
                "has_custom_certificate": info.has_custom_certificate,
                "pending_update_count": info.pending_update_count,
                "last_error_message": info.last_error_message,
                "max_connections": info.max_connections,
            }

    return asyncio.run(_info())
