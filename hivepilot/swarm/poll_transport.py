"""``PollTransport`` — the zero-infra DEFAULT swarm transport (Swarm Phase 1).

No broker, no network dependency, no optional package: pending work is read
straight from the local `swarm_events` state-DB table (already required by
every HivePilot install -- see `hivepilot.services.state_service`), so a
solo deployment federates with itself out of the box. This module
deliberately imports NOTHING beyond the stdlib + `hivepilot.services.
state_service` + `hivepilot.swarm.models` -- no `redis`, no optional extra --
so `"poll"` (the default `HIVEPILOT_SWARM_TRANSPORT`) is always available on
a core install.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from hivepilot.config import Settings
from hivepilot.services import state_service
from hivepilot.swarm.models import Event
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


class PollTransport:
    """Reads/writes `swarm_events` directly -- the table itself IS the queue.

    `claim`/`publish` delegate to the SAME `state_service` functions the
    "redis" transport also calls for its own claim step (see
    `hivepilot.swarm.redis_transport`'s module docstring) -- exactly-once
    claim semantics are therefore identical across both transports.
    """

    name = "poll"

    def __init__(self, *, settings: Settings, instance_id: str) -> None:
        del settings  # unused by poll (no broker config) — kept for interface parity
        self._instance_id = instance_id

    def publish(self, event: Event) -> None:
        """Persist *event* into `swarm_events`. For "poll", this IS the
        delivery mechanism -- there is no separate broker hop."""
        state_service.insert_swarm_event(event)

    def subscribe(self, types: list[str]) -> Iterator[Event]:
        """Yield every currently-`pending` row matching *types* as an
        `Event`. Every yielded event is a CANDIDATE only -- `claim()` is what
        actually grants ownership (see the module-level docstring on
        `hivepilot.swarm.transport.Transport.subscribe`)."""
        for row in state_service.list_pending_swarm_events(types=types or None):
            yield _row_to_event(row)

    def claim(self, event_id: str) -> bool:
        return state_service.claim_swarm_event(event_id, claimed_by=self._instance_id)

    def ack(self, event_id: str) -> None:
        """No-op: "poll" has no broker-level pending-entries-list to
        acknowledge -- `claim()` already recorded ownership durably."""
        del event_id

    def complete(self, event_id: str) -> None:
        """No-op: cleanup for "poll" is `state_service.mark_swarm_event_done`
        (called by `swarm_service.process_claimed_event`), not a
        transport-level operation."""
        del event_id


def _row_to_event(row: dict[str, Any]) -> Event:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return Event(
        id=row["id"],
        type=row["type"],
        payload=payload,
        tenant=row["tenant"],
        origin_instance=row["origin_instance"],
        ts=row["ts"] if row["ts"] is not None else 0.0,
        sig=row["sig"],
    )
