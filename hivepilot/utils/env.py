from __future__ import annotations

import os
from typing import Mapping


def merge_environments(*layers: Mapping[str, str] | None) -> dict[str, str]:
    """Build a process environment by overlaying the provided mappings on top of os.environ.

    A W3C ``TRACEPARENT`` header for the currently-active recording OTel
    span (see ``hivepilot.observability.tracing.traceparent_env``) is
    injected as a LOW-priority layer immediately after the ``os.environ``
    base — before any caller-supplied *layers* — so every runner subprocess
    picks up trace context for free (this is the single choke point ~15
    runners + drift_service all funnel through), while an explicit
    ``TRACEPARENT`` in a later layer (project/definition/secrets) would
    still win. Lazy, guarded, function-local import to avoid any
    circular-import risk; if the import or call fails for any reason,
    proceed with no traceparent — this function must stay robust
    regardless of tracing state. When tracing is off / OTel isn't
    installed / no span is recording, `traceparent_env()` returns `{}` and
    this call is a pure no-op — the merged env stays byte-identical to
    before tracing existed.
    """
    env = os.environ.copy()
    try:
        from hivepilot.observability.tracing import traceparent_env

        env.update(traceparent_env())
    except Exception:  # noqa: BLE001 — tracing must never break env merging
        pass
    for layer in layers:
        if layer:
            env.update(layer)
    return env


def gather_overrides(*layers: Mapping[str, str] | None) -> dict[str, str]:
    """Combine mappings without inheriting os.environ (used for container args)."""
    combined: dict[str, str] = {}
    for layer in layers:
        if layer:
            combined.update(layer)
    return combined


def proxy_env() -> dict[str, str]:
    """Return proxy-related environment variables present in the process env."""
    keys = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
    )
    return {k: os.environ[k] for k in keys if k in os.environ}
