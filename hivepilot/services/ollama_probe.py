"""Local Ollama connectivity probe (HP-73 audit quick-win).

A tiny, dependency-free reachability + model-list check against a local
Ollama daemon's OpenAI-compatible endpoint (`/v1/models`). This is the honest
minimum of the "verify before save" gap (HP-78): rather than only checking the
`ollama` binary is on PATH, it confirms the daemon actually answers and reports
which models are pulled — used by `hivepilot doctor` and available to any
onboarding flow.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field

#: Ollama's OpenAI-compatible base URL (the same one `OllamaRunner` defaults to
#: for `mode: api`). `/models` under it is the OpenAI-format model list.
DEFAULT_BASE_URL = "http://localhost:11434/v1"


def default_base_url() -> str:
    """The endpoint the probe targets: an explicit override wins, else the
    local default. Kept in sync with what `OllamaRunner` uses at dispatch."""
    return (
        os.environ.get("HIVEPILOT_OLLAMA_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL
    )


@dataclass
class OllamaProbe:
    base_url: str
    reachable: bool
    models: list[str] = field(default_factory=list)
    error: str | None = None


def probe_ollama(base_url: str | None = None, *, timeout: float = 1.5) -> OllamaProbe:
    """GET `{base_url}/models` and report reachability + the pulled model ids.

    Never raises — a probe that fails is a `reachable=False` result carrying the
    error string, so callers (doctor, onboarding) can render it as a status
    rather than crash."""
    base = (base_url or default_base_url()).rstrip("/")
    url = f"{base}/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — local http
            payload = json.loads(resp.read().decode("utf-8"))
        models = [
            m["id"]
            for m in payload.get("data", [])
            if isinstance(m, dict) and isinstance(m.get("id"), str)
        ]
        return OllamaProbe(base_url=base, reachable=True, models=models)
    except Exception as exc:  # noqa: BLE001 — reachability probe must never raise
        return OllamaProbe(base_url=base, reachable=False, error=str(exc))
