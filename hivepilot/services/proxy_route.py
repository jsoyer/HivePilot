"""Decide, per dispatch, whether to route an agent through the local
compression proxy — or straight to the provider.

Pinning `ANTHROPIC_BASE_URL` in the service environment is a **hard**
redirect. A proxy that is down, restarting, or failed to start at boot means
connection-refused on every agent call, and every step fails. `Restart=always`
narrows that window to a couple of seconds; it does not close it, and it does
nothing at all for a proxy that never came up.

That trade is not worth taking for an optimisation. The proxy compresses tool
outputs to save tokens; nothing about that justifies it being able to take the
whole fleet down. So the base URL is resolved here, at dispatch time:
reachable → route through it, unreachable → talk to the provider directly.

**The fallback is recorded, never silent.** A system that quietly changes
route produces numbers nobody can interpret later — "why did this run compress
nothing?" must have an answer that does not require guessing. Absent
configuration is a different thing entirely and stays quiet: most deployments
never run the proxy, and warning for them would bury the signal that matters.
"""

from __future__ import annotations

import socket
from urllib.parse import urlsplit

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# The probe runs before EVERY agent call, so its worst case is a real budget.
# 0.25s is far below the latency of the model call it precedes, and well above
# a loopback connect (sub-millisecond). A non-routable address is the case
# that would otherwise hang until the OS gives up, which can be minutes.
DEFAULT_TIMEOUT_SECONDS = 0.25

_DEFAULT_PORTS = {"http": 80, "https": 443}


def resolve_base_url(
    configured: str | None, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> str | None:
    """The base URL to route this dispatch through, or ``None`` for direct.

    ``None`` means "do not set ``ANTHROPIC_BASE_URL``" — the agent then talks
    to the provider itself, which is the pre-proxy behaviour and always works.

    Returns ``None`` for: no proxy configured, a malformed URL, or a proxy
    that is not accepting connections. Only the last of those is a degraded
    state, and only that one warns.
    """
    if not configured:
        return None

    try:
        parts = urlsplit(configured)
        host = parts.hostname
        port = parts.port or _DEFAULT_PORTS.get(parts.scheme)
        if not host or not port:
            raise ValueError(f"no host/port in {configured!r}")
    except Exception as exc:  # noqa: BLE001 — a typo must degrade, not raise
        # Misconfiguration is not a reason to fail every dispatch. It is a
        # reason to say so and carry on directly.
        logger.warning("proxy.unusable_url_direct_fallback", url=configured, error=str(exc))
        return None

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return configured
    except Exception as exc:  # noqa: BLE001 — refused, timed out, unroutable
        logger.warning(
            "proxy.unreachable_direct_fallback",
            url=configured,
            error=str(exc),
            detail="agent dispatched directly; this run is not compressed",
        )
        return None
