"""hivepilot.swarm -- peer federation bus (Swarm Phase 1). See
hivepilot/swarm/transport.py for the guarded-import rationale (mirrors
hivepilot/forges/__init__.py's httpx guard for forgejo/gitlab)."""

from __future__ import annotations

from hivepilot.swarm.poll_transport import PollTransport
from hivepilot.swarm.transport import (
    TRANSPORT_MAP,
    Transport,
    TransportCollisionError,
    TransportRegistry,
    UnknownTransportError,
    resolve_transport,
)

TransportRegistry.register("poll", PollTransport)

try:
    from hivepilot.swarm.redis_transport import RedisTransport

    TransportRegistry.register("redis", RedisTransport)
except ImportError:  # pragma: no cover -- exercised by CI's default (redis installed) env
    RedisTransport = None  # type: ignore[assignment, misc]

__all__ = [
    "TRANSPORT_MAP",
    "PollTransport",
    "RedisTransport",
    "Transport",
    "TransportCollisionError",
    "TransportRegistry",
    "UnknownTransportError",
    "resolve_transport",
]
