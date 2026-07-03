from __future__ import annotations

import os
from typing import Mapping


def merge_environments(*layers: Mapping[str, str] | None) -> dict[str, str]:
    """Build a process environment by overlaying the provided mappings on top of os.environ."""
    env = os.environ.copy()
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
