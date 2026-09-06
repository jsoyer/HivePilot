"""Verify then persist a provider API key (HP-65).

Fails closed: nothing is written unless ``model_verify.verify`` returns
``ok=True``. The only write path is ``setup_wizard_common._env_upsert``
(owner-only ``0600``). The key is never echoed in the result, never stored
in HivePilot YAML, and never logged.

Local daemons (Ollama / LM Studio) have no cloud key to save — use
``model_verify`` / ``POST /v1/models/verify`` instead.

Native OAuth (OpenRouter / OpenAI) and gemini device-code stay out of this
slice.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from hivepilot.config import resolve_env_file_with_provenance
from hivepilot.services import local_models, model_verify
from hivepilot.services.setup_wizard_common import _env_upsert

#: Providers that map to a single env var. Keep in lockstep with
#: ``model_verify._ENV_KEY``.
CONNECT_PROVIDERS = tuple(model_verify._ENV_KEY)


class ConnectError(Exception):
    """Operator-facing refusal (unknown provider, empty key, SSRF, local)."""


@dataclass
class ConnectResult:
    ok: bool
    provider: str
    env_key: str | None
    detail: str
    models: list[str] = field(default_factory=list)
    saved: bool = False
    error: str | None = None


def key_fingerprint(api_key: str) -> str:
    """Short hash for audit rows — never a prefix of the secret itself."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def connect(
    provider: str,
    api_key: str,
    *,
    base_url: str | None = None,
    env_path: Path | str | None = None,
) -> ConnectResult:
    """Verify *api_key* for *provider*, then upsert it into the resolved ``.env``.

    Raises ``ConnectError`` for structural refusals (do not attempt a write).
    A failed live check is ``ok=False`` / ``saved=False`` — still no write.
    """
    provider = (provider or "").strip().lower()
    api_key = (api_key or "").strip()
    if provider in {"ollama", "lmstudio", "local"}:
        raise ConnectError("local providers have no API key to save — use verify")
    env_key = model_verify._ENV_KEY.get(provider)
    if env_key is None:
        raise ConnectError(
            f"unknown provider {provider!r} — expected one of {', '.join(CONNECT_PROVIDERS)}"
        )
    if not api_key:
        raise ConnectError("api_key is required")
    if base_url and not local_models.verify_target_allowed(base_url):
        raise ConnectError("base_url must be loopback or a known model API host")

    checked = model_verify.verify(provider, base_url=base_url, api_key=api_key)
    if not checked.ok:
        return ConnectResult(
            ok=False,
            provider=provider,
            env_key=env_key,
            detail=checked.detail,
            models=checked.models,
            saved=False,
            error=checked.error,
        )

    path = Path(env_path) if env_path is not None else Path(resolve_env_file_with_provenance()[0])
    _env_upsert(path, env_key, api_key)
    os.environ[env_key] = api_key
    return ConnectResult(
        ok=True,
        provider=provider,
        env_key=env_key,
        detail=f"verified · saved {env_key} ({checked.detail})",
        models=checked.models,
        saved=True,
    )
