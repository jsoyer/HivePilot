"""Tests for the first-party ``kms`` secrets-provider plugin (``plugins/kms.py``).

This plugin dogfoods the ``secrets`` plugin provider type (alongside
``runners`` / ``notifiers`` / other secrets backends): its ``register()`` returns
``{"secrets": {"kms": KmsSecretsBackend()}, "health": {"kms": health}}``, loaded
into ``SECRETS_MAP`` under the same fail-closed trust model builtin backends use,
so a pipeline config can reference a value via ``${secret:NAME}`` where NAME's
spec has ``source: kms``.

The KMS backend DECRYPTS operator-provided ciphertext at runtime via the
operator's OWN cloud KMS. Two spec modes:

- **direct**  — ``{"ciphertext": "<b64>"}``: a KMS-encrypted blob decrypted
  straight by the provider Decrypt API; the plaintext IS the secret.
- **envelope** — ``{"encrypted_data_key","ciphertext","iv"[,"tag"]}``:
  KMS-decrypt a small data key, then AES-256-GCM-decrypt a local ciphertext
  with it (via the ``cryptography`` package).

Provider SDKs (``boto3`` / ``google-cloud-kms`` / ``azure-keyvault-keys``) and
``cryptography`` are imported LAZILY and are NOT installed by the plugin — they
are mocked / round-tripped here. On a missing lib/provider, or a decrypt
failure, ``resolve`` raises a clear ``RuntimeError`` naming ONLY the
lib/provider — NEVER a plaintext or data-key value.

Loads the plugin by file path — the same mechanism
``hivepilot.plugins._scan_local_plugins`` uses — so these tests don't depend on
``plugins`` being importable on sys.path (mirrors tests/test_onepassword.py).
"""

from __future__ import annotations

import base64
import importlib.util
import os
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hivepilot.config import settings
from hivepilot.registry import SECRETS_MAP, SecretRef, SecretsRegistry

REPO_ROOT = Path(__file__).parent.parent
KMS_PLUGIN_PATH = REPO_ROOT / "plugins" / "kms.py"

# A distinctive fake plaintext used to prove the plugin NEVER leaks a decrypted
# value into an error message.
_PLAINTEXT = "kms-decrypted-SECRET-should-not-leak-9f3a2b"
# A fixed 32-byte AES-256 data key (envelope mode). Deterministic for round-trip.
_DATA_KEY = b"0123456789abcdef0123456789abcdef"


def _load_kms_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hivepilot_plugin_kms_test", KMS_PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def kms_module() -> ModuleType:
    return _load_kms_module()


@pytest.fixture(autouse=True)
def _kms_settings(monkeypatch: pytest.MonkeyPatch):
    """A configured AWS baseline. Individual tests override to exercise other
    providers / fail-closed paths."""
    monkeypatch.setattr(settings, "kms_enabled", True, raising=False)
    monkeypatch.setattr(settings, "kms_provider", "aws", raising=False)
    monkeypatch.setattr(settings, "kms_key_id", None, raising=False)
    yield


def _mock_boto3(plaintext_bytes: bytes) -> MagicMock:
    """A mock ``boto3`` module whose ``client("kms").decrypt(...)`` returns a
    fake ``{"Plaintext": ...}`` response."""
    boto3 = MagicMock()
    kms_client = MagicMock()
    kms_client.decrypt.return_value = {"Plaintext": plaintext_bytes}
    boto3.client.return_value = kms_client
    return boto3


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _ref(**spec: object) -> SecretRef:
    return SecretRef(source="kms", spec=dict(spec))


def _envelope_spec(plaintext: str, *, split_tag: bool = False) -> dict[str, str]:
    """Encrypt *plaintext* under ``_DATA_KEY`` with AES-256-GCM and build the
    envelope-mode spec fields (data key left as a placeholder ciphertext the
    mocked KMS 'decrypts' back to ``_DATA_KEY``)."""
    iv = os.urandom(12)
    ct_with_tag = AESGCM(_DATA_KEY).encrypt(iv, plaintext.encode("utf-8"), None)
    spec: dict[str, str] = {
        "encrypted_data_key": _b64(b"kms-wrapped-data-key-placeholder"),
        "iv": _b64(iv),
    }
    if split_tag:
        ct, tag = ct_with_tag[:-16], ct_with_tag[-16:]
        spec["ciphertext"] = _b64(ct)
        spec["tag"] = _b64(tag)
    else:
        spec["ciphertext"] = _b64(ct_with_tag)
    return spec


# ---------------------------------------------------------------------------
# register() / health() gating
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_exposes_kms_secrets_backend_and_health(self, kms_module: ModuleType) -> None:
        hooks = kms_module.register()
        assert set(hooks) == {"secrets", "health"}
        assert set(hooks["secrets"]) == {"kms"}
        backend = hooks["secrets"]["kms"]
        assert callable(getattr(backend, "resolve", None))
        assert backend.name == "kms"
        assert hooks["health"]["kms"] is kms_module.health

    def test_register_returns_empty_when_disabled(
        self, kms_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "kms_enabled", False, raising=False)
        assert kms_module.register() == {}

    def test_backend_is_not_a_dataclass(self, kms_module: ModuleType) -> None:
        import dataclasses

        assert not dataclasses.is_dataclass(kms_module.KmsSecretsBackend)


class TestHealth:
    def test_health_reports_provider_availability_without_decrypting(
        self, kms_module: ModuleType
    ) -> None:
        # boto3 present -> aws available; provider aws configured -> ok.
        with patch.object(kms_module, "boto3", MagicMock()):
            result = kms_module.health()
        assert result.status in {"ok", "degraded", "error"}
        # Never decrypts: no boto3 client call is required to report health.

    def test_health_error_when_no_provider_sdk_installed(self, kms_module: ModuleType) -> None:
        with (
            patch.object(kms_module, "boto3", None),
            patch.object(kms_module, "_gcp_kms", None),
            patch.object(kms_module, "_azure_crypto", None),
        ):
            result = kms_module.health()
        assert result.status == "error"

    def test_health_never_raises(
        self, kms_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Boom:
            @property
            def kms_provider(self) -> str:
                raise RuntimeError("boom-should-not-leak")

        monkeypatch.setattr("hivepilot.config.settings", _Boom())
        result = kms_module.health()
        assert result.status == "error"
        assert "boom-should-not-leak" not in result.detail


# ---------------------------------------------------------------------------
# Direct mode
# ---------------------------------------------------------------------------


class TestDirectMode:
    def test_aws_direct_returns_decrypted_plaintext(self, kms_module: ModuleType) -> None:
        backend = kms_module.KmsSecretsBackend()
        boto3 = _mock_boto3(_PLAINTEXT.encode("utf-8"))
        blob = b"aws-kms-encrypted-blob"
        with patch.object(kms_module, "boto3", boto3):
            value = backend.resolve(_ref(provider="aws", ciphertext=_b64(blob)), settings)
        assert value == _PLAINTEXT
        # The raw ciphertext blob was passed to KMS Decrypt.
        _, kwargs = boto3.client.return_value.decrypt.call_args
        assert kwargs["CiphertextBlob"] == blob

    def test_provider_from_settings_when_absent_in_spec(
        self, kms_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "kms_provider", "aws", raising=False)
        backend = kms_module.KmsSecretsBackend()
        boto3 = _mock_boto3(_PLAINTEXT.encode("utf-8"))
        with patch.object(kms_module, "boto3", boto3):
            value = backend.resolve(_ref(ciphertext=_b64(b"blob")), settings)
        assert value == _PLAINTEXT

    def test_non_utf8_plaintext_raises_redacted_error(self, kms_module: ModuleType) -> None:
        """A decrypted plaintext that isn't valid UTF-8 must fail closed via
        `_to_utf8` — the error names only the provider, never the raw bytes,
        and the exception chain is fully severed (`from None`)."""
        backend = kms_module.KmsSecretsBackend()
        non_utf8 = b"\xff\xfe\x00invalid-utf8-bytes"
        boto3 = _mock_boto3(non_utf8)
        with patch.object(kms_module, "boto3", boto3):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(provider="aws", ciphertext=_b64(b"blob")), settings)
        msg = str(excinfo.value)
        assert "kms" in msg
        assert "utf-8" in msg.lower() or "UTF-8" in msg
        # Never leak the raw undecodable bytes / their repr into the message.
        assert repr(non_utf8) not in msg
        assert "\\xff\\xfe" not in msg
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True


# ---------------------------------------------------------------------------
# Envelope mode (round-trip with a real AES-256-GCM encrypt in-test)
# ---------------------------------------------------------------------------


class TestEnvelopeMode:
    def test_envelope_roundtrip_tag_appended(self, kms_module: ModuleType) -> None:
        backend = kms_module.KmsSecretsBackend()
        # KMS 'decrypts' the wrapped data key back to _DATA_KEY.
        boto3 = _mock_boto3(_DATA_KEY)
        spec = _envelope_spec(_PLAINTEXT, split_tag=False)
        with patch.object(kms_module, "boto3", boto3):
            value = backend.resolve(_ref(provider="aws", **spec), settings)
        assert value == _PLAINTEXT

    def test_envelope_roundtrip_tag_separate(self, kms_module: ModuleType) -> None:
        backend = kms_module.KmsSecretsBackend()
        boto3 = _mock_boto3(_DATA_KEY)
        spec = _envelope_spec(_PLAINTEXT, split_tag=True)
        with patch.object(kms_module, "boto3", boto3):
            value = backend.resolve(_ref(provider="aws", **spec), settings)
        assert value == _PLAINTEXT

    def test_tampered_ciphertext_raises_gcm_auth_failure_without_leaking(
        self, kms_module: ModuleType
    ) -> None:
        backend = kms_module.KmsSecretsBackend()
        boto3 = _mock_boto3(_DATA_KEY)
        spec = _envelope_spec(_PLAINTEXT, split_tag=False)
        # Flip a byte in the ciphertext -> GCM tag verification must fail.
        tampered = bytearray(base64.b64decode(spec["ciphertext"]))
        tampered[0] ^= 0xFF
        spec["ciphertext"] = _b64(bytes(tampered))
        with patch.object(kms_module, "boto3", boto3):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(provider="aws", **spec), settings)
        msg = str(excinfo.value)
        assert "kms" in msg
        assert _PLAINTEXT not in msg
        assert base64.b64encode(_DATA_KEY).decode() not in msg
        # Exception chain fully severed — no leaked upstream context.
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True

    def test_envelope_requires_cryptography(self, kms_module: ModuleType) -> None:
        backend = kms_module.KmsSecretsBackend()
        boto3 = _mock_boto3(_DATA_KEY)
        spec = _envelope_spec(_PLAINTEXT)
        with patch.object(kms_module, "boto3", boto3), patch.object(kms_module, "AESGCM", None):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(provider="aws", **spec), settings)
        msg = str(excinfo.value)
        assert "cryptography" in msg
        assert _PLAINTEXT not in msg


# ---------------------------------------------------------------------------
# Fail-closed / anti-leak
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_missing_sdk_raises_naming_provider_and_lib_not_value(
        self, kms_module: ModuleType
    ) -> None:
        backend = kms_module.KmsSecretsBackend()
        with patch.object(kms_module, "boto3", None):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(provider="aws", ciphertext=_b64(b"blob")), settings)
        msg = str(excinfo.value)
        assert "aws" in msg
        assert "boto3" in msg
        assert _PLAINTEXT not in msg

    def test_no_provider_configured_raises(
        self, kms_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "kms_provider", None, raising=False)
        backend = kms_module.KmsSecretsBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(ciphertext=_b64(b"blob")), settings)
        assert "provider" in str(excinfo.value).lower()

    def test_unsupported_provider_raises(self, kms_module: ModuleType) -> None:
        backend = kms_module.KmsSecretsBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(provider="oracle", ciphertext=_b64(b"blob")), settings)
        assert "oracle" in str(excinfo.value)

    def test_missing_ciphertext_and_data_key_raises(self, kms_module: ModuleType) -> None:
        backend = kms_module.KmsSecretsBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(provider="aws"), settings)
        assert "kms" in str(excinfo.value)

    def test_invalid_base64_raises_without_leaking(self, kms_module: ModuleType) -> None:
        backend = kms_module.KmsSecretsBackend()
        boto3 = _mock_boto3(_PLAINTEXT.encode("utf-8"))
        with patch.object(kms_module, "boto3", boto3):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(provider="aws", ciphertext="!!!not-base64!!!"), settings)
        assert "ciphertext" in str(excinfo.value)

    def test_gcp_requires_key_id(
        self, kms_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "kms_key_id", None, raising=False)
        backend = kms_module.KmsSecretsBackend()
        with patch.object(kms_module, "_gcp_kms", MagicMock()):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(provider="gcp", ciphertext=_b64(b"blob")), settings)
        assert "key_id" in str(excinfo.value)

    def test_provider_decrypt_error_is_redacted(self, kms_module: ModuleType) -> None:
        backend = kms_module.KmsSecretsBackend()
        boto3 = MagicMock()
        client = MagicMock()
        client.decrypt.side_effect = RuntimeError(f"upstream boom leaking {_PLAINTEXT}")
        boto3.client.return_value = client
        with patch.object(kms_module, "boto3", boto3):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(provider="aws", ciphertext=_b64(b"blob")), settings)
        msg = str(excinfo.value)
        assert "kms" in msg
        assert _PLAINTEXT not in msg
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True


# ---------------------------------------------------------------------------
# Masking: a KMS-resolved plaintext flows into the value-masking registry when
# resolved through the (opt-in) TTL cache at the SecretResolver layer.
# ---------------------------------------------------------------------------


class TestMaskingRegistration:
    def test_kms_plaintext_registered_for_masking_via_resolver_cache(
        self, kms_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot.services import secrets_service
        from hivepilot.services.config_provenance import (
            clear_secret_values,
            redact_text,
            registered_secret_values,
        )

        secrets_service.clear_secret_cache()
        clear_secret_values()
        # Opt in to the TTL cache so the resolver registers resolved values.
        monkeypatch.setattr(settings, "secrets_cache_ttl_seconds", 300, raising=False)

        backend = kms_module.KmsSecretsBackend()
        SecretsRegistry.register("kms", backend, override=True)
        boto3 = _mock_boto3(_PLAINTEXT.encode("utf-8"))
        try:
            with patch.object(kms_module, "boto3", boto3):
                out = secrets_service.secret_resolver.resolve(
                    {"S": {"source": "kms", "provider": "aws", "ciphertext": _b64(b"blob")}}
                )
            assert out["S"] == _PLAINTEXT
            assert _PLAINTEXT in registered_secret_values()
            assert redact_text(f"leak: {_PLAINTEXT}") == "leak: REDACTED"
        finally:
            secrets_service.clear_secret_cache()
            clear_secret_values()
            SECRETS_MAP.pop("kms", None)


# ---------------------------------------------------------------------------
# PluginManager load + collision (parity with test_onepassword.py's
# TestPluginManagerRegistersOnePassword).
# ---------------------------------------------------------------------------


class TestPluginManagerRegistersKms:
    def test_plugin_manager_registers_kms_into_secrets_map(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert "kms" in SECRETS_MAP
        assert callable(getattr(SECRETS_MAP["kms"], "resolve", None))
        assert any(r.source == "local-file" and r.name == "kms" for r in pm.loaded)

    def test_plugin_manager_skips_kms_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "kms_enabled", False, raising=False)

        plugins_mod.PluginManager()

        # register() early-returned {} → no secrets backend registered.
        assert "kms" not in SECRETS_MAP

    def test_kms_does_not_collide_with_onepassword_or_builtins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import KNOWN_SECRET_BACKENDS

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)

        plugins_mod.PluginManager()

        # All plugin providers coexist alongside every builtin backend.
        assert "kms" in SECRETS_MAP
        assert "onepassword" in SECRETS_MAP
        for builtin in KNOWN_SECRET_BACKENDS:
            assert builtin in SECRETS_MAP
        assert SECRETS_MAP["kms"] is not SECRETS_MAP["onepassword"]

    def test_name_collision_with_kms_aborts(self, kms_module: ModuleType) -> None:
        """A second backend registering under `kms` is rejected by the
        fail-closed trust model (SecretsBackendCollisionError)."""
        from hivepilot.registry import SecretsBackendCollisionError

        SecretsRegistry.register("kms", kms_module.KmsSecretsBackend())
        try:

            class _Other:
                def resolve(self, ref: SecretRef, s: object) -> str:
                    return "other"

            with pytest.raises(SecretsBackendCollisionError):
                SecretsRegistry.register("kms", _Other())
        finally:
            SECRETS_MAP.pop("kms", None)
