from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Any

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Lazily-initialised Application instance (used by webhook/FastAPI mode)
_app_instance = None


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


def _format_results(results) -> str:
    lines = [
        f"{'✓' if r.success else '✗'} {r.project} → {r.target}"
        + (f"\n  {r.detail}" if r.detail else "")
        for r in results
    ]
    return "\n".join(lines) or "Done."


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
        "/help — this message\n"
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
        await update.message.reply_text(f"*{project_name}* — last commit:\n```\n{output}\n```", parse_mode="Markdown")
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
        await update.message.reply_text(
            f"⏳ Reverting: `{commit_line}`", parse_mode="Markdown"
        )
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
    lines = [
        f"#{row['run_id']} {row['project']} / {row['task']} @ {row['requested_at']}"
        for row in pending
    ]
    await update.message.reply_text("Pending approvals:\n" + "\n".join(lines))


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
        result = _get_orch().run_approved(run_id=run_id, approve=True, approver="telegram")
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
        _get_orch().run_approved(run_id=run_id, approve=False, approver="telegram", reason=reason)
        await update.message.reply_text(f"Run #{run_id} denied.")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


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
    lines = [
        f"[{r['status']}] {r['project']} / {r['task']} — {r['started_at']}"
        for r in runs
    ]
    await update.message.reply_text("Recent runs:\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def _build_application(token: str):
    try:
        from telegram.ext import Application, CommandHandler
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
    return app


# ---------------------------------------------------------------------------
# Polling mode  (RPI / NAT — no public URL needed)
# ---------------------------------------------------------------------------

def run_polling() -> None:
    """Start the bot in long-polling mode. Blocking. No public URL required."""
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
    token = _token()
    effective_port = port or settings.telegram_webhook_port
    effective_secret = secret or settings.telegram_webhook_secret
    url_path = token.split(":")[1]
    full_url = f"{webhook_url.rstrip('/')}/{url_path}"

    logger.info("telegram.webhook.start", url=full_url, port=effective_port)
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
