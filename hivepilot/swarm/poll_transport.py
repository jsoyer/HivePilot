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
        self._instance_id = instance_id
        # MEDIUM #3 fix (opus security review): filter by served tenant(s) IN
        # SQL (`list_pending_swarm_events(tenants=...)` below), not just at
        # the `swarm_service.claim_next` post-filter. Without this, a burst
        # of pending events for a tenant this instance does NOT serve could
        # fill the whole `limit=50` window and permanently starve a
        # genuinely-served tenant's events out of every `subscribe()` call
        # (they'd never even appear as candidates to skip past). `None`
        # `settings` (defensive -- every real caller passes one via
        # `resolve_transport`) degrades to "no tenant filter" rather than
        # crashing the constructor.
        self._served_tenants = list(settings.swarm_served_tenants) if settings is not None else []

    def publish(self, event: Event) -> None:
        """Persist *event* into `swarm_events`. For "poll", this IS the
        delivery mechanism -- there is no separate broker hop."""
        state_service.insert_swarm_event(event)

    def subscribe(self, types: list[str]) -> Iterator[Event]:
        """Yield every currently-`pending` row matching *types* AND this
        instance's served tenant(s) as an `Event`. Every yielded event is a
        CANDIDATE only -- `claim()` is what actually grants ownership (see
        the module-level docstring on
        `hivepilot.swarm.transport.Transport.subscribe`).

        Bug-debt fix (fail-closed on an EMPTY served-tenant set): an instance
        configured with `swarm_served_tenants=[]` must claim NOTHING, ever --
        but `list_pending_swarm_events(tenants=self._served_tenants or
        None)` previously turned an empty list into `tenants=None`, which
        means "no tenant filter" (every tenant) at the SQL layer. End-to-end
        that was only saved by `swarm_service.claim_next`'s Python-level
        `event.tenant not in served_tenants` post-filter -- the SQL layer
        ALONE was fail-open, and a future caller of `subscribe()` that skips
        that post-filter (or a redis-transport-style caller with no
        post-filter at all) would silently see every tenant's events. An
        empty served set therefore short-circuits BEFORE ever querying --
        never even constructs a `tenants=None` "no filter" query -- so the
        SQL layer itself is fail-closed too, not just the caller above it.
        """
        if not self._served_tenants:
            return
        for row in state_service.list_pending_swarm_events(
            types=types or None, tenants=self._served_tenants
        ):
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
