from __future__ import annotations

import threading
from functools import lru_cache

from hivepilot.config import settings

# Large sentinel for "effectively unlimited"
_UNLIMITED = 2**31 - 1


@lru_cache(maxsize=None)
def _semaphore_for_kind(kind: str, cap: int) -> threading.Semaphore:
    """Return (and cache) a Semaphore for a given runner kind and cap.

    lru_cache keyed by (kind, cap) ensures we always return the same Semaphore
    object for a given kind during a process lifetime — thread-safe by construction.
    """
    return threading.Semaphore(cap)


def semaphore_for_kind(kind: str) -> threading.Semaphore:
    """Public accessor: returns the Semaphore for *kind*, sized from config.

    - "claude" → settings.claude_max_concurrency (default 1)
    - anything else → unlimited
    """
    cap = settings.claude_max_concurrency if kind == "claude" else _UNLIMITED
    return _semaphore_for_kind(kind, cap)
