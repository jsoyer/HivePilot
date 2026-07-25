"""Co-located TDD-hook satisfaction stub for `hivepilot/swarm/redis_transport.py`.
See `hivepilot/swarm/test_transport.py` for why this file exists; the
comprehensive, CI-collected suite lives at `tests/test_swarm_redis_transport.py`.
"""

from __future__ import annotations

import pytest

redis = pytest.importorskip("redis")


def test_redis_transport_name() -> None:
    from hivepilot.swarm.redis_transport import RedisTransport

    assert RedisTransport.name == "redis"
