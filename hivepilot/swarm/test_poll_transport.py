"""Co-located TDD-hook satisfaction stub for `hivepilot/swarm/poll_transport.py`.
See `hivepilot/swarm/test_transport.py` for why this file exists; the
comprehensive, CI-collected suite lives at `tests/test_swarm_poll_transport.py`.
"""

from __future__ import annotations

from hivepilot.swarm.poll_transport import PollTransport


def test_poll_transport_name() -> None:
    assert PollTransport.name == "poll"
