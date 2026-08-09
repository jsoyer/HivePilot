"""onepassword plugin — a first-party `secrets` provider backed by 1Password.

This plugin lets a pipeline config reference a value stored in
`1Password <https://1password.com>`_ (``${secret:NAME}`` where NAME's spec has
``source: onepassword``) instead of inlining it. It is a structural sibling of
``plugins/infisical.py`` — same ``secrets`` plugin provider type, same
fail-closed trust model: its ``register()`` returns
``{"secrets": {"onepassword": OnePasswordBackend()}}``, which
``hivepilot.plugins.PluginManager`` loads into ``hivepilot.registry.SECRETS_MAP``
(a name that collides with a builtin or another plugin aborts the load).

**Reference model.** 1Password addresses a value with a *secret reference*
``op://<vault>/<item>/<field>``. This backend accepts BOTH conventions
(mirroring how ``InfisicalBackend`` reads ``ref.spec`` keys):

- ``ref.spec["ref"]`` — a full ``op://vault/item/field`` string (takes
  precedence when present); or
- discrete ``ref.spec["vault"]`` + ``ref.spec["item"]`` + ``ref.spec["field"]``
  (all three required when no full ``ref`` is given).

The reference identity (``op://vault/item/field``) is the ONLY thing — besides
the provider name — ever surfaced in an error/log. It names a *location*
(vault/item/field titles or ids), never the fetched value or the token.

**Credential modes (both talk to a 1Password Connect endpoint).** The chosen
SDK (``onepasswordconnectsdk``) authenticates against a Connect API base URL
(``op_connect_host``, self-hostable) with a token. Two tokens are supported:

- **Connect** — ``op_connect_host`` + ``op_connect_token``.
- **service-account** — ``op_service_account_token`` (used only when no Connect
  token is set), presented to the same Connect endpoint. The SDK's
  ``new_client(url, token)`` documents its ``token`` argument as a *"Service
  Account token"*, so this is the SDK's own supported usage.

  *Caveat:* a hosted service account that does NOT front a Connect server would
  instead need the separate ``onepassword`` SDK (``op://`` resolution against
  ``api.1password.com``); that is out of scope for this Connect-based plugin.

**Lazy optional import & graceful degradation.** The 1Password Connect SDK
(``pip install onepasswordconnectsdk``) is NOT a hivepilot dependency and is
deliberately never installed by this plugin — imported lazily so the plugin
loads (and only fails at *resolve* time) when the library is absent. When the
SDK is missing, config is incomplete, the client errors, or no usable value is
found, ``resolve`` raises a clear ``RuntimeError`` naming ONLY the reference
identity + the provider name (``onepassword``) — NEVER the token or the fetched
value — so the pipeline's ``closed`` fail-mode aborts.

**SDK import/method names (verified against onepasswordconnectsdk 2.1.0).**
``from onepasswordconnectsdk.client import new_client``;
``new_client(url, token)`` returns a sync ``Client``;
``client.get_item(item, vault)`` returns an ``Item`` whose ``.fields`` is a list
of field objects each carrying ``.label`` / ``.id`` / ``.value``. The requested
field is matched by ``.label`` first, then ``.id``. If a future SDK differs,
``_fetch`` degrades: any SDK exception is caught and re-raised as the redacted,
identity-only error above — never leaking a value, token, or raw upstream
message. **Verify the SDK surface against your installed version before relying
on this provider in production.**

**Deliberately NOT a ``@dataclass``:** local-file plugins are loaded via
``importlib.util.spec_from_file_location()`` / ``exec_module()``
(``hivepilot.plugins._scan_local_plugins``), which never registers the module in
``sys.modules``. Combined with ``from __future__ import annotations`` that trips
a real CPython 3.14 ``dataclasses`` bug — see ``plugins/infisical.py`` /
``plugins/mem0.py`` for the full write-up. Plain classes sidestep it entirely.

Configured via ``HIVEPILOT_OP_*`` (``hivepilot/config.py``): ``op_connect_host``,
``op_connect_token``, ``op_service_account_token``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Coroutine

from hivepilot.config import Settings
from hivepilot.plugins import HealthStatus
from hivepilot.registry import SecretRef
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

try:
    from onepasswordconnectsdk.client import new_client
except ImportError:  # onepasswordconnectsdk is optional — never installed by this plugin
    new_client = None  # type: ignore[assignment,misc]

# Direct (non-Connect) service-account mode: the official async `onepassword`
# SDK (`pip install onepassword-sdk`) resolves an `op://vault/item/field`
# reference straight against api.1password.com — no self-hosted Connect server.
# Lazy/optional, like `new_client` above: absent -> only the direct path fails,
# at resolve time, with a redacted lib-only error.
try:
    from onepassword.client import Client as _OPClient
except ImportError:  # onepassword-sdk is optional — never installed by this plugin
    _OPClient = None  # type: ignore[assignment,misc]

# Provider name — the key this backend registers under in SECRETS_MAP and the
# only identifier (besides the reference identity) ever surfaced in an error.
_PROVIDER = "onepassword"

# The `op` CLI binary. A third resolution mode, and the only one that needs no
# Python package: both SDK modes require a pip install, and on this project's
# production box a heavy `pip install` has wedged the host before.
#
# It does NOT remove the credential requirement -- headless, `op` still needs
# OP_SERVICE_ACCOUNT_TOKEN (or an interactive `op signin` session). What it
# removes is the SDK.
_CLI_BINARY = "op"
_CLI_TIMEOUT_S = 20

_MODE_CONNECT = "connect"
_MODE_SERVICE_ACCOUNT = "service-account"
_MODE_CLI = "cli"


def _cli_available() -> bool:
    """True when the `op` binary is on PATH."""
    return shutil.which(_CLI_BINARY) is not None


def _sdk_available() -> bool:
    """True when the direct service-account SDK is importable."""
    return _OPClient is not None


def _mode_report(settings_obj: Any) -> list[tuple[str, bool, str]]:
    """(mode, usable, detail) for every mode, in precedence order.

    Reported per mode rather than collapsed into one verdict because the
    modes need DIFFERENT things -- `onepasswordconnectsdk`, `onepassword-sdk`,
    or just the binary -- and the previous single verdict called a working
    service-account or CLI deployment "error: onepasswordconnectsdk not
    installed". Never contains a token: presence booleans only.
    """
    host = bool(getattr(settings_obj, "op_connect_host", None))
    connect_token = bool(getattr(settings_obj, "op_connect_token", None))
    sa_token = bool(getattr(settings_obj, "op_service_account_token", None))

    connect_ok = host and (connect_token or sa_token) and new_client is not None
    connect_detail = (
        "ready" if connect_ok else "needs op_connect_host + a token + onepasswordconnectsdk"
    )

    sa_ok = sa_token and not host and _sdk_available()
    sa_detail = "ready" if sa_ok else "needs op_service_account_token + onepassword-sdk"

    cli_ok = _cli_available() and (sa_token or _cli_session_present())
    if cli_ok:
        cli_detail = "ready"
    elif not _cli_available():
        cli_detail = f"needs the `{_CLI_BINARY}` binary on PATH"
    else:
        cli_detail = "binary present, needs op_service_account_token or an `op signin` session"

    return [
        (_MODE_CONNECT, bool(connect_ok), connect_detail),
        (_MODE_SERVICE_ACCOUNT, bool(sa_ok), sa_detail),
        (_MODE_CLI, bool(cli_ok), cli_detail),
    ]


def _cli_session_present() -> bool:
    """True when the environment already carries an `op` session/credential.

    Checked rather than assumed: a daemon has neither, and reporting `ok`
    because a binary exists would be the "installed therefore working"
    mistake this codebase keeps paying for.
    """
    return any(
        os.environ.get(name)
        for name in ("OP_SERVICE_ACCOUNT_TOKEN", "OP_SESSION", "OP_CONNECT_TOKEN")
    ) or any(name.startswith("OP_SESSION_") for name in os.environ)


def _run_coro(coro: "Coroutine[Any, Any, Any]") -> Any:
    """Run *coro* to completion synchronously, future-proofed against a caller
    that is ALREADY inside a running event loop (e.g. a future async runner
    invoking this synchronous ``resolve()``).

    ``asyncio.run()`` raises ``RuntimeError: asyncio.run() cannot be called
    from a running event loop`` when one is already active. Today's callers
    (``SecretResolver.resolve`` / ``secret_refs`` / the CLI / the orchestrator)
    are all synchronous, so the running-loop branch is not exercised in
    practice yet — but this keeps the plugin's public ``resolve()`` contract
    (a plain synchronous call) safe under ANY caller, present or future. When a
    loop is already running, the coroutine runs on a short-lived, single-use
    worker thread (its own fresh loop via ``asyncio.run``) so it never competes
    with the caller's loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop running in this thread — the common case. Safe to drive the
        # coroutine directly.
        return asyncio.run(coro)
    # A loop IS already running in this thread — asyncio.run() would raise.
    # Hand the coroutine to a dedicated worker thread with its own fresh loop.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


def _parse_ref(ref: SecretRef) -> tuple[str, str, str]:
    """Resolve the (vault, item, field) triple from ``ref.spec``.

    Accepts a full ``op://vault/item/field`` string in ``ref.spec["ref"]``
    (takes precedence) OR discrete ``vault`` / ``item`` / ``field`` keys.
    Raises ``RuntimeError`` (naming only the provider) when neither form yields
    all three components.
    """
    full = ref.spec.get("ref")
    if full:
        if not isinstance(full, str) or not full.startswith("op://"):
            raise RuntimeError(
                f"{_PROVIDER} secret 'ref' must be an 'op://vault/item/field' reference"
            )
        parts = [p for p in full[len("op://") :].split("/") if p]
        # Only exactly 3 segments (vault/item/field) are accepted. A
        # section-qualified ref (op://vault/item/section/field) is REJECTED
        # rather than silently collapsed to its last segment: collapsing would
        # let `_extract_field_value`'s label/id match (which scans ALL fields
        # regardless of section) silently fetch the wrong secret when two
        # sections share a field label -- the one path that would otherwise
        # violate this plugin's fail-closed posture.
        if len(parts) != 3:
            raise RuntimeError(
                f"{_PROVIDER} secret ref {full!r} must be exactly "
                "'op://vault/item/field' (section-qualified refs are not supported)"
            )
        return parts[0], parts[1], parts[2]

    vault = ref.spec.get("vault")
    item = ref.spec.get("item")
    field = ref.spec.get("field")
    if not (vault and item and field):
        raise RuntimeError(
            f"{_PROVIDER} secret is missing required spec fields: provide a full "
            "'ref' (op://vault/item/field) or all of 'vault', 'item' and 'field'"
        )
    return str(vault), str(item), str(field)


def _extract_field_value(item_obj: Any, field: str) -> str | None:
    """Best-effort extraction of ``field``'s string value from an SDK ``Item``.

    Tolerant of SDK signature drift: reads ``item_obj.fields`` (or a ``fields``
    dict key), matching each entry by ``.label`` then ``.id`` (or the equivalent
    dict keys) against ``field``. Returns ``None`` (rather than raising) when no
    matching string value is found, so the caller emits a single redacted error.
    """
    fields = getattr(item_obj, "fields", None)
    if fields is None and isinstance(item_obj, dict):
        fields = item_obj.get("fields")
    if not fields:
        return None

    for entry in fields:
        label = getattr(entry, "label", None)
        ident = getattr(entry, "id", None)
        value = getattr(entry, "value", None)
        if isinstance(entry, dict):
            label = entry.get("label", label)
            ident = entry.get("id", ident)
            value = entry.get("value", value)
        if field in (label, ident) and isinstance(value, str):
            return value
    return None


class OnePasswordBackend:
    """Resolve a secret stored in 1Password (via a Connect endpoint).

    ``ref.spec`` keys (mirrors the builtin backends' spec-reading):
      ref:    (optional) a full ``op://vault/item/field`` reference; when set it
              takes precedence over the discrete keys below.
      vault:  (required unless ``ref``) the vault title or id.
      item:   (required unless ``ref``) the item title or id.
      field:  (required unless ``ref``) the field label or id to read.
    """

    name = _PROVIDER

    def resolve(self, ref: SecretRef, settings: Settings) -> str:
        # Direct (non-Connect) service-account mode is selected ONLY when no
        # Connect host is configured but a service-account token IS. When a
        # Connect host is set, the Connect path is always used — even with only
        # a service-account token — so existing Connect deployments are
        # unaffected (backward-compatible selection).
        if not settings.op_connect_host and settings.op_service_account_token:
            if _sdk_available():
                return self._resolve_service_account(ref, settings)
            # The SDK is absent. Before failing, try the `op` CLI -- same
            # credential, same reference model, no Python package. This is the
            # ONLY behaviour change for an existing deployment, and it can only
            # turn a hard failure into a success.
            if _cli_available():
                try:
                    return self._resolve_cli(ref, settings)
                except RuntimeError as exc:
                    # Keep BOTH facts. The CLI failure alone would hide that
                    # the intended mode was service-account and that its
                    # package is simply not installed -- the one thing the
                    # operator can act on.
                    raise RuntimeError(
                        f"{exc} The 'onepassword-sdk' package is also not "
                        "installed, so direct service-account mode was "
                        "unavailable (pip install onepassword-sdk)."
                    ) from None
            return self._resolve_service_account(ref, settings)

        vault, item, field = _parse_ref(ref)
        identity = f"op://{vault}/{item}/{field}"

        # No Connect host configured at all and no service-account token: the
        # CLI may still be signed in. Checked before the Connect error below,
        # whose advice would otherwise be wrong for a CLI-only host.
        if not settings.op_connect_host and _cli_available() and _cli_session_present():
            return self._resolve_cli(ref, settings)

        if new_client is None:
            raise RuntimeError(
                f"{_PROVIDER} secret {identity!r} cannot be resolved: the "
                "'onepasswordconnectsdk' package is not installed "
                "(pip install onepasswordconnectsdk)"
            )

        host = settings.op_connect_host
        # Connect token preferred; fall back to a service-account token. Both
        # authenticate the SAME Connect client — selection is purely which
        # credential is presented.
        token = settings.op_connect_token or settings.op_service_account_token

        missing = [
            env_name
            for env_name, value in (
                ("HIVEPILOT_OP_CONNECT_HOST", host),
                ("HIVEPILOT_OP_CONNECT_TOKEN or HIVEPILOT_OP_SERVICE_ACCOUNT_TOKEN", token),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"{_PROVIDER} secret {identity!r} is not configured: missing {', '.join(missing)}"
            )

        return self._fetch(
            host=host,
            token=token,
            vault=vault,
            item=item,
            field=field,
            identity=identity,
        )

    def _resolve_cli(self, ref: SecretRef, settings: Settings) -> str:
        """Resolve via `op read op://vault/item/field`.

        The token goes in the ENVIRONMENT, never in argv: a process's command
        line is world-readable through /proc, so a token on it leaks to every
        local user. Its environment is not.

        stdout is the secret. It is never logged, and a failure surfaces the
        reference identity and the exit code ONLY -- `op`'s stderr can echo
        both the reference and, on some errors, the value, so it is
        deliberately not propagated.
        """
        vault, item, field = _parse_ref(ref)
        identity = f"op://{vault}/{item}/{field}"
        binary = shutil.which(_CLI_BINARY)
        if binary is None:
            raise RuntimeError(
                f"{_PROVIDER} secret {identity!r} cannot be resolved: the "
                f"`{_CLI_BINARY}` CLI is not on PATH"
            )

        env = dict(os.environ)
        token = settings.op_service_account_token
        if token:
            env["OP_SERVICE_ACCOUNT_TOKEN"] = token

        try:
            completed = subprocess.run(  # noqa: S603 - operator-installed 1Password CLI
                [binary, "read", identity, "--no-newline"],
                capture_output=True,
                text=True,
                timeout=_CLI_TIMEOUT_S,
                check=False,
                env=env,
                stdin=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001 - never leak the upstream message
            raise RuntimeError(
                f"{_PROVIDER} secret {identity!r} could not be read via the "
                f"`{_CLI_BINARY}` CLI ({type(exc).__name__})"
            ) from None

        if completed.returncode != 0:
            raise RuntimeError(
                f"{_PROVIDER} secret {identity!r} could not be read via the "
                f"`{_CLI_BINARY}` CLI (exit {completed.returncode}). Check the "
                "reference and that the credential grants access to that vault."
            )

        value = (completed.stdout or "").strip()
        if not value:
            # An empty string would sail through as a resolved secret and fail
            # far away, at whatever consumes it.
            raise RuntimeError(
                f"{_PROVIDER} secret {identity!r} resolved to an EMPTY value via "
                f"the `{_CLI_BINARY}` CLI; refusing to return it as a secret"
            )
        return value

    def _resolve_service_account(self, ref: SecretRef, settings: Settings) -> str:
        """Direct (non-Connect) resolution via the official async
        ``onepassword-sdk`` against api.1password.com. Reuses the same
        ``op://vault/item/field`` reference model; the reference identity is the
        ONLY thing (besides the provider) ever surfaced in an error — never the
        token or the fetched value."""
        reference = ref.spec.get("reference") or ref.spec.get("ref")
        if reference:
            if not isinstance(reference, str) or not reference.startswith("op://"):
                raise RuntimeError(
                    f"{_PROVIDER} secret 'reference' must be an 'op://vault/item/field' reference"
                )
        else:
            vault, item, field = _parse_ref(ref)
            reference = f"op://{vault}/{item}/{field}"
        identity = reference

        if _OPClient is None:
            raise RuntimeError(
                f"{_PROVIDER} secret {identity!r} cannot be resolved: the "
                "'onepassword-sdk' package is required for 1Password service-account "
                "(non-Connect) mode (pip install onepassword-sdk)"
            )

        token = settings.op_service_account_token
        try:
            # The official SDK is async; run its authenticate+resolve to
            # completion synchronously. Construction is INSIDE this guard: an
            # authenticate() failure can embed the token in its message — the
            # exact leak this redact-and-reraise boundary prevents.
            value = _run_coro(
                self._fetch_service_account(
                    reference=reference,
                    token=token,
                    integration_name=settings.op_integration_name,
                    integration_version=settings.op_integration_version,
                )
            )
        except Exception as exc:
            logger.warning(
                "plugin.onepassword.sa_fetch_failed",
                provider=_PROVIDER,
                reference=identity,
                error_type=type(exc).__name__,
            )
            raise RuntimeError(
                f"{_PROVIDER} failed to resolve secret {identity!r} ({type(exc).__name__})"
            ) from None

        if not value or not isinstance(value, str):
            # Fail-closed on an empty/missing value (mirrors the Connect path
            # and EnvSecretsBackend's `if not value: raise`).
            raise RuntimeError(f"{_PROVIDER} returned no usable value for secret {identity!r}")
        return value

    async def _fetch_service_account(
        self,
        *,
        reference: str,
        token: str,
        integration_name: str,
        integration_version: str,
    ) -> Any:
        client = await _OPClient.authenticate(
            auth=token,
            integration_name=integration_name,
            integration_version=integration_version,
        )
        return await client.secrets.resolve(reference)

    def _build_client(self, host: str, token: str) -> Any:
        # Positional (url, token) — matches onepasswordconnectsdk.new_client.
        return new_client(host, token)

    def _fetch(
        self,
        *,
        host: str,
        token: str,
        vault: str,
        item: str,
        field: str,
        identity: str,
    ) -> str:
        # Client construction is INSIDE this try too: Connect clients can
        # authenticate at construction time, and a construction failure can
        # embed the token (or, in principle, a value) in its message -- the
        # exact leak this redact-and-reraise boundary exists to prevent. Never
        # let `_build_client` run outside this guard.
        try:
            client = self._build_client(host, token)
            item_obj = client.get_item(item, vault)
        except Exception as exc:
            # Re-raise with the reference identity + provider ONLY. `from None`
            # + no str(exc) guarantees neither the token, the fetched value, nor
            # a raw upstream message (which could echo either) leaks -- and
            # severs `__context__` so a caller logging `exc.__cause__` / walking
            # the exception chain can't resurface the original error.
            logger.warning(
                "plugin.onepassword.fetch_failed",
                provider=_PROVIDER,
                reference=identity,
                error_type=type(exc).__name__,
            )
            raise RuntimeError(
                f"{_PROVIDER} failed to resolve secret {identity!r} ({type(exc).__name__})"
            ) from None

        value = _extract_field_value(item_obj, field)
        if not value:
            # Fail-closed on a missing/empty/whitespace value too (mirrors
            # EnvSecretsBackend's `if not value: raise`) -- an empty string is a
            # truthy `is not None` check but is never a legitimate secret.
            raise RuntimeError(f"{_PROVIDER} returned no usable value for secret {identity!r}")
        return value


def health(**kwargs: Any) -> HealthStatus:
    """Report CONFIGURATION status per MODE — NEVER a token value and NEVER a
    resolved secret (Phase 19 discipline; mirrors `plugins/mem0.py`).

    - `ok` when AT LEAST ONE mode is usable, naming which.
    - `degraded` when none is, listing what each mode is missing.
    - `error` only when the check itself fails.

    Per mode, because the three need DIFFERENT things -- `onepasswordconnectsdk`,
    `onepassword-sdk`, or just the `op` binary. This previously returned
    `error: onepasswordconnectsdk not installed` whenever the Connect SDK was
    absent, which called a perfectly working service-account or CLI deployment
    broken -- and a health check that reports a working thing as broken is the
    same defect as one reporting a broken thing as fine.

    The detail carries presence booleans / mode names only — never the token,
    the Connect host URL, or a fetched value. Never raises: any internal error
    is reported as the exception TYPE name only (never a message), matching
    `PluginManager.run_health_check`.
    """
    try:
        from hivepilot.config import settings

        modes = _mode_report(settings)
        usable = [name for name, ok, _ in modes if ok]
        summary = "; ".join(f"{name}: {detail}" for name, _, detail in modes)

        if usable:
            return HealthStatus("ok", f"usable via {', '.join(usable)} ({summary})")
        return HealthStatus("degraded", f"no usable mode ({summary})")
    except Exception as exc:  # noqa: BLE001 — a health check must never crash
        return HealthStatus("error", type(exc).__name__)


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.onepassword_enabled:
        return {}
    return {"secrets": {_PROVIDER: OnePasswordBackend()}, "health": {_PROVIDER: health}}
