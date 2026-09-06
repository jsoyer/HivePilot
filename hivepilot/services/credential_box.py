"""HP-58: encrypt MCP/OpenAPI credentials at rest.

Uses Fernet with a key derived from ``settings.credentials_key``. When the
key is unset, literals cannot be stored (callers keep using ``${env:}`` /
``${secret:}`` refs). GET APIs never return plaintext — only a presence flag.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from hivepilot.config import settings


class CredentialBoxError(RuntimeError):
    """Missing key or corrupt ciphertext."""


def _fernet():
    key = (settings.credentials_key or "").strip()
    if not key:
        raise CredentialBoxError("HIVEPILOT_CREDENTIALS_KEY is not set")
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover — dev extra installs it
        raise CredentialBoxError("cryptography is required to store credentials") from exc
    material = base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf-8")).digest())
    return Fernet(material)


def can_encrypt() -> bool:
    return bool((settings.credentials_key or "").strip())


def encrypt_secret_map(values: dict[str, str]) -> str:
    payload = json.dumps(values, sort_keys=True, ensure_ascii=False).encode("utf-8")
    token: bytes = _fernet().encrypt(payload)
    return token.decode("ascii")


def decrypt_secret_map(ciphertext: str | None) -> dict[str, str]:
    if not ciphertext:
        return {}
    try:
        raw = _fernet().decrypt(ciphertext.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CredentialBoxError("could not decrypt credentials") from exc
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def has_ciphertext(value: Any) -> bool:
    return bool(value)
