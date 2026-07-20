from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict

import yaml

from hivepilot.config import Settings, settings
from hivepilot.registry import (
    KNOWN_SECRET_BACKENDS,
    SECRETS_MAP,
    SecretRef,
    SecretsRegistry,
)
from hivepilot.services.config_provenance import register_secret_value
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Secret TTL cache / rotation (Phase 19 follow-up).
#
# SECURITY (deliberate, opt-in tradeoff): caching resolved PLAINTEXT secret
# values in memory. This is OFF by default (`secrets_cache_ttl_seconds == 0`)
# and, when enabled, is:
#   * process-local — a plain module dict, never shared across processes;
#   * NEVER persisted — held only in memory, never written to disk / state DB;
#   * TTL-bounded — an entry older than the configured TTL is discarded and the
#     value re-fetched (so a rotated secret is picked up after expiry);
#   * flushable — `clear_secret_cache()` (and `hivepilot secrets cache-clear`)
#     empty it immediately, forcing a live re-fetch on the next resolution.
# A cached value can never outlive its TTL: every read checks the monotonic
# expiry BEFORE returning, and every hit still registers the value for masking
# so no sink can leak it. When the TTL is 0 the cache path is skipped entirely
# and behaviour is byte-identical to the pre-cache implementation.
#
# `_now` is a monotonic clock indirection so TTL expiry is testable (patch
# `secrets_service._now`) without real sleeps; monotonic time is also immune to
# wall-clock adjustments that could otherwise extend an entry's lifetime.
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
# cache_key -> (monotonic_expiry, plaintext_value)
_SECRET_CACHE: Dict[str, tuple[float, str]] = {}


def _now() -> float:
    return time.monotonic()


def _cache_key(source: str, spec: Dict[str, Any]) -> str:
    """A stable key from (source, sorted spec items). The spec carries only
    non-sensitive locators (env var name, KMS ciphertext, vault path, ...) —
    never a plaintext — and is hashed regardless, so the key leaks nothing."""
    payload = json.dumps(
        {"source": source, "spec": spec}, sort_keys=True, default=str, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def clear_secret_cache() -> None:
    """Drop all cached secret values (rotation flush / process reset / tests)."""
    with _cache_lock:
        _SECRET_CACHE.clear()


class EnvSecretsBackend:
    """Resolve a secret from an environment variable."""

    def resolve(self, ref: SecretRef, settings: Settings) -> str:
        key = ref.spec["key"]
        value = os.environ.get(key)
        if not value:
            raise RuntimeError(f"Environment variable {key} not set for secret")
        return value


class FileSecretsBackend:
    """Resolve a secret by reading a plaintext file."""

    def resolve(self, ref: SecretRef, settings: Settings) -> str:
        path = Path(ref.spec["path"])
        return path.read_text(encoding="utf-8").strip()


class VaultSecretsBackend:
    """Resolve a secret from HashiCorp Vault (KV v2).

    ref.spec must contain:
      path: KV v2 path, e.g. "secret/data/myapp"
      key:  key within the data dict
    """

    def resolve(self, ref: SecretRef, settings: Settings) -> str:
        try:
            import hvac  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError("hvac not installed; run: pip install hvac") from exc

        vault_addr = settings.vault_addr or os.environ.get("HIVEPILOT_VAULT_ADDR")
        vault_token = settings.vault_token or os.environ.get("HIVEPILOT_VAULT_TOKEN")

        if not vault_addr or not vault_token:
            raise ValueError(
                "Vault is not configured: set HIVEPILOT_VAULT_ADDR and "
                "HIVEPILOT_VAULT_TOKEN environment variables (or vault_addr / "
                "vault_token in settings)."
            )

        client = hvac.Client(url=vault_addr, token=vault_token)
        path: str = ref.spec["path"]
        key: str = ref.spec["key"]

        response = client.secrets.kv.v2.read_secret_version(path=path)
        data: dict[str, Any] = response["data"]["data"]
        return data[key]


class SopsSecretsBackend:
    """Resolve a secret by decrypting a SOPS-encrypted file.

    ref.spec must contain:
      file: path to the SOPS-encrypted YAML or JSON file
      key:  top-level key to extract from the decrypted content
    """

    def resolve(self, ref: SecretRef, settings: Settings) -> str:
        import shutil

        if not shutil.which("sops"):
            raise RuntimeError(
                "sops binary not found in PATH; install it from https://github.com/getsops/sops"
            )

        file_path = ref.spec["file"]
        key: str = ref.spec["key"]

        result = subprocess.run(
            ["sops", "-d", file_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"sops decryption failed for {file_path!r}: {result.stderr.strip()}")

        # Try YAML first, then JSON
        try:
            data: dict[str, Any] = yaml.safe_load(result.stdout)
        except yaml.YAMLError:
            data = json.loads(result.stdout)

        if key not in data:
            raise KeyError(f"Key {key!r} not found in decrypted sops file {file_path!r}")

        return str(data[key])


_BUILTIN_SECRETS: Dict[str, Any] = {
    "env": EnvSecretsBackend(),
    "file": FileSecretsBackend(),
    "vault": VaultSecretsBackend(),
    "sops": SopsSecretsBackend(),
}
for _name, _backend in _BUILTIN_SECRETS.items():
    SecretsRegistry.register(_name, _backend)
assert set(_BUILTIN_SECRETS) == set(KNOWN_SECRET_BACKENDS), (
    "hivepilot.services.secrets_service._BUILTIN_SECRETS and "
    "hivepilot.registry.KNOWN_SECRET_BACKENDS have drifted out of sync"
)


class SecretResolver:
    def __init__(self) -> None:
        # Legacy-compatible view: some callers/tests reach into
        # `resolver.resolvers` and the individual `_from_*` methods directly.
        # Both are kept as thin wrappers over the registry-driven backends
        # below so behaviour (including exceptions raised) is byte-identical
        # to the pre-registry implementation.
        self.resolvers: dict[str, Any] = {
            name: getattr(self, f"_from_{name}") for name in KNOWN_SECRET_BACKENDS
        }

    def resolve(self, config: Dict[str, Any]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        ttl = getattr(settings, "secrets_cache_ttl_seconds", 0) or 0
        for name, spec in config.items():
            source = spec.get("source", "env")
            backend = SECRETS_MAP.get(source)
            if backend is None:
                raise ValueError(f"Unknown secret source: {source}")
            if ttl > 0:
                resolved[name] = self._resolve_cached(source, spec, backend, ttl)
            else:
                # TTL disabled (default) — always-live path, byte-identical to
                # the pre-cache implementation (no store, no self-registration;
                # the caller registers the value for masking).
                resolved[name] = backend.resolve(SecretRef(source=source, spec=spec), settings)
        return resolved

    def _resolve_cached(self, source: str, spec: Dict[str, Any], backend: Any, ttl: int) -> str:
        """Return a cached value when still within its TTL, else resolve live
        and store it. Every path — hit OR miss — registers the value for masking
        so a cache hit can never leak past a redaction sink."""
        key = _cache_key(source, spec)
        now = _now()
        with _cache_lock:
            entry = _SECRET_CACHE.get(key)
            if entry is not None and entry[0] > now:
                cached_value = entry[1]
                register_secret_value(cached_value)
                return cached_value
        # Miss or expiry — resolve live OUTSIDE the lock (a backend call can be
        # slow / re-entrant; we never hold the lock across it).
        value = backend.resolve(SecretRef(source=source, spec=spec), settings)
        with _cache_lock:
            _SECRET_CACHE[key] = (now + ttl, value)
        register_secret_value(value)
        return value

    def _from_env(self, spec: Dict[str, Any]) -> str:
        return SECRETS_MAP["env"].resolve(SecretRef(source="env", spec=spec), settings)

    def _from_file(self, spec: Dict[str, Any]) -> str:
        return SECRETS_MAP["file"].resolve(SecretRef(source="file", spec=spec), settings)

    def _from_vault(self, spec: Dict[str, Any]) -> str:
        return SECRETS_MAP["vault"].resolve(SecretRef(source="vault", spec=spec), settings)

    def _from_sops(self, spec: Dict[str, Any]) -> str:
        return SECRETS_MAP["sops"].resolve(SecretRef(source="sops", spec=spec), settings)


secret_resolver = SecretResolver()
