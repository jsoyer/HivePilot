"""Verify a model connection BEFORE it is saved (HP-78 / HP-65).

The honest minimum of OpenClaw's "prove the choice can answer before keeping
it": confirm a provider credential / local endpoint / agent session actually
works, without persisting anything and without spending a paid completion.

Three checks, each grounded in what we already have:

- **API providers** — a cheap authenticated `GET /models`. A 200 proves the
  key is accepted by the provider (openai-compatible incl. OpenRouter/Ollama/
  local proxies, plus Anthropic and Google via their own auth shapes). This is
  the credential check, not a generation.
- **Local Ollama** — reuses `ollama_probe` (`GET /v1/models`).
- **CLI agents** (claude/codex/cursor/grok/gemini) — reuses
  `agent_auth.auth_state`: a session is present or not. It never invokes a
  model (the agent-auth doctrine), so the detail says so plainly.

Never raises: a failed check is `ok=False` carrying the reason, so callers
(CLI, onboarding) render a status rather than crash.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

#: Providers reachable through the OpenAI-compatible `GET /models` + Bearer key.
_OPENAI_COMPATIBLE = {"openai", "openrouter", "mistral", "perplexity", "ollama", "local"}

_DEFAULT_BASE_URL = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "mistral": "https://api.mistral.ai/v1",
    "perplexity": "https://api.perplexity.ai",
    "ollama": "http://localhost:11434/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
}

_ENV_KEY = {
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}


@dataclass
class VerifyResult:
    ok: bool
    target: str
    detail: str
    models: list[str] = field(default_factory=list)
    error: str | None = None


def _get_json(url: str, headers: dict[str, str], timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 — explicit https/http
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _resolve_key(provider: str, api_key: str | None) -> str | None:
    if api_key:
        return api_key
    env_name = _ENV_KEY.get(provider)
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    if provider in _OPENAI_COMPATIBLE:
        return os.environ.get("OPENAI_API_KEY")
    return None


def verify_openai_compatible(
    base_url: str, api_key: str | None, *, target: str = "openai", timeout: float = 2.5
) -> VerifyResult:
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        status, data = _get_json(url, headers, timeout)
        models = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
        return VerifyResult(
            ok=status == 200,
            target=target,
            detail=f"HTTP {status} · {len(models)} models",
            models=models,
        )
    except urllib.error.HTTPError as exc:
        return VerifyResult(
            ok=False, target=target, detail=f"HTTP {exc.code} (key rejected?)", error=str(exc)
        )
    except Exception as exc:  # noqa: BLE001 — verify never raises
        return VerifyResult(ok=False, target=target, detail="unreachable", error=str(exc))


def verify_anthropic(api_key: str | None, base_url: str, *, timeout: float = 2.5) -> VerifyResult:
    headers = {"anthropic-version": "2023-06-01"}
    if api_key:
        headers["x-api-key"] = api_key
    try:
        status, data = _get_json(base_url.rstrip("/") + "/models", headers, timeout)
        models = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
        return VerifyResult(
            ok=status == 200,
            target="anthropic",
            detail=f"HTTP {status} · {len(models)} models",
            models=models,
        )
    except urllib.error.HTTPError as exc:
        return VerifyResult(
            ok=False, target="anthropic", detail=f"HTTP {exc.code} (key rejected?)", error=str(exc)
        )
    except Exception as exc:  # noqa: BLE001
        return VerifyResult(ok=False, target="anthropic", detail="unreachable", error=str(exc))


def verify_google(api_key: str | None, base_url: str, *, timeout: float = 2.5) -> VerifyResult:
    if not api_key:
        return VerifyResult(ok=False, target="google", detail="no api key")
    url = f"{base_url.rstrip('/')}/models?key={api_key}"
    try:
        status, data = _get_json(url, {}, timeout)
        models = [
            m["name"] for m in data.get("models", []) if isinstance(m, dict) and m.get("name")
        ]
        return VerifyResult(
            ok=status == 200,
            target="google",
            detail=f"HTTP {status} · {len(models)} models",
            models=models,
        )
    except urllib.error.HTTPError as exc:
        return VerifyResult(
            ok=False, target="google", detail=f"HTTP {exc.code} (key rejected?)", error=str(exc)
        )
    except Exception as exc:  # noqa: BLE001
        return VerifyResult(ok=False, target="google", detail="unreachable", error=str(exc))


def verify_agent(kind: str) -> VerifyResult:
    """Verify a CLI agent's SESSION is present (never invokes a model — the
    agent-auth doctrine). `present` = a stored credential at the vendor's
    default location; not a proof of validity."""
    from hivepilot.services import agent_auth

    state = agent_auth.auth_state(kind)
    return VerifyResult(
        ok=state == "present",
        target=f"agent:{kind}",
        detail=f"session {state} (login state only, not a live model call)",
    )


def verify(
    provider: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = 2.5,
) -> VerifyResult:
    """Verify an API/local provider connection. Dispatches to the right auth
    shape; an unknown provider returns an honest `ok=False`."""
    provider = provider.strip().lower()
    key = _resolve_key(provider, api_key)
    if provider in _OPENAI_COMPATIBLE:
        base = base_url or os.environ.get("OPENAI_BASE_URL") or _DEFAULT_BASE_URL.get(provider)
        if not base:
            return VerifyResult(ok=False, target=provider, detail="no base_url resolved")
        return verify_openai_compatible(base, key, target=provider, timeout=timeout)
    if provider == "anthropic":
        return verify_anthropic(key, base_url or _DEFAULT_BASE_URL["anthropic"], timeout=timeout)
    if provider == "google":
        return verify_google(key, base_url or _DEFAULT_BASE_URL["google"], timeout=timeout)
    return VerifyResult(
        ok=False, target=provider, detail=f"verification not implemented for provider {provider!r}"
    )
