"""infisical plugin — a first-party `secrets` provider backed by Infisical.

`Infisical <https://infisical.com>`_ is an open-source, self-hostable
configuration / value store. This plugin lets a pipeline config reference a
value stored in Infisical (``${secret:NAME}`` where NAME's spec has
``source: infisical``) instead of inlining it. It dogfoods the THIRD plugin
provider type — ``secrets`` — alongside ``runners`` / ``notifiers``: its
``register()`` returns ``{"secrets": {"infisical": InfisicalBackend()}}``,
which ``hivepilot.plugins.PluginManager`` loads into
``hivepilot.registry.SECRETS_MAP`` under the fail-closed trust model (a name
that collides with a builtin or another plugin aborts the load).

**Step 0 findings (investigated before writing this plugin):**

- ``SecretsBackend`` (``hivepilot/registry.py``) is a structural
  ``Protocol`` with a single method
  ``resolve(self, ref: SecretRef, settings: Settings) -> str``. ``SecretRef``
  is a frozen dataclass carrying ``source: str`` and ``spec: dict[str, Any]``.
- Builtin backends (``hivepilot/services/secrets_service.py``) read their
  per-secret parameters off ``ref.spec`` by key: ``EnvSecretsBackend`` uses
  ``ref.spec["key"]``, ``FileSecretsBackend`` uses ``ref.spec["path"]``,
  ``VaultSecretsBackend`` uses ``ref.spec["path"]`` + ``ref.spec["key"]`` and
  reads its connection config off ``settings`` (``settings.vault_addr`` /
  ``settings.vault_token``), raising ``ValueError``/``RuntimeError`` with the
  reference identity — never the fetched value — when config is missing. This
  backend mirrors that exact shape: ``ref.spec["key"]`` names the value to
  fetch; ``ref.spec`` may override ``environment`` / ``path`` /
  ``workspace_id`` (a.k.a. ``project_id``) per-secret; connection config comes
  from ``settings.infisical_*``.

**Lazy optional import & graceful degradation.** The Infisical Python SDK
(``pip install infisicalsdk``) is NOT a hivepilot dependency and is
deliberately never installed by this plugin — imported lazily so the plugin
loads (and only fails at *resolve* time) when the library is absent. When the
SDK is missing, config is incomplete, or the client errors, ``resolve`` raises
a clear ``RuntimeError`` naming ONLY the secret key + the provider name
(``infisical``) — NEVER the fetched value — so the pipeline's ``closed``
fail-mode aborts.

**SDK import/method names (assumption — see note).** The exact SDK surface is
NOT pinned by this optional integration (``infisicalsdk`` is never installed
here). This plugin targets the modern SDK: ``from infisical_sdk import
InfisicalSDKClient``, constructed with ``host=`` (self-host base URL, omitted
to use the hosted default) + ``token=``, and fetched via
``client.secrets.get_secret_by_name(secret_name=..., project_id=...,
environment_slug=..., secret_path=...)`` returning an object whose value is on
``.secretValue`` (``.secret_value`` is also accepted as a fallback). If the
real API differs, ``_fetch`` degrades: any SDK exception is caught and
re-raised as the redacted, name-only error described above — never leaking a
value or the raw upstream message.

**Deliberately NOT a ``@dataclass``:** local-file plugins are loaded via
``importlib.util.spec_from_file_location()`` / ``exec_module()``
(``hivepilot.plugins._scan_local_plugins``), which never registers the module
in ``sys.modules``. Combined with ``from __future__ import annotations`` that
trips a real CPython 3.14 ``dataclasses`` bug — see ``plugins/mem0.py`` /
``plugins/rtk.py`` for the full write-up. Plain classes sidestep it entirely.

Configured via ``HIVEPILOT_INFISICAL_*`` (``hivepilot/config.py``):
``infisical_url`` (self-host base URL; unset -> SDK default / hosted),
``infisical_token``, ``infisical_workspace_id``, ``infisical_environment``.
"""

from __future__ import annotations

from typing import Any

from hivepilot.config import Settings
from hivepilot.plugins import HealthStatus
from hivepilot.registry import SecretRef
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

try:
    from infisical_sdk import InfisicalSDKClient
except ImportError:  # infisicalsdk is optional — never installed by this plugin
    InfisicalSDKClient = None  # type: ignore[assignment,misc]

# Provider name — the key this backend registers under in SECRETS_MAP and the
# only identifier (besides the secret key) ever surfaced in an error message.
_PROVIDER = "infisical"


def _extract_secret_value(secret: Any) -> str | None:
    """Best-effort extraction of the string value from an SDK response.

    Tolerant of SDK signature drift: reads ``.secretValue`` (modern SDK), then
    ``.secret_value``, then dict forms, then a bare string. Returns ``None``
    (rather than raising) when no string value is found, so the caller emits a
    single redacted error.
    """
    for attr in ("secretValue", "secret_value"):
        val = getattr(secret, attr, None)
        if isinstance(val, str):
            return val
    if isinstance(secret, dict):
        for key in ("secretValue", "secret_value"):
            val = secret.get(key)
            if isinstance(val, str):
                return val
    if isinstance(secret, str):
        return secret
    return None


class InfisicalBackend:
    """Resolve a secret stored in Infisical.

    ``ref.spec`` keys (mirrors the builtin backends' spec-reading):
      key:          (required) the Infisical secret name to fetch
      environment:  (optional) environment slug override (else settings)
      path:         (optional) secret path (default "/")
      workspace_id: (optional, a.k.a. ``project_id``) project id override
    """

    name = _PROVIDER

    def resolve(self, ref: SecretRef, settings: Settings) -> str:
        key = ref.spec.get("key")
        if not key:
            raise RuntimeError(f"{_PROVIDER} secret is missing the required spec field 'key'")

        if InfisicalSDKClient is None:
            raise RuntimeError(
                f"{_PROVIDER} secret {key!r} cannot be resolved: the "
                "'infisicalsdk' package is not installed (pip install infisicalsdk)"
            )

        token = settings.infisical_token
        workspace_id = (
            ref.spec.get("workspace_id")
            or ref.spec.get("project_id")
            or settings.infisical_workspace_id
        )
        environment = ref.spec.get("environment") or settings.infisical_environment
        secret_path = ref.spec.get("path", "/")

        missing = [
            env_name
            for env_name, value in (
                ("HIVEPILOT_INFISICAL_TOKEN", token),
                ("HIVEPILOT_INFISICAL_WORKSPACE_ID", workspace_id),
                ("HIVEPILOT_INFISICAL_ENVIRONMENT", environment),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"{_PROVIDER} secret {key!r} is not configured: missing "
                f"{', '.join(missing)} (or the matching ref.spec override)"
            )

        return self._fetch(
            settings,
            token=token,
            key=key,
            workspace_id=workspace_id,
            environment=environment,
            secret_path=secret_path,
        )

    def _build_client(self, settings: Settings, token: str) -> Any:
        kwargs: dict[str, Any] = {"token": token}
        if settings.infisical_url:
            # Self-host base URL; omitted entirely to use the SDK/hosted default.
            kwargs["host"] = settings.infisical_url
        return InfisicalSDKClient(**kwargs)

    def _fetch(
        self,
        settings: Settings,
        *,
        token: str,
        key: str,
        workspace_id: str,
        environment: str,
        secret_path: str,
    ) -> str:
        # Client construction is INSIDE this try too: modern SDK clients often
        # authenticate at construction time, and a construction failure can
        # embed the token (or, in principle, a value) in its message -- the
        # exact leak this redact-and-reraise boundary exists to prevent. Never
        # let `_build_client` run outside this guard.
        try:
            client = self._build_client(settings, token)
            secret = client.secrets.get_secret_by_name(
                secret_name=key,
                project_id=workspace_id,
                environment_slug=environment,
                secret_path=secret_path,
            )
        except Exception as exc:
            # Re-raise with the reference identity + provider ONLY. `from None`
            # + no str(exc) guarantees neither the token, the fetched value,
            # nor a raw upstream message (which could echo either) leaks --
            # and severs `__context__` so a caller logging `exc.__cause__` /
            # walking the exception chain can't resurface the original error.
            logger.warning(
                "plugin.infisical.fetch_failed",
                provider=_PROVIDER,
                secret=key,
                error_type=type(exc).__name__,
            )
            raise RuntimeError(
                f"{_PROVIDER} failed to resolve secret {key!r} ({type(exc).__name__})"
            ) from None

        value = _extract_secret_value(secret)
        if not value:
            # Fail-closed on an empty/whitespace value too (mirrors
            # EnvSecretsBackend's `if not value: raise`) -- an empty string is
            # a truthy `is not None` check but is never a legitimate secret.
            raise RuntimeError(f"{_PROVIDER} returned no usable value for secret {key!r}")
        return value


def health(**kwargs: Any) -> HealthStatus:
    """Report Infisical CONFIGURATION status only — NEVER a token/endpoint
    value and NEVER a resolved secret (Phase 19 discipline; mirrors
    `plugins/mem0.py`'s `health()`):

    - `error` when the `infisicalsdk` package isn't importable (lib missing).
    - `degraded` ("not configured") when the required connection config
      (token + workspace id + environment) is incomplete.
    - `ok` ("configured") when the SDK is importable AND that config is present.

    The detail carries presence booleans / mode names only — never the token,
    URL, workspace id, or a fetched value. Never raises: any internal error is
    reported as the exception TYPE name only (never a message), matching
    `PluginManager.run_health_check`.
    """
    try:
        if InfisicalSDKClient is None:
            return HealthStatus("error", "infisicalsdk not installed")

        from hivepilot.config import settings

        configured = bool(
            settings.infisical_token
            and settings.infisical_workspace_id
            and settings.infisical_environment
        )
        if configured:
            return HealthStatus("ok", "configured")
        return HealthStatus("degraded", "not configured")
    except Exception as exc:  # noqa: BLE001 — a health check must never crash
        return HealthStatus("error", type(exc).__name__)


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.infisical_enabled:
        return {}
    return {"secrets": {_PROVIDER: InfisicalBackend()}, "health": {_PROVIDER: health}}
