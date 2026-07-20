"""hivepilot/services/signal_bot.py — dual-mode Signal bot (Phase 23e), at
parity with the Telegram/Slack/Discord bots.

Signal reality (this grounds the whole design): there is NO Signal cloud bot
API. A bot is a dedicated phone NUMBER registered via the `signal-cli` binary
(https://github.com/AsamK/signal-cli) or its HTTP wrapper,
`signal-cli-rest-api` (https://github.com/bbernhard/signal-cli-rest-api).
Signal is end-to-end encrypted, peer-to-peer messaging — there is NO inbound
webhook mode (unlike Telegram/Slack/Discord, all of which offer a push
delivery mechanism). Messages are only ever RECEIVED by polling:

  - ``cli`` mode:  ``signal-cli -a <number> --output=json receive`` (a fresh
    subprocess per poll tick — no long-lived daemon connection needed).
  - ``rest`` mode: ``GET {signal_rest_url}/v1/receive/{number}`` against a
    running ``signal-cli-rest-api`` container.

Sending mirrors that split: ``signal-cli ... send -m <msg> <recipient>``
(cli) or ``POST {signal_rest_url}/v2/send`` (rest).

``signal-cli`` is an OPTIONAL external dependency (PATH-gated), mirroring
``plugins/rtk.py``/``plugins/hugo.py``: a missing binary degrades gracefully
(the receive loop logs a warning and skips the tick; ``notify``/
``notify_approval_required`` raise a clear ``RuntimeError`` that
``notification_service.send_approval_keyboard`` already catches and no-ops
on, same as the Slack/Discord branches) — it never crashes a HivePilot run.

Command dispatch reuses ``chatops_service.handle_signal`` (added alongside
this module) — no orchestrator/state_service logic is duplicated here,
unlike slack_bot.py/discord_bot.py (which pre-date this shared dispatcher
and reimplement it for their SDK-specific button/block-kit UX). Signal is
text-only (no inline buttons), so there is nothing SDK-specific to add on
top of chatops_service's plain-text replies.

Auth: ``signal_allowed_numbers`` is an E.164 whitelist, open when empty
(same policy shape as slack_allowed_channel_ids / discord_allowed_*_ids).

Secrets never touch argv: signal-cli manages its registration credentials
(identity keys, session state) in its own on-disk data directory,
established out-of-band via ``hivepilot signal register``/``hivepilot
signal link`` — our subprocess argv only ever carries the bot number, a
recipient, and the message text, never a token/password/credential. (The
one exception — a signal-cli `register --captcha <token>` value — is a
one-time, human-solved CAPTCHA challenge token from Signal's own captcha
service, not a HivePilot-managed secret, and signal-cli's own CLI contract
requires it as an argument with no stdin alternative.)
"""

from __future__ import annotations

import json
import shutil
import signal as signal_module
import subprocess
import threading
from typing import Any

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_RECEIVE_TIMEOUT_SECS = 5  # signal-cli `receive -t <secs>` poll window
_SUBPROCESS_TIMEOUT_SECS = 30
_REST_TIMEOUT_SECS = 10

_HELP_TEXT = (
    "Commands:\n"
    "/run <project> <task> [instructions]\n"
    "/approvals\n"
    "/approve <run_id>\n"
    "/deny <run_id> [reason]\n"
    "/status\n"
    "/help\n"
    "Signal has no buttons — reply 'approve <run_id>' or 'deny <run_id>' directly."
)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _cli_path() -> str:
    return settings.signal_cli_path or "signal-cli"


def _bot_number() -> str:
    number = settings.signal_number
    if not number:
        raise RuntimeError("Signal bot number not configured. Set HIVEPILOT_SIGNAL_NUMBER.")
    return number


def _rest_url() -> str:
    url = settings.signal_rest_url
    if not url:
        raise RuntimeError(
            "Signal REST URL not configured. Set HIVEPILOT_SIGNAL_REST_URL "
            "(base URL of a running signal-cli-rest-api instance)."
        )
    return url.rstrip("/")


def _resolve_mode(explicit: str | None = None) -> str:
    mode = (explicit or settings.signal_receive_mode or "cli").lower()
    if mode not in ("cli", "rest"):
        raise RuntimeError(f"Unknown Signal receive mode: {mode!r}. Use 'cli' or 'rest'.")
    return mode


def _is_allowed(sender: str) -> bool:
    """Return True if sender (E.164) is whitelisted (open to all when list is empty)."""
    allowed = settings.signal_allowed_numbers
    if not allowed:
        return True
    return sender in allowed


def _notification_number() -> str | None:
    return settings.signal_notification_number


# ---------------------------------------------------------------------------
# Command dispatch — reuse chatops_service, never duplicate orchestrator calls
# ---------------------------------------------------------------------------


def _dispatch_text(text: str) -> str:
    """Route an inbound Signal message body to a reply string. `help` is
    handled locally (pure UI text, not a chatops_service concern); everything
    else — including the bare `approve <run_id>`/`deny <run_id>` reply form —
    delegates to `chatops_service.handle_signal`, never reimplementing the
    orchestrator/state_service calls the other bots duplicate locally."""
    text = text.strip()
    if not text:
        return "Unknown command"
    first_word = text.split(None, 1)[0].lstrip("/").lower()
    if first_word == "help":
        return _HELP_TEXT

    from hivepilot.services.chatops_service import handle_signal

    try:
        return handle_signal({"text": text})
    except Exception as exc:  # noqa: BLE001 — never let a bad command crash the receive loop
        logger.error("signal_bot.dispatch.error", error=str(exc))
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Envelope parsing (shared by cli/rest transports)
# ---------------------------------------------------------------------------


def parse_envelope(raw: Any) -> tuple[str, str] | None:
    """Extract (sender, text) from a signal-cli JSON envelope (cli mode) or a
    signal-cli-rest-api receive-list item (rest mode) — both wrap the same
    envelope shape. Returns None for non-data-message envelopes (delivery
    receipts, typing indicators, …) or when the envelope carries no sender or
    no text body."""
    if not isinstance(raw, dict):
        return None
    envelope = raw.get("envelope", raw)
    if not isinstance(envelope, dict):
        return None
    sender = envelope.get("sourceNumber") or envelope.get("source")
    data_message = envelope.get("dataMessage") or {}
    text = data_message.get("message")
    if not sender or not text:
        return None
    return str(sender), str(text)


# ---------------------------------------------------------------------------
# CLI transport (signal-cli)
# ---------------------------------------------------------------------------


def _receive_cli() -> list[dict[str, Any]]:
    binary = shutil.which(_cli_path())
    if not binary:
        logger.warning(
            "signal_bot.cli_missing",
            detail=f"signal-cli not found on PATH ({_cli_path()!r}) — skipping poll",
        )
        return []
    argv = [
        binary,
        "-a",
        _bot_number(),
        "--output=json",
        "receive",
        "-t",
        str(_RECEIVE_TIMEOUT_SECS),
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_SECS, check=False
        )
    except subprocess.TimeoutExpired:
        logger.warning("signal_bot.cli_receive_timeout")
        return []
    except OSError as exc:
        logger.warning("signal_bot.cli_receive_error", error=str(exc))
        return []
    envelopes: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            envelopes.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return envelopes


def _send_cli(recipient: str, message: str) -> None:
    binary = shutil.which(_cli_path())
    if not binary:
        raise RuntimeError(
            f"signal-cli not found on PATH ({_cli_path()!r}). Install signal-cli, or "
            "set HIVEPILOT_SIGNAL_CLI_PATH, or use rest mode with signal-cli-rest-api."
        )
    argv = [binary, "-a", _bot_number(), "send", "-m", message, recipient]
    subprocess.run(
        argv, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_SECS, check=True
    )


# ---------------------------------------------------------------------------
# REST transport (signal-cli-rest-api)
# ---------------------------------------------------------------------------


def _receive_rest() -> list[dict[str, Any]]:
    import requests

    url = f"{_rest_url()}/v1/receive/{_bot_number()}"
    try:
        resp = requests.get(url, timeout=_REST_TIMEOUT_SECS)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — an unreachable REST endpoint must never crash the loop
        logger.warning("signal_bot.rest_receive_error", error=str(exc))
        return []
    data = resp.json()
    return data if isinstance(data, list) else []


def _send_rest(recipient: str, message: str) -> None:
    import requests

    url = f"{_rest_url()}/v2/send"
    payload = {"message": message, "number": _bot_number(), "recipients": [recipient]}
    resp = requests.post(url, json=payload, timeout=_REST_TIMEOUT_SECS)
    resp.raise_for_status()


def _send(recipient: str, message: str, mode: str | None = None) -> None:
    resolved = _resolve_mode(mode)
    if resolved == "rest":
        _send_rest(recipient, message)
    else:
        _send_cli(recipient, message)


# ---------------------------------------------------------------------------
# SignalBot — pull-only receive loop, graceful shutdown
# ---------------------------------------------------------------------------


class SignalBot:
    """Pull-only Signal bot: no webhook mode exists (Signal is E2E P2P), so
    this drives a poll loop against either transport. Mirrors
    `SchedulerDaemon`'s SIGTERM/SIGINT + threading.Event shutdown pattern
    (hivepilot/services/scheduler_daemon.py)."""

    def __init__(self, mode: str | None = None, poll_interval: float = 3.0) -> None:
        self.mode = _resolve_mode(mode)
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        """Blocking receive loop. Handles SIGTERM/SIGINT for graceful shutdown."""
        _bot_number()  # fail fast if unconfigured
        if self.mode == "cli":
            if not shutil.which(_cli_path()):
                raise RuntimeError(
                    f"signal-cli not found on PATH ({_cli_path()!r}). Install signal-cli, or "
                    "use --mode rest with signal-cli-rest-api."
                )
        else:
            _rest_url()  # raises if unconfigured

        signal_module.signal(signal_module.SIGTERM, self._handle_signal)
        signal_module.signal(signal_module.SIGINT, self._handle_signal)

        logger.info("signal_bot.start", mode=self.mode)
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001 — a bad poll tick must never kill the loop
                logger.exception("signal_bot.poll_error")
            self._stop_event.wait(timeout=self.poll_interval)
        logger.info("signal_bot.stop")

    def stop(self) -> None:
        self._stop_event.set()

    def _handle_signal(self, signum: int, frame: Any) -> None:  # noqa: ANN401
        logger.info("signal_bot.signal_received", signum=signum)
        self._stop_event.set()

    def _poll_once(self) -> None:
        envelopes = _receive_cli() if self.mode == "cli" else _receive_rest()
        for raw in envelopes:
            self._handle_envelope(raw)

    def _handle_envelope(self, raw: dict[str, Any]) -> None:
        parsed = parse_envelope(raw)
        if parsed is None:
            return
        sender, text = parsed
        if not _is_allowed(sender):
            logger.warning("signal_bot.unauthorized", sender=sender)
            return
        reply = _dispatch_text(text)
        if reply:
            self.send(sender, reply)

    def send(self, recipient: str, message: str) -> None:
        _send(recipient, message, mode=self.mode)


# ---------------------------------------------------------------------------
# Proactive notifications — called from notification_service / CLI
# ---------------------------------------------------------------------------


def notify(message: str) -> None:
    """Send a plain text message to the configured Signal notification number."""
    recipient = _notification_number()
    if not recipient:
        raise RuntimeError(
            "No Signal notification number configured (HIVEPILOT_SIGNAL_NOTIFICATION_NUMBER)"
        )
    _send(recipient, message)


def notify_approval_required(*, run_id: int, project: str, task: str) -> None:
    """Send a text-only approval request (Signal has no inline buttons — the
    recipient replies `approve <run_id>` / `deny <run_id>`)."""
    recipient = _notification_number()
    if not recipient:
        raise RuntimeError(
            "No Signal notification number configured (HIVEPILOT_SIGNAL_NOTIFICATION_NUMBER)"
        )
    message = (
        f"Approval required — run #{run_id}\n"
        f"Project: {project}\n"
        f"Task: {task}\n"
        f"Reply: approve {run_id}  OR  deny {run_id} [reason]"
    )
    _send(recipient, message)


# ---------------------------------------------------------------------------
# One-time setup helpers — register / link (called from the CLI)
# ---------------------------------------------------------------------------


def register(number: str, *, voice: bool = False, captcha: str | None = None) -> None:
    """Register a phone number as a Signal bot via signal-cli (one-time
    setup). Follow up with `signal-cli -a <number> verify <code>` once the
    SMS/voice code arrives — that step is deliberately NOT wrapped here since
    it needs the human-received code as input."""
    binary = shutil.which(_cli_path())
    if not binary:
        raise RuntimeError(
            f"signal-cli not found on PATH ({_cli_path()!r}). Install signal-cli first."
        )
    argv = [binary, "-a", number, "register"]
    if voice:
        argv.append("--voice")
    if captcha:
        argv += ["--captcha", captcha]
    subprocess.run(argv, check=True)


def link(device_name: str = "hivepilot") -> None:
    """Link this machine as a secondary Signal device. Blocking — prints a
    `sgnl://linkdevice?...` URI/QR data to stdout for the primary phone to
    scan (signal-cli's own output, not captured here so the operator can see
    it live)."""
    binary = shutil.which(_cli_path())
    if not binary:
        raise RuntimeError(
            f"signal-cli not found on PATH ({_cli_path()!r}). Install signal-cli first."
        )
    subprocess.run([binary, "link", "-n", device_name], check=True)


def info() -> dict[str, Any]:
    """Return current Signal bot configuration + signal-cli PATH availability."""
    mode = _resolve_mode()
    cli_path = _cli_path()
    return {
        "mode": mode,
        "number": settings.signal_number or "(not set)",
        "cli_binary": cli_path,
        "cli_available": bool(shutil.which(cli_path)),
        "rest_url": settings.signal_rest_url or "(not set)",
        "notification_number": settings.signal_notification_number or "(not set)",
        "allowed_numbers": list(settings.signal_allowed_numbers or []),
    }
