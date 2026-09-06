"""Inbound email/IMAP watcher (HP-75).

An **inbound-only** trigger: for each new allowlisted message in a mailbox, it
starts a RESTRICTED reader run — no HTTP hook, and it can neither send nor
modify mail. The safety shape mirrors OpenClaw 2.0's IMAP watcher:

- **Disabled by default** — a watcher runs only if its YAML entry sets
  `enabled: true`.
- **Sender allowlist required** — an empty allowlist admits nothing
  (fail-closed); a non-allowlisted sender is recorded skipped, never dispatched.
- **Read-only** — the IMAP client selects the mailbox `readonly=True` and only
  fetches; it never stores flags, appends, or deletes. Dedup is by message-id
  in `mail_processed` (survives restart), so `\\Seen` is never needed and a
  message is never processed twice.
- **Bounded admission** — a message whose dispatch keeps failing is retried up
  to `max_admission_failures` (default 3), then recorded skipped rather than
  retried forever.

`poll_once` takes an injected client + dispatch so the whole policy is testable
without a live mailbox; `ImapMailClient` is the real read-only adapter.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import yaml

from hivepilot.config import settings
from hivepilot.services import state_service
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

#: Cap the email body carried into the run prompt.
_MAX_BODY = 8000


@dataclass
class InboundMessage:
    message_id: str
    sender: str
    subject: str = ""
    body: str = ""


@dataclass
class MailWatcher:
    name: str
    host: str
    username: str
    password: str  # literal, ${env:NAME}, or an injected secret
    task: str
    projects: list[str]
    port: int = 993
    mailbox: str = "INBOX"
    allow_senders: list[str] = field(default_factory=list)
    max_admission_failures: int = 3
    enabled: bool = False


@dataclass
class PollResult:
    watcher: str
    disabled: bool = False
    dispatched: int = 0
    rejected: int = 0  # sender not allowlisted
    duplicates: int = 0  # already dispatched/skipped
    failed: int = 0  # dispatch error, still under the retry bound
    skipped: int = 0  # dispatch failed past the retry bound


class MailClient(Protocol):
    def fetch_unseen(self) -> list[InboundMessage]: ...


def load_mail_watchers(path: Path | None = None) -> dict[str, MailWatcher]:
    resolved = settings.resolve_config_path(path or settings.mail_watchers_file)
    if not resolved.exists():
        return {}
    data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    watchers: dict[str, MailWatcher] = {}
    for name, values in (data.get("mail_watchers") or {}).items():
        watchers[name] = MailWatcher(
            name=name,
            host=values["host"],
            username=values["username"],
            password=values.get("password", ""),
            task=values["task"],
            projects=values.get("projects", []),
            port=int(values.get("port", 993)),
            mailbox=values.get("mailbox", "INBOX"),
            allow_senders=list(values.get("allow_senders", [])),
            max_admission_failures=int(values.get("max_admission_failures", 3)),
            enabled=bool(values.get("enabled", False)),
        )
    return watchers


def _extract_addr(sender: str) -> str:
    """The bare address from a `Name <addr@x>` (or plain) From header."""
    match = re.search(r"<([^>]+)>", sender or "")
    return (match.group(1) if match else (sender or "")).strip().lower()


def sender_allowed(sender: str, allow_senders: list[str]) -> bool:
    """Fail-closed allowlist: empty list admits nothing. A rule starting with
    `@` matches a whole domain; otherwise it's an exact address match."""
    if not allow_senders:
        return False
    addr = _extract_addr(sender)
    if not addr:
        return False
    for rule in allow_senders:
        r = rule.strip().lower()
        if not r:
            continue
        if r.startswith("@"):
            if addr.endswith(r):
                return True
        elif addr == r:
            return True
    return False


Dispatch = Callable[[MailWatcher, InboundMessage], None]


def _synthetic_message_id(msg: InboundMessage) -> str:
    """Stable id when the sender omitted ``Message-ID``. Hashed from
    sender+subject+body so two different no-id messages never collapse into
    one dedup key (empty-string would otherwise make the first win forever)."""
    raw = f"{msg.sender}\0{msg.subject}\0{msg.body}"
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]
    return f"<synth-{digest}@hivepilot>"


def _ensure_message_id(msg: InboundMessage) -> InboundMessage:
    if (msg.message_id or "").strip():
        return msg
    msg.message_id = _synthetic_message_id(msg)
    return msg


def _format_email_prompt(msg: InboundMessage) -> str:
    return (
        f"Inbound email received (read-only triage).\n"
        f"From: {msg.sender}\n"
        f"Subject: {msg.subject}\n\n"
        f"{(msg.body or '')[:_MAX_BODY]}"
    )


def _run_restricted(config: MailWatcher, msg: InboundMessage) -> None:
    """Default dispatch: run the watcher's configured task (a restricted reader
    role, by config) with the email as context. Never sends/modifies mail."""
    from hivepilot.orchestrator import Orchestrator

    Orchestrator().run_task(
        project_names=config.projects,
        task_name=config.task,
        extra_prompt=_format_email_prompt(msg),
        auto_git=False,
    )


def poll_once(
    config: MailWatcher, client: MailClient, *, dispatch: Dispatch | None = None
) -> PollResult:
    """Process the mailbox's unseen messages once, enforcing the watcher's
    guardrails. Idempotent across restarts (dedup by message-id)."""
    result = PollResult(watcher=config.name)
    if not config.enabled:
        result.disabled = True
        return result
    do_dispatch = dispatch or _run_restricted

    for msg in client.fetch_unseen():
        msg = _ensure_message_id(msg)
        record = state_service.get_mail_processed(config.name, msg.message_id)
        prior_attempts = int(record["attempts"]) if record else 0
        if record and record["status"] in ("dispatched", "skipped"):
            result.duplicates += 1
            continue
        if not sender_allowed(msg.sender, config.allow_senders):
            state_service.upsert_mail_processed(
                config.name,
                msg.message_id,
                status="skipped",
                attempts=prior_attempts,
                error="sender not allowlisted",
            )
            result.rejected += 1
            continue

        attempts = prior_attempts + 1
        try:
            do_dispatch(config, msg)
        except Exception as exc:  # noqa: BLE001 — a bad dispatch must not abort the poll
            if attempts >= config.max_admission_failures:
                state_service.upsert_mail_processed(
                    config.name, msg.message_id, status="skipped", attempts=attempts, error=str(exc)
                )
                result.skipped += 1
            else:
                state_service.upsert_mail_processed(
                    config.name, msg.message_id, status="pending", attempts=attempts, error=str(exc)
                )
                result.failed += 1
            continue

        state_service.upsert_mail_processed(
            config.name, msg.message_id, status="dispatched", attempts=attempts
        )
        result.dispatched += 1

    return result


def _resolve_secret(value: str) -> str:
    """Resolve `${env:NAME}` to its env var; a literal passes through. (Vault/
    file backends inject via the env, matching the rest of HivePilot.)"""
    match = re.fullmatch(r"\$\{env:([^}]+)\}", (value or "").strip())
    return os.environ.get(match.group(1), "") if match else value


class ImapMailClient:
    """Read-only IMAP adapter. Selects the mailbox `readonly=True` (never sets
    `\\Seen`) and only fetches — it cannot send or modify mail."""

    def __init__(self, config: MailWatcher) -> None:
        self.config = config

    def fetch_unseen(self) -> list[InboundMessage]:
        import email
        import imaplib

        password = _resolve_secret(self.config.password)
        conn = imaplib.IMAP4_SSL(self.config.host, self.config.port)
        messages: list[InboundMessage] = []
        try:
            conn.login(self.config.username, password)
            conn.select(self.config.mailbox, readonly=True)
            _typ, data = conn.search(None, "UNSEEN")
            for num in (data[0] or b"").split():
                _t, msgdata = conn.fetch(num.decode("ascii", "ignore"), "(RFC822)")
                if not msgdata or not isinstance(msgdata[0], tuple):
                    continue
                parsed = email.message_from_bytes(msgdata[0][1])
                messages.append(_parse_email(parsed))
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
        return messages


def _parse_email(parsed: Any) -> InboundMessage:
    body = ""
    if parsed.is_multipart():
        for part in parsed.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")
                    break
    else:
        payload = parsed.get_payload(decode=True)
        if payload:
            body = payload.decode("utf-8", errors="replace")
    msg = InboundMessage(
        message_id=(parsed.get("Message-ID") or "").strip(),
        sender=parsed.get("From") or "",
        subject=parsed.get("Subject") or "",
        body=body,
    )
    return _ensure_message_id(msg)


def poll_all(*, dispatch: Dispatch | None = None, only: str | None = None) -> dict[str, PollResult]:
    """Poll every enabled watcher once (or just `only`). Builds a real
    read-only `ImapMailClient` per watcher."""
    results: dict[str, PollResult] = {}
    for name, config in load_mail_watchers().items():
        if only and name != only:
            continue
        if not config.enabled:
            results[name] = PollResult(watcher=name, disabled=True)
            continue
        try:
            results[name] = poll_once(config, ImapMailClient(config), dispatch=dispatch)
        except Exception as exc:  # noqa: BLE001 — one bad mailbox mustn't kill the rest
            logger.warning("mail_watcher.poll_failed", watcher=name, error=str(exc))
            results[name] = PollResult(watcher=name)
    return results
