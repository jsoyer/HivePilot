"""bitwarden plugin — a first-party `secrets` provider backed by Bitwarden.

`Bitwarden <https://bitwarden.com>`_ is an open-source password / secrets
manager. This plugin lets a pipeline config reference a value stored in the
user's own Bitwarden vault (``${secret:NAME}`` where NAME's spec has
``source: bitwarden``) instead of inlining it. It is a structural sibling of
``plugins/infisical.py`` / ``plugins/onepassword.py`` — same ``secrets`` plugin
provider type, same fail-closed trust model: its ``register()`` returns
``{"secrets": {"bitwarden": BitwardenBackend()}, "health": {...}}``, which
``hivepilot.plugins.PluginManager`` loads into
``hivepilot.registry.SECRETS_MAP`` (a name that collides with a builtin or
another plugin aborts the load).

**Access path — the official ``bw`` CLI (an EXTERNAL tool, not a Python dep).**
Unlike the infisical/onepassword backends (which use Python SDKs), this backend
shells out to the official Bitwarden command-line client, ``bw``. That binary is
NEVER a hivepilot dependency and is never installed by this plugin — it is
discovered lazily via ``shutil.which("bw")`` so the plugin loads (and only fails
at *resolve* time) when the CLI is absent.

A value is addressed by its Bitwarden item (``ref.spec["item"]`` — an item id or
name). Retrieval runs ``bw get item <id-or-name> --response --session <token>``,
parses the JSON envelope, and reads the secret from ``.data.login.password``
(falling back to ``.data.notes`` for secure-note items). The session token is
passed **explicitly** via ``--session`` (read from the ``BW_SESSION`` env var);
this plugin never relies on an ambient, already-unlocked vault.

**Fail-closed (HARD).** ``resolve`` raises ``RuntimeError`` if ``bw`` is not on
PATH, if ``BW_SESSION`` is unset, if the CLI errors, or if no usable value is
found. Every error names ONLY the item + provider (``bitwarden``) and the
setting/env-var by name — NEVER the fetched secret value and NEVER the
``BW_SESSION`` token — so the pipeline's ``closed`` fail-mode aborts. A
``CalledProcessError`` from the CLI embeds the full command line (including the
``--session`` token) in its message; the redact-and-reraise boundary in
``_fetch`` (``raise ... from None`` + type-name-only text) guarantees neither
that token nor a value-bearing ``stdout`` ever propagates.

**Deliberately NOT a ``@dataclass``:** local-file plugins are loaded via
``importlib.util.spec_from_file_location()`` / ``exec_module()``
(``hivepilot.plugins._scan_local_plugins``), which never registers the module in
``sys.modules``. Combined with ``from __future__ import annotations`` that trips
a real CPython 3.14 ``dataclasses`` bug — see ``plugins/infisical.py`` /
``plugins/mem0.py`` for the full write-up. Plain classes sidestep it entirely.

Configured via ``HIVEPILOT_BITWARDEN_ENABLED`` (``hivepilot/config.py``,
opt-out; default True) + the ``BW_SESSION`` environment variable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

from hivepilot.config import Settings
from hivepilot.plugins import HealthStatus
from hivepilot.registry import SecretRef
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Provider name — the key this backend registers under in SECRETS_MAP and the
# only identifier (besides the item location) ever surfaced in an error message.
_PROVIDER = "bitwarden"
# The official Bitwarden CLI binary (external tool; never a Python dependency).
_BW_BINARY = "bw"
# The env var carrying the unlocked-vault session token. Its VALUE is never
# logged or surfaced — only its NAME appears in a fail-closed error.
_SESSION_ENV = "BW_SESSION"


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


class BitwardenBackend:
    """Resolve a secret stored in Bitwarden via the official ``bw`` CLI.

    ``ref.spec`` keys (mirrors the builtin backends' spec-reading):
      item: (required) the Bitwarden item id or name to fetch. ``id`` / ``name``
            are accepted as aliases.
    """

    name = _PROVIDER

    def resolve(self, ref: SecretRef, settings: Settings) -> str:
        item = ref.spec.get("item") or ref.spec.get("id") or ref.spec.get("name")
        if not item:
            raise RuntimeError(
                f"{_PROVIDER} secret is missing the required spec field 'item' "
                "(the Bitwarden item id or name)"
            )
        item = str(item)

        if shutil.which(_BW_BINARY) is None:
            raise RuntimeError(
                f"{_PROVIDER} secret {item!r} cannot be resolved: the "
                f"'{_BW_BINARY}' CLI is not installed or not on PATH"
            )

        session = os.environ.get(_SESSION_ENV)
        if not session:
            raise RuntimeError(
                f"{_PROVIDER} secret {item!r} cannot be resolved: {_SESSION_ENV} is not set"
            )

        return self._fetch(item=item, session=session)

    def _fetch(self, *, item: str, session: str) -> str:
        # The whole CLI interaction sits INSIDE this try: a CalledProcessError
        # (check=True) embeds the full argv -- INCLUDING the `--session` token --
        # in its message, and stdout may carry the secret value. `from None` +
        # a type-name-only message guarantees neither leaks, and severs
        # `__context__` so a caller walking the exception chain can't resurface
        # the original, token/value-bearing error.
        try:
            proc = subprocess.run(
                [_BW_BINARY, "get", "item", item, "--response", "--session", session],
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(proc.stdout)
        except Exception as exc:
            logger.warning(
                "plugin.bitwarden.fetch_failed",
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
    """Report Bitwarden CLI CONFIGURATION status only — NEVER the BW_SESSION
    token value and NEVER a resolved secret (Phase 19 discipline):

    - `error` when the `bw` CLI is not on PATH.
    - `degraded` ("not configured") when `bw` is present but ``BW_SESSION`` is
      unset (no unlocked-vault session).
    - `ok` ("configured") when `bw` is on PATH AND ``BW_SESSION`` is set.

    The detail carries mode names only — never the session token or a fetched
    value. Never raises: any internal error is reported as the exception TYPE
    name only, matching ``PluginManager.run_health_check``.
    """
    try:
        if shutil.which(_BW_BINARY) is None:
            return HealthStatus("error", f"{_BW_BINARY} CLI not installed")
        if os.environ.get(_SESSION_ENV):
            return HealthStatus("ok", "configured")
        return HealthStatus("degraded", "not configured")
    except Exception as exc:  # noqa: BLE001 — a health check must never crash
        return HealthStatus("error", type(exc).__name__)


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.bitwarden_enabled:
        return {}
    return {"secrets": {_PROVIDER: BitwardenBackend()}, "health": {_PROVIDER: health}}
