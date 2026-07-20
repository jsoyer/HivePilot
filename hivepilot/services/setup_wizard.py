"""hivepilot.services.setup_wizard -- the `hivepilot setup` guided wizard.

A single, friendly, idempotent walk through everything a fresh HivePilot
install needs: an optional private config repo, the first (admin) API
token, agent runner CLIs, Telegram (bot token + auto-detected chat ids),
curated example plugins, the opt-in NL concierge, and OS service scaffolding.

This module owns orchestration (`run_setup`) plus the steps that need to
share module-global state with their own internal helpers for testability
(config repo + `_run_config_sync`, concierge, telegram + its polling
helpers) -- the remaining, more self-contained steps live in
`setup_wizard_extra.py` and are re-exported here (see that module's
docstring for why). Shared primitives (`SetupOptions`, `.env` upsert,
secret masking, `STEP_NAMES`) live in the leaf module
`setup_wizard_common.py` so both can import them without a cycle.

Design notes:

- **Idempotent & resumable.** Every step reads current state first and shows
  a ✓/○ status line before doing anything; nothing is silently clobbered.
- **Safe.** Secrets are masked in every echo. The one deliberate exception is
  the freshly-minted admin token (`step_admin_token`, in `setup_wizard_extra`),
  shown once, boxed, with an explicit "won't be shown again" warning -- the
  same UX `hivepilot tokens add` already uses.
- **Never hangs.** `interactive` is threaded through every step; when False
  (headless/non-interactive), no step ever calls `typer.prompt`/`typer.confirm`.
  A step that has no way to do anything useful without interactive input
  simply skips itself -- EXCEPT when the operator explicitly asked for just
  that section via `--only <step>` in `--non-interactive` mode, in which case
  a missing required value is a clear, actionable error (`_RequiredValueMissing`)
  rather than a silent no-op.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import requests
import typer

from hivepilot.config import settings
from hivepilot.services import (
    token_service,  # noqa: F401 -- re-exported for `setup_wizard.token_service` (test monkeypatch target; step_admin_token itself lives in setup_wizard_extra)
)
from hivepilot.services.setup_wizard_common import (
    STEP_NAMES,
    SetupOptions,
    _default_env_path,
    _env_get,
    _env_upsert,
    _mask_secret,
    _RequiredValueMissing,
    _section_header,
    _status_line,
    _TelegramPollError,
)
from hivepilot.services.setup_wizard_extra import (
    step_admin_token,
    step_plugins,
    step_runners,
    step_services,
    step_summary,
    step_welcome,
)
from hivepilot.utils.logging import get_logger

if TYPE_CHECKING:
    from rich.console import Console
    from rich.table import Table

__all__ = [
    "STEP_NAMES",
    "SetupOptions",
    "run_setup",
    "step_welcome",
    "step_config_repo",
    "step_admin_token",
    "step_runners",
    "step_telegram",
    "step_plugins",
    "step_concierge",
    "step_services",
    "step_summary",
]

logger = get_logger(__name__)

_POLL_INTERVAL_SECONDS = 1


# ---------------------------------------------------------------------------
# Step 2: config repo (optional)
# ---------------------------------------------------------------------------


def _run_config_sync(console: "Console") -> None:
    """Shell out to `hivepilot config sync` (a fresh process re-reads the
    `.env` we just wrote -- calling `config_service.sync()` in-process would
    see the stale, already-constructed `Settings` singleton instead).
    Surfaces failure without aborting the wizard."""
    binary = shutil.which("hivepilot")
    cmd = (
        [binary, "config", "sync"]
        if binary
        else [sys.executable, "-c", "from hivepilot.cli import app; app()", "config", "sync"]
    )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001 -- never abort the wizard
        console.print(f"  [yellow]config sync could not be run: {exc}[/yellow]")
        return
    if result.returncode == 0:
        console.print("  [green]✓ config sync OK[/green]")
    else:
        console.print(f"  [yellow]config sync failed (exit {result.returncode})[/yellow]")
        detail = (result.stderr or result.stdout or "").strip()
        if detail:
            console.print(f"  {detail.splitlines()[-1]}")


def step_config_repo(
    console: "Console",
    options: SetupOptions,
    interactive: bool,
    env_path: Path,
    *,
    only_requested: bool,
) -> str:
    _section_header(console, "Config repo")
    current = getattr(settings, "config_repo", None)
    _status_line(console, bool(current), f"config_repo: {current or 'not configured'}")

    config_repo = options.config_repo
    if config_repo is None and interactive:
        use_repo = typer.confirm(
            "Do you use a private HivePilot config repo?", default=bool(current)
        )
        if use_repo:
            config_repo = typer.prompt("Config repo git URL or local path", default=current or "")

    if not config_repo:
        if only_requested and options.non_interactive:
            raise _RequiredValueMissing(
                "--config-repo is required for --only config in --non-interactive mode"
            )
        console.print("  [dim]skipped[/dim] (no config repo)")
        return "skipped"

    scheme = (
        "ssh"
        if config_repo.startswith("git@") or config_repo.startswith("ssh://")
        else ("https" if config_repo.startswith("https://") else "local")
    )
    console.print(f"  detected scheme: {scheme}")

    token = options.config_token
    if scheme == "https":
        if token is None and interactive:
            console.print(
                "  [dim]HTTPS repo: set a fine-grained token (Contents:read) or use an "
                "SSH deploy key instead -- SSH needs no token stored here but requires "
                "host key/agent setup.[/dim]"
            )
            if typer.confirm("Set HIVEPILOT_CONFIG_TOKEN now?", default=False):
                token = typer.prompt("Config token", hide_input=True)
        if token:
            console.print(f"  will write HIVEPILOT_CONFIG_TOKEN={_mask_secret(token)}")
    elif scheme == "ssh":
        console.print(
            "  [dim]SSH URL -- ensure a deploy key is loaded in this host's ssh-agent/"
            "known_hosts.[/dim]"
        )

    do_write = (
        options.assume_yes
        or options.non_interactive
        or (
            interactive
            and typer.confirm(f"Write HIVEPILOT_CONFIG_REPO to {env_path}?", default=True)
        )
    )
    if not do_write:
        console.print("  [dim]not written[/dim]")
        return "skipped"

    # LOW-2: a non-interactive re-run must not silently clobber a secret
    # already written -- require an explicit --force to replace it. The
    # interactive path already shows the current value + a confirm prompt,
    # so this guard only applies headlessly.
    if (
        options.non_interactive
        and not options.force
        and _env_get(env_path, "HIVEPILOT_CONFIG_REPO") is not None
    ):
        console.print(
            "  [dim]HIVEPILOT_CONFIG_REPO already set -- pass --force to replace it.[/dim]"
        )
        return "skipped"

    _env_upsert(env_path, "HIVEPILOT_CONFIG_REPO", config_repo)
    if token:
        _env_upsert(env_path, "HIVEPILOT_CONFIG_TOKEN", token)

    run_sync = options.assume_yes or (
        interactive and typer.confirm("Run `hivepilot config sync` now?", default=True)
    )
    if run_sync:
        _run_config_sync(console)

    return "configured"


# ---------------------------------------------------------------------------
# Step 5: Telegram -- bot token + chat-id auto-detection
# ---------------------------------------------------------------------------


def _parse_chats(payload: dict) -> list[dict]:
    """Distinct chats from a `getUpdates` JSON body's `result[]`."""
    chats: dict[int, dict] = {}
    for update in payload.get("result", []):
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or {}
        )
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        title = chat.get("title") or chat.get("username") or chat.get("first_name") or str(chat_id)
        chats[chat_id] = {"id": chat_id, "type": chat.get("type", "unknown"), "title": title}
    return list(chats.values())


def _telegram_get_updates(token: str, timeout: int = 5) -> list[dict]:
    """One `getUpdates` call. Raises `_TelegramPollError` (never a raw
    `requests`/JSON exception) on a 409 conflict or a webhook-set error."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    resp = requests.get(url, params={"timeout": timeout}, timeout=timeout + 5)

    if resp.status_code == 409:
        raise _TelegramPollError(
            "Telegram returned 409 Conflict -- another instance (e.g. the running bot "
            "service) is already polling this token. Stop it first, then re-run."
        )

    payload = resp.json() if resp.content else {}
    if not payload.get("ok", True):
        description = str(payload.get("description", ""))
        if "webhook" in description.lower():
            raise _TelegramPollError(
                "This token has a webhook set, so getUpdates can't poll it. Call "
                "https://api.telegram.org/bot<TOKEN>/deleteWebhook first, then re-run."
            )
        raise _TelegramPollError(f"Telegram API error: {description or resp.status_code}")

    # NOTE: deliberately no `resp.raise_for_status()` here -- the `ok`
    # contract check above already covers Telegram's own error signaling,
    # and `HTTPError`'s message embeds the full request URL (bot token
    # included) verbatim. See MED-1.
    return _parse_chats(payload)


def _poll_telegram_chats(console: "Console", token: str, total_timeout: int) -> list[dict]:
    """Poll `getUpdates` for up to *total_timeout* seconds, returning as soon
    as any chats are seen. Never raises -- Telegram API errors are printed
    and end the poll early; a plain network hiccup is retried until the
    deadline."""
    console.print(
        "  \U0001f449 Now send any message to your bot in the chats you want to authorize "
        "(DM and/or your group)..."
    )
    deadline = time.monotonic() + max(total_timeout, 0)
    poll_window = min(5, max(total_timeout, 1))
    seen: dict[int, dict] = {}

    while True:
        try:
            for chat in _telegram_get_updates(token, timeout=poll_window):
                seen[chat["id"]] = chat
        except _TelegramPollError as exc:
            console.print(f"  [yellow]{exc}[/yellow]")
            return list(seen.values())
        except Exception as exc:  # noqa: BLE001 -- transient network hiccup, keep polling
            # MED-1: never interpolate str(exc) here -- `requests`
            # exceptions (HTTPError, ConnectionError, ...) commonly embed
            # the full request URL, bot token included, in their message.
            # The exception TYPE is enough to tell the operator something
            # is retrying.
            console.print(f"  [dim]getUpdates error ({type(exc).__name__}); retrying...[/dim]")

        if seen:
            return list(seen.values())
        if time.monotonic() >= deadline:
            console.print("  [dim]No chats detected in time.[/dim]")
            return []
        time.sleep(min(_POLL_INTERVAL_SECONDS, max(total_timeout, 0)))


def _select_indices(raw: str, count: int) -> list[int]:
    """Parse a comma-separated "1,3" style pick list into valid 1-based
    indices within `[1, count]`, silently dropping anything out of range or
    non-numeric."""
    picks: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token.isdigit():
            continue
        value = int(token)
        if 1 <= value <= count:
            picks.append(value)
    return picks


def _chat_table(chats: list[dict]) -> "Table":
    """Build the chat-picker table. MED-2: `title`/`type` come straight from
    Telegram (an attacker who DMs the bot fully controls their own chat
    title/username) -- `rich.markup.escape()` them so a value like
    `[red]x[/]` renders as literal text instead of being interpreted as
    markup (styling injection) or, for a malformed tag like `[bad`, raising
    `rich.errors.MarkupError` and crashing the wizard mid-poll."""
    from rich.markup import escape
    from rich.table import Table

    table = Table(box=None)
    for col in ("#", "id", "type", "title"):
        table.add_column(col)
    for i, chat in enumerate(chats, start=1):
        table.add_row(
            str(i),
            str(chat["id"]),
            escape(str(chat.get("type", ""))),
            escape(str(chat.get("title", ""))),
        )
    return table


def step_telegram(
    console: "Console",
    options: SetupOptions,
    interactive: bool,
    env_path: Path,
    *,
    only_requested: bool,
) -> str:
    _section_header(console, "Telegram")
    current_token = getattr(settings, "telegram_bot_token", None)
    _status_line(
        console,
        bool(current_token),
        f"bot token: {_mask_secret(current_token) if current_token else 'not configured'}",
    )

    token = options.telegram_bot_token
    if token is None and interactive:
        if typer.confirm("Configure Telegram now?", default=bool(current_token)):
            token = typer.prompt("Telegram bot token", hide_input=True) or None

    if not token:
        if only_requested and options.non_interactive:
            raise _RequiredValueMissing(
                "--telegram-bot-token is required for --only telegram in --non-interactive mode"
            )
        console.print("  [dim]skipped[/dim] (no bot token)")
        return "skipped"

    console.print(f"  will write HIVEPILOT_TELEGRAM_BOT_TOKEN={_mask_secret(token)}")

    chats: list[dict] = []
    allowed_ids: list[int] = []
    if options.telegram_allowed_chat_ids:
        allowed_ids = [
            int(x.strip()) for x in options.telegram_allowed_chat_ids.split(",") if x.strip()
        ]
    elif interactive:
        chats = _poll_telegram_chats(console, token, options.timeout)
        if chats:
            console.print(_chat_table(chats))
            raw = typer.prompt(
                "Authorize which chats? (comma-separated #, blank = all)", default=""
            )
            picks = _select_indices(raw, len(chats))
            allowed_ids = [chats[i - 1]["id"] for i in picks] if picks else [c["id"] for c in chats]
        else:
            manual = typer.prompt(
                "No chats detected -- enter chat ids to authorize (comma-separated, blank to skip)",
                default="",
            )
            allowed_ids = [int(x.strip()) for x in manual.split(",") if x.strip()]

    notification_id = options.telegram_notification_chat_id
    stream_id = options.telegram_stream_chat_id
    if interactive and chats:
        privates = [c for c in chats if c["type"] == "private"]
        groups = [c for c in chats if c["type"] != "private"]
        default_notif = (privates[0] if privates else chats[0])["id"]
        default_stream = (groups[0] if groups else chats[0])["id"]
        if notification_id is None:
            notification_id = int(typer.prompt("Notification chat id", default=str(default_notif)))
        if stream_id is None:
            stream_id = int(typer.prompt("Live agent-stream chat id", default=str(default_stream)))

    do_write = (
        options.assume_yes
        or options.non_interactive
        or (interactive and typer.confirm(f"Write Telegram settings to {env_path}?", default=True))
    )
    if not do_write:
        console.print("  [dim]not written[/dim]")
        return "skipped"

    # LOW-2: same don't-clobber-non-interactively guard as step_config_repo.
    if (
        options.non_interactive
        and not options.force
        and _env_get(env_path, "HIVEPILOT_TELEGRAM_BOT_TOKEN") is not None
    ):
        console.print(
            "  [dim]HIVEPILOT_TELEGRAM_BOT_TOKEN already set -- pass --force to replace it.[/dim]"
        )
        return "skipped"

    _env_upsert(env_path, "HIVEPILOT_TELEGRAM_BOT_TOKEN", token)
    if allowed_ids:
        _env_upsert(
            env_path, "HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS", ",".join(str(i) for i in allowed_ids)
        )
    if notification_id is not None:
        _env_upsert(env_path, "HIVEPILOT_TELEGRAM_NOTIFICATION_CHAT_ID", str(notification_id))
    if stream_id is not None:
        _env_upsert(env_path, "HIVEPILOT_TELEGRAM_STREAM_CHAT_ID", str(stream_id))
    return "configured"


# ---------------------------------------------------------------------------
# Step 7: concierge (opt-in NL chatops) -- capability-gated
# ---------------------------------------------------------------------------


def step_concierge(
    console: "Console",
    options: SetupOptions,
    interactive: bool,
    env_path: Path,
    *,
    only_requested: bool,
) -> str:
    _section_header(console, "Concierge (natural-language chatops)")

    if not hasattr(settings, "chatops_concierge_enabled"):
        console.print(
            "  [dim]available after upgrade -- this install doesn't have the concierge "
            "feature yet.[/dim]"
        )
        return "unavailable"

    console.print(
        "  Talk to the bot like a friend -- it routes your message to the right agent, "
        "confirms destructive actions, and defaults to the CEO role."
    )
    current = bool(getattr(settings, "chatops_concierge_enabled", False))
    _status_line(console, current, "concierge enabled" if current else "concierge disabled")

    enable = options.concierge
    if not enable and interactive:
        enable = bool(typer.confirm("Enable the NL concierge?", default=current))

    if not enable:
        console.print("  [dim]skipped[/dim]")
        return "skipped"

    _env_upsert(env_path, "HIVEPILOT_CHATOPS_CONCIERGE_ENABLED", "true")
    _env_upsert(env_path, "HIVEPILOT_CHATOPS_DEFAULT_ROLE", "ceo")
    return "enabled"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_setup(console: "Console", options: SetupOptions) -> int:
    """Run the wizard (or, with `options.only` set, a single section).
    Returns a process exit code: 0 on success, 1 if a required value was
    missing in `--only <step> --non-interactive` mode or `only` names an
    unknown step."""
    from hivepilot.banner import render_banner

    interactive = (not options.non_interactive) and sys.stdin.isatty()
    env_path = options.env_path if options.env_path is not None else _default_env_path()

    render_banner(console, subtitle="Guided setup")
    step_welcome(console, env_path)

    if options.only is not None and options.only not in STEP_NAMES:
        console.print(
            f"[red]setup: unknown --only step {options.only!r}. "
            f"Choose from: {', '.join(STEP_NAMES)}[/red]"
        )
        return 1

    run_all = options.only is None
    results: dict[str, str] = {}

    try:
        if run_all or options.only == "config":
            results["config"] = step_config_repo(
                console, options, interactive, env_path, only_requested=options.only == "config"
            )
        if run_all or options.only == "token":
            results["token"] = step_admin_token(console, options, interactive)
        if run_all or options.only == "runners":
            results["runners"] = step_runners(console)
        if run_all or options.only == "telegram":
            results["telegram"] = step_telegram(
                console, options, interactive, env_path, only_requested=options.only == "telegram"
            )
        if run_all or options.only == "plugins":
            results["plugins"] = step_plugins(
                console, options, interactive, env_path, only_requested=options.only == "plugins"
            )
        if run_all or options.only == "concierge":
            results["concierge"] = step_concierge(
                console,
                options,
                interactive,
                env_path,
                only_requested=options.only == "concierge",
            )
        if run_all or options.only == "services":
            results["services"] = step_services(console, interactive, options)
    except _RequiredValueMissing as exc:
        console.print(f"[red]setup: {exc}[/red]")
        return 1

    step_summary(console, results)
    return 0
