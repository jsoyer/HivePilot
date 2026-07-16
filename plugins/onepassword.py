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

from typing import Any

from hivepilot.config import Settings
from hivepilot.plugins import HealthStatus
from hivepilot.registry import SecretRef
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

try:
    from onepasswordconnectsdk.client import new_client
except ImportError:  # onepasswordconnectsdk is optional — never installed by this plugin
    new_client = None  # type: ignore[assignment,misc]

# Provider name — the key this backend registers under in SECRETS_MAP and the
# only identifier (besides the reference identity) ever surfaced in an error.
_PROVIDER = "onepassword"


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
        vault, item, field = _parse_ref(ref)
        identity = f"op://{vault}/{item}/{field}"

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
    """Report 1Password Connect CONFIGURATION status only — NEVER a token
    value and NEVER a resolved secret (Phase 19 discipline; mirrors
    `plugins/mem0.py`'s `health()`):

    - `error` when the `onepasswordconnectsdk` package isn't importable.
    - `degraded` ("not configured") when the required connection config
      (Connect host + a Connect or service-account token) is incomplete.
    - `ok` ("configured") when the SDK is importable AND that config is present.

    The detail carries presence booleans / mode names only — never the token,
    the Connect host URL, or a fetched value. Never raises: any internal error
    is reported as the exception TYPE name only (never a message), matching
    `PluginManager.run_health_check`.
    """
    try:
        if new_client is None:
            return HealthStatus("error", "onepasswordconnectsdk not installed")

        from hivepilot.config import settings

        host = settings.op_connect_host
        token = settings.op_connect_token or settings.op_service_account_token
        if host and token:
            return HealthStatus("ok", "configured")
        return HealthStatus("degraded", "not configured")
    except Exception as exc:  # noqa: BLE001 — a health check must never crash
        return HealthStatus("error", type(exc).__name__)


def register() -> dict[str, Any]:
    return {"secrets": {_PROVIDER: OnePasswordBackend()}, "health": {_PROVIDER: health}}
