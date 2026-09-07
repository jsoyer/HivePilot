"""OpenCodex (``ocx``) is a local *provider proxy*, not a HivePilot runner.

It is not the OpenAI Codex CLI (``codex`` / ``kind: codex``) and not the
OpenCode CLI (``opencode`` / ``kind: opencode``). This module only reports
whether the ``ocx`` binary is on PATH and whether a loopback proxy answers.
It never writes ``~/.codex/config.toml``, never registers a runner kind, and
never treats a Codex CLI session as OpenCodex.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from hivepilot.services.local_models import is_loopback_url

#: Documented default bind for ``ocx start`` (opencodex.me).
DEFAULT_BASE_URL = "http://127.0.0.1:10100"
_KIND = "opencodex"
_BINARY = "ocx"


def default_base_url() -> str:
    return os.environ.get("HIVEPILOT_OPENCODEX_BASE_URL") or DEFAULT_BASE_URL


def openai_compat_base(base_url: str | None = None) -> str:
    """``…/v1`` root for chat-completions / models. Never a Codex CLI path."""
    root = (base_url or default_base_url()).strip().rstrip("/") or DEFAULT_BASE_URL
    if root.endswith("/v1"):
        return root
    return root + "/v1"


def _binary_present() -> bool:
    return shutil.which(_BINARY) is not None


def _http_reachable(base_url: str, *, timeout: float = 1.5) -> tuple[bool, str | None]:
    """Loopback liveness only. Tries ``/health`` then ``/v1/models``.

    A 401/403 still means a process answered — the proxy is up. Connection
    errors are ``reachable=False``. Non-loopback URLs are refused (same
    contract as local-model discovery).
    """
    if not is_loopback_url(base_url):
        return False, "refused: discovery only probes loopback"
    root = base_url.rstrip("/")
    # Strip a trailing /v1 so we don't probe /v1/health by accident.
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    last_error: str | None = None
    for path in ("/health", "/v1/models"):
        url = root + path
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                if 200 <= getattr(resp, "status", 200) < 500:
                    return True, None
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 404}:
                # 404 on one path: try the next. 401/403 = process is up.
                if exc.code in {401, 403}:
                    return True, None
                last_error = f"HTTP {exc.code}"
                continue
            return False, f"HTTP {exc.code}"
        except OSError as exc:
            last_error = str(exc)
            break
    return False, last_error


def _list_models(base_url: str, *, timeout: float) -> list[str]:
    """Loopback ``GET /v1/models`` only. Empty on any failure — never raises."""
    if not is_loopback_url(base_url):
        return []
    url = openai_compat_base(base_url) + "/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        rows = data.get("data", []) if isinstance(data, dict) else []
        return [m["id"] for m in rows if isinstance(m, dict) and isinstance(m.get("id"), str)]
    except (OSError, ValueError, TypeError):
        return []


def snapshot(*, base_url: str | None = None, timeout: float = 1.5) -> dict[str, Any]:
    """One OpenCodex row. Always ``kind=opencodex`` — never ``codex``."""
    url = (base_url or default_base_url()).strip() or DEFAULT_BASE_URL
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        url = DEFAULT_BASE_URL
    reachable, error = _http_reachable(url, timeout=timeout)
    present = _binary_present()
    if not reachable and error is None and not present:
        error = "ocx not on PATH and proxy did not answer"
    models = _list_models(url, timeout=timeout) if reachable else []
    return {
        "kind": _KIND,
        "binary": _BINARY,
        "binary_present": present,
        "base_url": url,
        "reachable": reachable,
        "models": models,
        "error": error,
    }
