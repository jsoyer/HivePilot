"""vaultwarden plugin — a first-party `secrets` provider backed by Vaultwarden.

`Vaultwarden <https://github.com/dani-garcia/vaultwarden>`_ is a lightweight,
self-hosted, Bitwarden-compatible server. This plugin is the self-hosted sibling
of ``plugins/bitwarden.py``: same ``secrets`` plugin provider type, same
fail-closed trust model, same official Bitwarden ``bw`` CLI — but it targets a
self-hosted server instead of the Bitwarden cloud endpoint. Its ``register()``
returns ``{"secrets": {"vaultwarden": VaultwardenBackend()}, "health": {...}}``,
loaded into ``hivepilot.registry.SECRETS_MAP`` (a name that collides with a
builtin or another plugin aborts the load).

**Access path — the official ``bw`` CLI (an EXTERNAL tool, not a Python dep).**
The ``bw`` binary is NEVER a hivepilot dependency and is never installed by this
plugin — it is discovered lazily via ``shutil.which("bw")``. Because a
Vaultwarden deployment lives at an operator-chosen URL, this backend first
points the CLI at that server via ``bw config server <url>`` (from
``settings.vaultwarden_server_url``) and then runs
``bw get item <id-or-name> --response --session <token>``, parsing the JSON
envelope and reading the secret from ``.data.login.password`` (falling back to
``.data.notes`` for secure-note items). The session token is passed
**explicitly** via ``--session`` (read from the ``BW_SESSION`` env var); this
plugin never relies on an ambient, already-unlocked vault.

*Deviation note:* the ``bw config server`` step mutates the CLI's persisted
server setting (there is no per-invocation ``--server`` flag on ``bw get``).
This is the officially documented mechanism for pointing the CLI at a
self-hosted server; it is idempotent for a stable ``vaultwarden_server_url``.

**Fail-closed (HARD).** ``resolve`` raises ``RuntimeError`` if ``bw`` is not on
PATH, if ``BW_SESSION`` is unset, if ``vaultwarden_server_url`` is not
configured, if the CLI errors, or if no usable value is found. Every error names
ONLY the item + provider (``vaultwarden``) and the setting/env-var by name
(e.g. ``"vaultwarden_server_url is not configured"``, ``"BW_SESSION is not
set"``) — NEVER the fetched secret value and NEVER the ``BW_SESSION`` token — so
the pipeline's ``closed`` fail-mode aborts. A ``CalledProcessError`` embeds the
full command line (including the ``--session`` token) in its message; the
redact-and-reraise boundary in ``_fetch`` (``raise ... from None`` +
type-name-only text) guarantees neither that token nor a value-bearing
``stdout`` ever propagates.

**Deliberately NOT a ``@dataclass``:** local-file plugins are loaded via
``importlib.util.spec_from_file_location()`` / ``exec_module()``
(``hivepilot.plugins._scan_local_plugins``), which never registers the module in
``sys.modules``. Combined with ``from __future__ import annotations`` that trips
a real CPython 3.14 ``dataclasses`` bug — see ``plugins/infisical.py`` /
``plugins/mem0.py`` for the full write-up. Plain classes sidestep it entirely.

Configured via ``HIVEPILOT_VAULTWARDEN_ENABLED`` /
``HIVEPILOT_VAULTWARDEN_SERVER_URL`` (``hivepilot/config.py``; enabled opt-out,
default True) + the ``BW_SESSION`` environment variable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from typing import Any

from hivepilot.config import Settings
from hivepilot.plugins import HealthStatus
from hivepilot.registry import SecretRef
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Provider name — the key this backend registers under in SECRETS_MAP and the
# only identifier (besides the item location) ever surfaced in an error message.
_PROVIDER = "vaultwarden"
# The official Bitwarden CLI binary (external tool; never a Python dependency).
_BW_BINARY = "bw"
# The env var carrying the unlocked-vault session token. Its VALUE is never
# logged or surfaced — only its NAME appears in a fail-closed error.
_SESSION_ENV = "BW_SESSION"

# Serializes this process's `bw` invocations so the two-step
# `config server` + `get item` sequence is ATOMIC. `bw` has no per-invocation
# `--server` flag, so the server is set via GLOBAL, persisted CLI state; without
# this lock two concurrent vaultwarden resolves could interleave as
# "config server URL-A" -> "config server URL-B" -> "get (against B)" and fetch
# from the wrong server. RESIDUAL (inherent to `bw`, degrades closed): because
# that server setting is global, running this self-hosted backend concurrently
# with the cloud `bitwarden` backend in ONE process is unsupported — after this
# backend runs the CLI stays pointed at the self-hosted server, so a later cloud
# `bw get` hits the wrong server and fails closed. Isolate the two providers in
# separate processes if you need both.
_BW_CLI_LOCK = threading.Lock()


def _extract_secret_value(payload: Any) -> str | None:
    """Best-effort extraction of the secret string from a ``bw get item
    --response`` JSON payload.

    Reads ``.data.login.password`` first (login items), then falls back to
    ``.data.notes`` (secure-note items). Tolerates a bare (un-enveloped) item
    dict too. Returns ``None`` (rather than raising) when no usable string value
    is found, so the caller emits a single redacted error.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    node = data if isinstance(data, dict) else payload

    login = node.get("login")
    if isinstance(login, dict):
        password = login.get("password")
        if isinstance(password, str) and password:
            return password

    notes = node.get("notes")
    if isinstance(notes, str) and notes:
        return notes
    return None


class VaultwardenBackend:
    """Resolve a secret stored in a self-hosted Vaultwarden server via ``bw``.

    ``ref.spec`` keys (mirrors the builtin backends' spec-reading):
      item: (required) the item id or name to fetch. ``id`` / ``name`` are
            accepted as aliases.

    The server URL comes from ``settings.vaultwarden_server_url`` (a required
    setting for this backend) — never from ``ref.spec``.
    """

    name = _PROVIDER

    def resolve(self, ref: SecretRef, settings: Settings) -> str:
        item = ref.spec.get("item") or ref.spec.get("id") or ref.spec.get("name")
        if not item:
            raise RuntimeError(
                f"{_PROVIDER} secret is missing the required spec field 'item' "
                "(the item id or name)"
            )
        item = str(item)

        if shutil.which(_BW_BINARY) is None:
            raise RuntimeError(
                f"{_PROVIDER} secret {item!r} cannot be resolved: the "
                f"'{_BW_BINARY}' CLI is not installed or not on PATH"
            )

        server_url = settings.vaultwarden_server_url
        if not server_url:
            raise RuntimeError(
                f"{_PROVIDER} secret {item!r} cannot be resolved: "
                "vaultwarden_server_url is not configured"
            )

        session = os.environ.get(_SESSION_ENV)
        if not session:
            raise RuntimeError(
                f"{_PROVIDER} secret {item!r} cannot be resolved: {_SESSION_ENV} is not set"
            )

        return self._fetch(item=item, session=session, server_url=server_url)

    def _fetch(self, *, item: str, session: str, server_url: str) -> str:
        # The whole CLI interaction sits INSIDE this try: a CalledProcessError
        # (check=True) embeds the full argv -- INCLUDING the `--session` token --
        # in its message, and stdout may carry the secret value. `from None` +
        # a type-name-only message guarantees neither leaks, and severs
        # `__context__` so a caller walking the exception chain can't resurface
        # the original, token/value-bearing error.
        try:
            # Hold the CLI lock across BOTH steps so the server-pin and the
            # fetch can't be split by another provider/thread's `bw` call (see
            # _BW_CLI_LOCK). Only the subprocess calls need serializing; the
            # JSON parse below runs outside the lock.
            with _BW_CLI_LOCK:
                # Point the CLI at the self-hosted server first (no
                # per-invocation --server flag exists on `bw get`).
                subprocess.run(
                    [_BW_BINARY, "config", "server", server_url],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                proc = subprocess.run(
                    [_BW_BINARY, "get", "item", item, "--response", "--session", session],
                    capture_output=True,
                    text=True,
                    check=True,
                )
            payload = json.loads(proc.stdout)
        except Exception as exc:
            logger.warning(
                "plugin.vaultwarden.fetch_failed",
                provider=_PROVIDER,
                item=item,
                error_type=type(exc).__name__,
            )
            raise RuntimeError(
                f"{_PROVIDER} failed to resolve secret {item!r} ({type(exc).__name__})"
            ) from None

        value = _extract_secret_value(payload)
        if not value:
            # Fail-closed on a missing/empty value too (mirrors
            # EnvSecretsBackend's `if not value: raise`) -- an empty string is a
            # truthy `is not None` check but is never a legitimate secret.
            raise RuntimeError(f"{_PROVIDER} returned no usable value for secret {item!r}")
        return value


def health(**kwargs: Any) -> HealthStatus:
    """Report Vaultwarden CLI CONFIGURATION status only — NEVER the BW_SESSION
    token value, the server URL as a secret, or a resolved secret (Phase 19
    discipline):

    - `error` when the `bw` CLI is not on PATH.
    - `degraded` ("not configured") when `bw` is present but ``BW_SESSION`` is
      unset OR ``vaultwarden_server_url`` is not configured.
    - `ok` ("configured") when `bw` is on PATH AND ``BW_SESSION`` is set AND the
      server URL is configured.

    The detail carries mode names only — never the session token, the server
    URL, or a fetched value. Never raises: any internal error is reported as the
    exception TYPE name only, matching ``PluginManager.run_health_check``.
    """
    try:
        if shutil.which(_BW_BINARY) is None:
            return HealthStatus("error", f"{_BW_BINARY} CLI not installed")

        from hivepilot.config import settings

        if os.environ.get(_SESSION_ENV) and settings.vaultwarden_server_url:
            return HealthStatus("ok", "configured")
        return HealthStatus("degraded", "not configured")
    except Exception as exc:  # noqa: BLE001 — a health check must never crash
        return HealthStatus("error", type(exc).__name__)


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.vaultwarden_enabled:
        return {}
    return {"secrets": {_PROVIDER: VaultwardenBackend()}, "health": {_PROVIDER: health}}
