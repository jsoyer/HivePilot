"""kms plugin — a first-party ``secrets`` provider backed by a cloud KMS.

This plugin lets a pipeline config reference a value that is stored as
operator-provided CIPHERTEXT and decrypted at runtime via the operator's OWN
cloud KMS (``${secret:NAME}`` where NAME's spec has ``source: kms``) instead of
inlining a plaintext. It is a structural sibling of ``plugins/infisical.py`` /
``plugins/onepassword.py`` — same ``secrets`` plugin provider type, same
fail-closed trust model: its ``register()`` returns
``{"secrets": {"kms": KmsSecretsBackend()}, "health": {"kms": health}}``, which
``hivepilot.plugins.PluginManager`` loads into
``hivepilot.registry.SECRETS_MAP`` (a name that collides with a builtin or
another plugin aborts the load).

**Two spec modes (auto-detected by which keys ``ref.spec`` carries):**

- **direct** — ``ref.spec = {"ciphertext": "<base64>"}``: the base64 is a
  KMS-encrypted blob (≤4KB). Decrypted directly via the provider's Decrypt API;
  the returned UTF-8 plaintext IS the secret.
- **envelope** — ``ref.spec`` has ``{"encrypted_data_key": "<b64>",
  "ciphertext": "<b64>", "iv": "<b64>"[, "tag": "<b64>"]}``: the provider
  KMS-decrypts the small wrapped data key, then the local ciphertext is
  AES-256-GCM-decrypted with it (``cryptography``). ``tag`` is optional — when
  absent, the GCM tag is assumed appended to ``ciphertext`` (the layout
  ``AESGCM.encrypt`` produces).

**Three providers** (``ref.spec["provider"]`` or ``settings.kms_provider``):

- ``aws``   — ``boto3.client("kms").decrypt(CiphertextBlob=...)`` (the
  ``[cloud]`` extra; ``key_id`` is embedded in the ciphertext for direct mode).
- ``gcp``   — ``google.cloud.kms`` ``decrypt(name=key_id, ciphertext=...)``.
- ``azure`` — ``azure.keyvault.keys.crypto`` ``CryptographyClient(key_id,
  cred).decrypt(...)``.

``key_id`` comes from ``ref.spec["key_id"]`` or ``settings.kms_key_id``
(required for gcp/azure; not required for aws direct mode).

**Lazy optional imports & graceful degradation.** ``boto3`` /
``google-cloud-kms`` / ``azure-keyvault-keys`` + ``azure-identity`` and
``cryptography`` are imported LAZILY and are NOT installed by this plugin —
imported so the plugin loads (and only fails at *resolve* time) when a library
is absent. A missing provider SDK / a missing ``cryptography`` / a decrypt
failure raises a clear ``RuntimeError`` naming ONLY the provider + library (and
never a plaintext, data-key, or raw upstream message) so the pipeline's
``closed`` fail-mode aborts.

**Anti-leak discipline (CRITICAL).** A decrypted plaintext or data key is NEVER
logged, returned in an error, or otherwise surfaced. Every error names only
config / lib / provider / spec-field. The returned plaintext is registered for
masking by the CALLER (``SecretResolver`` / orchestrator / cli / secret_refs),
exactly like every other backend — this plugin does not bypass that.

**Deliberately NOT a ``@dataclass``:** local-file plugins are loaded via
``importlib.util.spec_from_file_location()`` / ``exec_module()``
(``hivepilot.plugins._scan_local_plugins``), which never registers the module in
``sys.modules``. Combined with ``from __future__ import annotations`` that trips
a real CPython 3.14 ``dataclasses`` bug — see ``plugins/infisical.py`` /
``plugins/mem0.py`` for the full write-up. Plain classes sidestep it entirely.

Configured via ``HIVEPILOT_KMS_*`` (``hivepilot/config.py``): ``kms_enabled``,
``kms_provider``, ``kms_key_id``.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from hivepilot.config import Settings
from hivepilot.plugins import HealthStatus
from hivepilot.registry import SecretRef
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# --- Lazy optional provider SDKs (never installed by this plugin) ----------
try:
    import boto3  # AWS KMS — also in the [cloud] extra
except ImportError:
    boto3 = None  # type: ignore[assignment]

try:
    from google.cloud import kms as _gcp_kms  # GCP Cloud KMS
except ImportError:
    _gcp_kms = None  # type: ignore[assignment]

try:
    from azure.identity import DefaultAzureCredential as _AzureCredential
    from azure.keyvault.keys.crypto import CryptographyClient as _azure_crypto
    from azure.keyvault.keys.crypto import EncryptionAlgorithm as _AzureAlgo
except ImportError:
    _azure_crypto = None  # type: ignore[assignment,misc]
    _AzureCredential = None  # type: ignore[assignment,misc]
    _AzureAlgo = None  # type: ignore[assignment,misc]

# --- Lazy AES-256-GCM for envelope mode ------------------------------------
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    AESGCM = None  # type: ignore[assignment,misc]

# Provider name — the key this backend registers under in SECRETS_MAP and the
# only identifier (besides the provider/lib/spec-field) ever surfaced in an error.
_PROVIDER = "kms"
_SUPPORTED = ("aws", "gcp", "azure")


def _b64decode_field(value: Any, field_name: str) -> bytes:
    """Decode a required base64 spec field, raising a redacted error (naming
    only the field) on absence or malformed input."""
    if not isinstance(value, str) or not value:
        raise RuntimeError(
            f"{_PROVIDER} secret spec field {field_name!r} must be a non-empty base64 string"
        )
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise RuntimeError(
            f"{_PROVIDER} secret spec field {field_name!r} is not valid base64"
        ) from None


class KmsSecretsBackend:
    """Resolve a secret by decrypting operator-provided ciphertext via a cloud KMS.

    ``ref.spec`` keys:
      provider:            (optional) "aws" | "gcp" | "azure" (else settings.kms_provider)
      key_id:              (optional) KMS key id/resource name (else settings.kms_key_id;
                           required for gcp/azure)
      ciphertext:          (required) base64 — a KMS-encrypted blob (direct mode) OR the
                           AES-GCM ciphertext (envelope mode)
      encrypted_data_key:  (envelope) base64 — the KMS-wrapped AES-256 data key
      iv:                  (envelope) base64 — the AES-GCM nonce
      tag:                 (envelope, optional) base64 — the GCM tag (else assumed
                           appended to `ciphertext`)
    """

    name = _PROVIDER

    def resolve(self, ref: SecretRef, settings: Settings) -> str:
        spec = ref.spec
        provider = spec.get("provider") or settings.kms_provider
        if not provider:
            raise RuntimeError(
                f"{_PROVIDER} secret is not configured: no provider set "
                "(spec 'provider' or HIVEPILOT_KMS_PROVIDER; one of aws/gcp/azure)"
            )
        provider = str(provider).lower()
        if provider not in _SUPPORTED:
            raise RuntimeError(
                f"{_PROVIDER} provider {provider!r} is not supported "
                f"(expected one of {', '.join(_SUPPORTED)})"
            )
        key_id = spec.get("key_id") or settings.kms_key_id

        # Envelope mode is selected when a wrapped data key is present; else
        # direct mode (the ciphertext itself is KMS-decrypted).
        if spec.get("encrypted_data_key"):
            plaintext = self._resolve_envelope(provider, spec, key_id)
        elif spec.get("ciphertext"):
            plaintext = self._resolve_direct(provider, spec, key_id)
        else:
            raise RuntimeError(
                f"{_PROVIDER} secret is missing required spec fields: provide 'ciphertext' "
                "(direct mode) or 'encrypted_data_key' + 'ciphertext' + 'iv' (envelope mode)"
            )
        return _to_utf8(plaintext)

    # -- modes --------------------------------------------------------------
    def _resolve_direct(self, provider: str, spec: dict[str, Any], key_id: str | None) -> bytes:
        blob = _b64decode_field(spec.get("ciphertext"), "ciphertext")
        return self._kms_decrypt(provider, blob, key_id)

    def _resolve_envelope(self, provider: str, spec: dict[str, Any], key_id: str | None) -> bytes:
        if AESGCM is None:
            raise RuntimeError(
                "cryptography package required for KMS envelope mode — pip install hivepilot[kms]"
            )
        wrapped_key = _b64decode_field(spec.get("encrypted_data_key"), "encrypted_data_key")
        ciphertext = _b64decode_field(spec.get("ciphertext"), "ciphertext")
        iv = _b64decode_field(spec.get("iv"), "iv")
        tag_field = spec.get("tag")
        # AESGCM.decrypt expects ciphertext||tag; append the tag when supplied
        # separately, otherwise assume it is already appended.
        ct_with_tag = ciphertext + _b64decode_field(tag_field, "tag") if tag_field else ciphertext

        data_key = self._kms_decrypt(provider, wrapped_key, key_id)
        try:
            return AESGCM(data_key).decrypt(iv, ct_with_tag, None)
        except Exception as exc:
            # A GCM authentication failure (tampered ciphertext / wrong key)
            # must leak nothing — not the data key, not partial plaintext, not
            # the upstream message. `from None` severs the chain too.
            logger.warning(
                "plugin.kms.envelope_decrypt_failed",
                provider=provider,
                error_type=type(exc).__name__,
            )
            raise RuntimeError(
                f"{_PROVIDER} envelope decryption failed (AES-256-GCM authentication)"
            ) from None

    # -- provider dispatch --------------------------------------------------
    def _kms_decrypt(self, provider: str, blob: bytes, key_id: str | None) -> bytes:
        # SDK-availability / required-config checks run BEFORE the redaction
        # boundary so their clear (value-free) messages are not collapsed into
        # the generic decrypt-failed error below.
        if provider == "aws":
            if boto3 is None:
                raise RuntimeError(
                    f"{_PROVIDER} provider 'aws' requires boto3 — pip install hivepilot[cloud]"
                )
        elif provider == "gcp":
            if _gcp_kms is None:
                raise RuntimeError(
                    f"{_PROVIDER} provider 'gcp' requires google-cloud-kms "
                    "— pip install hivepilot[kms]"
                )
            if not key_id:
                raise RuntimeError(
                    f"{_PROVIDER} provider 'gcp' requires a key_id "
                    "(spec 'key_id' or HIVEPILOT_KMS_KEY_ID)"
                )
        else:  # azure
            if _azure_crypto is None:
                raise RuntimeError(
                    f"{_PROVIDER} provider 'azure' requires azure-keyvault-keys + azure-identity "
                    "— pip install hivepilot[kms]"
                )
            if not key_id:
                raise RuntimeError(
                    f"{_PROVIDER} provider 'azure' requires a key_id "
                    "(spec 'key_id' or HIVEPILOT_KMS_KEY_ID)"
                )

        try:
            if provider == "aws":
                return self._aws_decrypt(blob, key_id)
            if provider == "gcp":
                return self._gcp_decrypt(blob, key_id)
            return self._azure_decrypt(blob, key_id)
        except Exception as exc:
            # Re-raise naming provider + error TYPE ONLY. `from None` + no
            # str(exc) guarantees neither a plaintext, a data key, nor a raw
            # upstream message (which could echo either) leaks.
            logger.warning(
                "plugin.kms.decrypt_failed",
                provider=provider,
                error_type=type(exc).__name__,
            )
            raise RuntimeError(
                f"{_PROVIDER} failed to decrypt via provider {provider!r} ({type(exc).__name__})"
            ) from None

    def _aws_decrypt(self, blob: bytes, key_id: str | None) -> bytes:
        client = boto3.client("kms")
        kwargs: dict[str, Any] = {"CiphertextBlob": blob}
        if key_id:
            # Optional for symmetric keys (key id is embedded in the blob) but
            # honoured when the operator pins it.
            kwargs["KeyId"] = key_id
        response = client.decrypt(**kwargs)
        return response["Plaintext"]

    def _gcp_decrypt(self, blob: bytes, key_id: str | None) -> bytes:
        client = _gcp_kms.KeyManagementServiceClient()
        response = client.decrypt(request={"name": key_id, "ciphertext": blob})
        return response.plaintext

    def _azure_decrypt(self, blob: bytes, key_id: str | None) -> bytes:
        credential = _AzureCredential()
        client = _azure_crypto(key_id, credential)
        result = client.decrypt(_AzureAlgo.rsa_oaep_256, blob)
        return result.plaintext


def _to_utf8(plaintext: bytes) -> str:
    """Decode a decrypted plaintext to UTF-8, failing closed (never returning a
    partial / lossy value) and never echoing the bytes on error."""
    if not isinstance(plaintext, (bytes, bytearray)):
        raise RuntimeError(f"{_PROVIDER} decrypt returned a non-bytes plaintext")
    try:
        value = bytes(plaintext).decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeError(f"{_PROVIDER} decrypted plaintext is not valid UTF-8") from None
    if not value:
        # An empty plaintext is a `str` but never a legitimate secret (mirrors
        # EnvSecretsBackend's `if not value: raise`).
        raise RuntimeError(f"{_PROVIDER} decrypt returned an empty plaintext")
    return value


def health(**kwargs: Any) -> HealthStatus:
    """Report KMS lib/provider AVAILABILITY only — NEVER decrypts anything and
    NEVER surfaces a key id or a value (Phase 19 discipline; mirrors
    ``plugins/infisical.py``'s ``health()``):

    - ``error`` when NO provider SDK is importable (nothing can decrypt).
    - ``degraded`` when a provider is configured but its SDK isn't installed,
      or when no provider is configured yet.
    - ``ok`` when the configured provider's SDK is importable.

    Detail carries provider/lib names only. Never raises: any internal error is
    reported as the exception TYPE name only.
    """
    try:
        available = []
        if boto3 is not None:
            available.append("aws")
        if _gcp_kms is not None:
            available.append("gcp")
        if _azure_crypto is not None:
            available.append("azure")

        if not available:
            return HealthStatus("error", "no KMS provider SDK installed (aws/gcp/azure)")

        from hivepilot.config import settings

        provider = settings.kms_provider
        if provider:
            provider = str(provider).lower()
            if provider in available:
                return HealthStatus("ok", f"provider '{provider}' available")
            return HealthStatus("degraded", f"provider '{provider}' SDK not installed")
        return HealthStatus(
            "degraded", f"available: {', '.join(available)}; no provider configured"
        )
    except Exception as exc:  # noqa: BLE001 — a health check must never crash
        return HealthStatus("error", type(exc).__name__)


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.kms_enabled:
        return {}
    return {"secrets": {_PROVIDER: KmsSecretsBackend()}, "health": {_PROVIDER: health}}
