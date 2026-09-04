"""Discover local model daemons already running on this machine (HP-78).

Ollama and LM Studio both speak OpenAI-compatible ``GET /v1/models``. This
module lists what is reachable on loopback — it never persists a choice and
it never fetches a non-loopback URL (the onboarding API is not a proxy).
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

from hivepilot.services.ollama_probe import DEFAULT_BASE_URL as OLLAMA_DEFAULT
from hivepilot.services.ollama_probe import probe_ollama

LMSTUDIO_DEFAULT = "http://127.0.0.1:1234/v1"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

#: Hosts ``POST /v1/models/verify`` may reach besides loopback. The API is
#: not a generic HTTP proxy — an operator-supplied URL outside this set is
#: refused (SSRF).
_VERIFY_CLOUD_HOSTS = {
    "api.openai.com",
    "openrouter.ai",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "api.mistral.ai",
    "api.perplexity.ai",
}


@dataclass
class LocalBackend:
    kind: str
    base_url: str
    reachable: bool
    models: list[str] = field(default_factory=list)
    error: str | None = None


def verify_target_allowed(url: str) -> bool:
    """Loopback or a known public model API — nothing else."""
    if is_loopback_url(url):
        return True
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return host in _VERIFY_CLOUD_HOSTS


def is_loopback_url(url: str) -> bool:
    """True only for http(s) URLs whose host is loopback.

    Anything else (empty host, LAN IP, public hostname) is rejected so the
    discovery/verify API cannot be pointed at an arbitrary internal service.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return host in _LOOPBACK_HOSTS


def _ollama_url() -> str:
    return (
        os.environ.get("HIVEPILOT_OLLAMA_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or OLLAMA_DEFAULT
    )


def _lmstudio_url() -> str:
    return os.environ.get("HIVEPILOT_LMSTUDIO_BASE_URL") or LMSTUDIO_DEFAULT


def _probe_loopback(kind: str, base_url: str) -> LocalBackend:
    if not is_loopback_url(base_url):
        return LocalBackend(
            kind=kind,
            base_url=base_url,
            reachable=False,
            error="refused: discovery only probes loopback",
        )
    result = probe_ollama(base_url)
    return LocalBackend(
        kind=kind,
        base_url=result.base_url,
        reachable=result.reachable,
        models=result.models,
        error=result.error,
    )


def discover() -> list[LocalBackend]:
    """Probe Ollama then LM Studio. Order is stable so the UI can render
    without reshuffling; an unreachable daemon is a row, not an omission."""
    return [
        _probe_loopback("ollama", _ollama_url()),
        _probe_loopback("lmstudio", _lmstudio_url()),
    ]


def cli_sessions() -> list[dict[str, str | bool]]:
    """Reuse existing CLI sign-ins (Codex / Claude / Cursor / Grok / Gemini).

    ``state`` is presence of a credential file, never token validity — same
    contract as ``agent_auth.auth_state``.
    """
    from hivepilot.services import agent_auth

    rows: list[dict[str, str | bool]] = []
    for kind, contract in agent_auth.AUTH_CONTRACTS.items():
        rows.append(
            {
                "kind": kind,
                "state": agent_auth.auth_state(kind),
                "login_available": bool(contract.login_argv),
            }
        )
    return rows


def machine_snapshot() -> dict[str, object]:
    """What is already working on this box — the OpenClaw setup surface."""
    return {
        "local": [asdict(b) for b in discover()],
        "cli": cli_sessions(),
    }
