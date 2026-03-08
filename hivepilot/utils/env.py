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
