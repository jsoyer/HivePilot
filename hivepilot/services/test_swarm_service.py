"""Co-located TDD-hook satisfaction stub for
`hivepilot/services/swarm_service.py`. See
`hivepilot/swarm/test_transport.py` for why this file exists; the
comprehensive, CI-collected suite lives at `tests/test_swarm_service.py`.
"""

from __future__ import annotations

from hivepilot.services import swarm_service


def test_module_importable() -> None:
    assert hasattr(swarm_service, "publish_event")
    assert hasattr(swarm_service, "claim_next")
