"""hivepilot.services.setup_wizard_common -- leaf module shared by
`setup_wizard` and `setup_wizard_extra`.

Deliberately has NO import of either sibling module, so both can import
from here without a circular-import cycle (`setup_wizard.py` imports the
steps it doesn't own from `setup_wizard_extra.py`, and both need the same
`SetupOptions`/`STEP_NAMES`/`.env`-upsert/masking primitives).

Everything here is re-exported by `hivepilot.services.setup_wizard` for
backwards-compatible imports (`from hivepilot.services.setup_wizard import
SetupOptions`, etc. keep working).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from hivepilot.config import Settings

if TYPE_CHECKING:
    from rich.console import Console

# Sections runnable individually via `hivepilot setup --only <step>`.
STEP_NAMES: tuple[str, ...] = (
    "config",
    "token",
    "runners",
    "telegram",
    "plugins",
    "concierge",
    "services",
)

# Characters that force double-quoting a `.env` value (LOW-1): whitespace,
# `#` (would otherwise start a comment on an unquoted value), `$`/`"`/`'`
# (parser-significant in dotenv-style files).
_QUOTE_TRIGGER_CHARS = frozenset(" \t\n#$\"'")

# `.env` holds secrets (bot tokens, PATs, the admin token's rotation
# metadata) -- never leave it (or a directory this module creates for it)
# readable by anyone but the owner. See HIGH-1.
_ENV_FILE_MODE = 0o600
_ENV_DIR_MODE = 0o700


class _RequiredValueMissing(Exception):
    """Raised by a step when `--only <step>` + `--non-interactive` was used
    but the one value that step needs to do anything wasn't supplied via a
    flag/env -- caught by `run_setup` and turned into a clean exit(1)."""


class _TelegramPollError(Exception):
    """A handled, human-readable Telegram `getUpdates` API error (409
    conflict, webhook-set) -- never a raw `requests`/JSON exception."""


@dataclass
class SetupOptions:
    """Flags for one `hivepilot setup` invocation -- the non-interactive
    automation surface (CI-friendly `--flag`/env equivalents of every
    interactive prompt) plus wizard-level controls (`only`, `timeout`)."""

    non_interactive: bool = False
    assume_yes: bool = False
    only: str | None = None
    timeout: int = 30
    config_repo: str | None = None
    config_token: str | None = None
    telegram_bot_token: str | None = None
    telegram_allowed_chat_ids: str | None = None  # comma-separated
    telegram_notification_chat_id: int | None = None
    telegram_stream_chat_id: int | None = None
    plugins: str | None = None  # comma-separated
    concierge: bool = False
    env_path: Path | None = None  # override for tests; default: the real .env
    # HIGH-2: minting the bootstrap admin token in --non-interactive mode is
    # opt-in ONLY -- `assume_yes`/`non_interactive` alone must never trigger
    # it (a headless run on a fresh install would otherwise silently mint
    # and, historically, cleartext-print a standing admin credential into
    # whatever captured the process's stdout, e.g. CI logs).
    mint_admin_token: bool = False
    # LOW-2: a non-interactive re-run must not silently clobber an existing
    # secret already written to `.env` -- pass --force to explicitly opt
    # into replacing it.
    force: bool = False


def _default_env_path() -> Path:
    """The same dotenv file `Settings` reads its overrides from -- mirrors
    `plugin_installer._default_env_path` / `ui.plugin_persist.
    persist_plugins_disabled`'s identical resolution."""
    return Path(str(Settings.model_config.get("env_file") or ".env"))


def _quote_env_value(value: str) -> str:
    """Dotenv-style quoting (LOW-1): wrap *value* in double quotes -- with
    the inner `\\`/`"`/`$`/backtick escaped -- when it contains whitespace
    or any of `#$"'`. Values with none of those triggers are left bare
    (keeps every existing plain-value write/test byte-identical)."""
    if not any(ch in value for ch in _QUOTE_TRIGGER_CHARS):
        return value
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    )
    return f'"{escaped}"'


def _chmod_quiet(path: Path, mode: int) -> None:
    """Best-effort `chmod` -- never lets a permissions quirk (odd
    filesystem, already-restricted host) abort the wizard."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _env_upsert(path: Path, key: str, value: str) -> None:
    """Upsert `KEY=value` into *path*, preserving every other line verbatim
    (comments, blanks, unrelated keys) and never duplicating *key*. Same
    upsert shape as `plugin_installer.persist_enabled`, generalized to an
    arbitrary key/value pair so every step in this wizard can share it.

    HIGH-1: the file (and, if this call creates it, its parent directory)
    is chmod'd to owner-only (`0600`/`0700`) after every write -- `.env`
    holds secrets and must never inherit the process umask's default
    world/group-readable permissions.
    """
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    line = f"{key}={_quote_env_value(value)}"
    for i, existing in enumerate(lines):
        if existing.startswith(f"{key}="):
            lines[i] = line
            break
    else:
        lines.append(line)

    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        _chmod_quiet(path.parent, _ENV_DIR_MODE)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _chmod_quiet(path, _ENV_FILE_MODE)


def _env_get(path: Path, key: str) -> str | None:
    """Read *key*'s current raw value from *path*, or None if the file
    doesn't exist, the key isn't present, or its value is empty. Backs the
    LOW-2 non-interactive don't-clobber-an-existing-secret guard -- a
    purely read-only lookup, never writes anything."""
    if not path.exists():
        return None
    prefix = f"{key}="
    for existing in path.read_text(encoding="utf-8").splitlines():
        if existing.startswith(prefix):
            value = existing[len(prefix) :].strip()
            return value or None
    return None


def _mask_secret(value: str, keep: int = 4) -> str:
    """Mask *value* for display: never the full secret, just its last
    *keep* characters behind a fixed run of asterisks."""
    if not value:
        return ""
    tail = value[-keep:] if len(value) > keep else value
    return f"******{tail}"


def _section_header(console: "Console", title: str) -> None:
    console.print(f"\n[bold cyan]-- {title} --[/bold cyan]")


def _status_line(console: "Console", ok: bool, text: str) -> None:
    icon = "[green]\u2713[/green]" if ok else "[dim]\u25cb[/dim]"
    console.print(f"  {icon} {text}")


def _detect_init_system() -> str:
    if shutil.which("rc-service") is not None:
        return "OpenRC"
    if shutil.which("systemctl") is not None:
        return "systemd"
    return "none detected"
