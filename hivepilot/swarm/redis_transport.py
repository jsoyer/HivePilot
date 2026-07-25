"""``RedisTransport`` — the redis-streams swarm transport (Swarm Phase 1),
name ``"redis"``.

Uses redis **consumer groups** (``XADD``/``XREADGROUP``/``XACK``) so a stream
entry is delivered to at most ONE consumer within the shared group
(``GROUP``) -- every fleet instance reads with its own ``instance_id`` as the
consumer name, so Redis itself provides delivery-exclusivity as a live
push/pop optimisation.

That said, the ACTUAL exactly-once authority is the SAME
``state_service.claim_swarm_event`` atomic ``UPDATE ... WHERE
status='pending'`` that ``PollTransport.claim`` also calls -- ``claim()``
here delegates to it too, belt-and-suspenders on top of Redis's own
guarantee (never a replacement for it): if Redis's consumer-group delivery
were ever violated (a visibility-timeout redelivery race, a
misconfiguration, a second, DIFFERENT consumer group reading the same
stream, ...), the shared SQLite/Postgres claim is still the final,
authoritative gate. This also means both transports are TESTABLE against the
exact same exactly-once contract without needing a real broker for either.

``redis`` is an OPTIONAL extra (``pip install hivepilot[swarm]``), not a core
dependency -- imported at module level (so this module's tests can mock the
client directly), guarded by ``hivepilot/swarm/__init__.py``'s
``try/except ImportError`` registration (mirrors ``hivepilot/forges/
forgejo.py``'s ``httpx`` guard): a core install with no extras never fails to
import ``hivepilot.swarm``, "redis" simply never registers into
``TRANSPORT_MAP`` -- a deployment that sets ``HIVEPILOT_SWARM_TRANSPORT=redis``
without installing the extra fails closed with a clear "unknown transport"
error (``UnknownTransportError``) at resolve time, never an import crash.
"""

from __future__ import annotations

from typing import Any, Iterator

import redis

from hivepilot.config import Settings
from hivepilot.services import state_service
from hivepilot.swarm.models import Event
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


def _decode(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else str(value)


class RedisTransport:
    name = "redis"

    # A single shared stream + consumer group for every event type in Phase
    # 1 (one end-to-end event type, `pr_ready`) -- `subscribe`'s `types`
    # filter narrows client-side. A later phase with high-volume, distinct
    # event types could split into per-type streams without changing this
    # class's public contract.
    STREAM_KEY = "hivepilot:swarm:events"
    GROUP = "hivepilot-swarm"

    def __init__(self, *, settings: Settings, instance_id: str, client: Any = None) -> None:
        self._instance_id = instance_id
        # Typed `Any` deliberately: accepts a real (sync) `redis.Redis`
        # client, a test double, or a mock — this class only ever calls a
        # narrow duck-typed subset (xgroup_create/xadd/xreadgroup/xack), not
        # the full client surface.
        self._client: Any = (
            client
            if client is not None
            else redis.Redis.from_url(
                settings.swarm_redis_url or settings.redis_url or "redis://localhost:6379/0"
            )
        )
        # event_id -> redis stream message id, populated by subscribe() so a
        # later claim()/ack() call knows which stream entry to XACK.
        # Process-local only (never persisted) -- losing it on restart just
        # means a still-pending stream entry gets naturally redelivered by
        # Redis's own pending-entries-list mechanics; no correctness impact
        # since `claim()`'s authority is the DB, not this map.
        self._pending_msg_ids: dict[str, Any] = {}
        self._ensure_group()

    def _ensure_group(self) -> None:
        try:
            self._client.xgroup_create(self.STREAM_KEY, self.GROUP, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001
            if "BUSYGROUP" not in str(exc):
                raise

    def publish(self, event: Event) -> None:
        # Persist to the shared audit/dedupe table FIRST (self-sufficient,
        # mirrors PollTransport.publish -- see that module's docstring),
        # then hand off to the broker for live delivery.
        state_service.insert_swarm_event(event)
        self._client.xadd(self.STREAM_KEY, {"data": event.model_dump_json()})

    def subscribe(self, types: list[str]) -> Iterator[Event]:
        resp = self._client.xreadgroup(
            self.GROUP, self._instance_id, {self.STREAM_KEY: ">"}, count=50
        )
        for _stream_name, messages in resp or []:
            for msg_id, fields in messages:
                data = fields.get(b"data")
                if data is None:
                    data = fields.get("data")
                if data is None:
                    continue
                event = Event.model_validate_json(_decode(data))
                self._pending_msg_ids[event.id] = msg_id
                if types and event.type not in types:
                    continue
                yield event

    def claim(self, event_id: str) -> bool:
        claimed = state_service.claim_swarm_event(event_id, claimed_by=self._instance_id)
        if claimed:
            self._xack(event_id)
        return claimed

    def ack(self, event_id: str) -> None:
        self._xack(event_id)

    def complete(self, event_id: str) -> None:
        self._xack(event_id)

    def _xack(self, event_id: str) -> None:
        msg_id = self._pending_msg_ids.get(event_id)
        if msg_id is None:
            return
        try:
            self._client.xack(self.STREAM_KEY, self.GROUP, msg_id)
        except Exception:  # noqa: BLE001 — broker-level ack failure must never break the run
            logger.warning("swarm.redis_xack_failed", event_id=event_id)
