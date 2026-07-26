"""Tests for hivepilot.swarm.redis_transport.RedisTransport — the
redis-streams transport (Swarm Phase 1), using consumer groups
(XADD/XREADGROUP/XACK) for native delivery-exclusivity, with the SAME
`state_service.claim_swarm_event` atomic claim as `PollTransport` underneath
as the authoritative exactly-once guarantee (belt-and-suspenders — see the
module docstring in `hivepilot/swarm/redis_transport.py`).

No real Redis server is used: a minimal in-memory fake reproducing the
subset of redis-py's Streams/consumer-group semantics this transport relies
on (XADD/XGROUP CREATE/XREADGROUP/XACK, including "a stream entry is
delivered to only ONE consumer within a group") stands in for it.

Also verifies the module GUARDS its `redis` import (skipped entirely when
the package isn't installed — see `hivepilot/swarm/__init__.py`).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("redis")

from hivepilot.config import settings  # noqa: E402
from hivepilot.services import state_service  # noqa: E402
from hivepilot.swarm.models import Event, compute_event_id  # noqa: E402
from hivepilot.swarm.redis_transport import RedisTransport  # noqa: E402


class _FakeRedisStreams:
    """Minimal in-memory stand-in for redis-py's Streams/consumer-group API,
    reproducing the ONE property this transport depends on: a stream entry
    is delivered via XREADGROUP to at most one consumer within a group."""

    def __init__(self) -> None:
        self._streams: dict[str, list[tuple[bytes, dict[bytes, bytes]]]] = {}
        self._delivered: dict[tuple[str, str], set[bytes]] = {}
        self._counter = 0

    def xgroup_create(
        self, name: str, groupname: str, id: str = "0", mkstream: bool = False
    ) -> None:
        key = (name, groupname)
        if key in self._delivered:
            raise Exception("BUSYGROUP Consumer Group name already exists")
        self._delivered[key] = set()
        self._streams.setdefault(name, [])

    def xadd(self, name: str, fields: dict) -> bytes:
        self._counter += 1
        msg_id = f"{self._counter}-0".encode()
        encoded = {
            (k.encode() if isinstance(k, str) else k): (v.encode() if isinstance(v, str) else v)
            for k, v in fields.items()
        }
        self._streams.setdefault(name, []).append((msg_id, encoded))
        return msg_id

    def xreadgroup(
        self, groupname: str, consumername: str, streams: dict, count: int | None = None
    ):
        result = []
        for name in streams:
            key = (name, groupname)
            delivered = self._delivered.setdefault(key, set())
            entries = self._streams.get(name, [])
            fresh = [(mid, fields) for mid, fields in entries if mid not in delivered]
            if count:
                fresh = fresh[:count]
            for mid, _ in fresh:
                delivered.add(mid)
            if fresh:
                result.append((name.encode(), fresh))
        return result

    def xack(self, name: str, groupname: str, *ids) -> int:
        return len(ids)


def _event(dedupe_key: str = "acme/widgets:hivepilot/x:deadbeef", **overrides: Any) -> Event:
    defaults: dict[str, Any] = dict(
        id=compute_event_id("pr_ready", dedupe_key),
        type="pr_ready",
        payload={"repo": "acme/widgets"},
        tenant="default",
        origin_instance="inst-publisher",
        ts=1_700_000_000.0,
        sig="sig",
    )
    defaults.update(overrides)
    return Event(**defaults)


def _transport(fake: _FakeRedisStreams, instance_id: str) -> RedisTransport:
    return RedisTransport(settings=settings, instance_id=instance_id, client=fake)


class TestName:
    def test_name_is_redis(self) -> None:
        assert RedisTransport.name == "redis"


class TestGuardedImport:
    def test_module_does_not_require_real_server_to_import(self) -> None:
        import hivepilot.swarm.redis_transport as mod

        assert hasattr(mod, "redis")


class TestPublish:
    def test_publish_persists_and_adds_to_stream(self) -> None:
        fake = _FakeRedisStreams()
        event = _event()
        _transport(fake, "inst-a").publish(event)
        assert state_service.get_swarm_event(event.id) is not None
        assert len(fake._streams.get(RedisTransport.STREAM_KEY, [])) == 1


class TestSubscribeConsumerGroupExclusivity:
    def test_second_consumer_never_sees_already_delivered_entry(self) -> None:
        fake = _FakeRedisStreams()
        event = _event()
        _transport(fake, "inst-publisher").publish(event)

        instance_a = _transport(fake, "inst-a")
        instance_b = _transport(fake, "inst-b")

        seen_a = [e.id for e in instance_a.subscribe(["pr_ready"])]
        seen_b = [e.id for e in instance_b.subscribe(["pr_ready"])]

        assert event.id in seen_a
        assert event.id not in seen_b

    def test_subscribe_reconstructs_event_fields(self) -> None:
        fake = _FakeRedisStreams()
        event = _event(payload={"repo": "acme/widgets", "branch": "b", "sha": "s"})
        _transport(fake, "inst-publisher").publish(event)

        seen = list(_transport(fake, "inst-a").subscribe(["pr_ready"]))
        assert len(seen) == 1
        assert seen[0].id == event.id
        assert seen[0].payload == event.payload
        assert seen[0].tenant == event.tenant


class TestClaimExactlyOnce:
    def test_claim_delegates_to_shared_state_service_atomic_update(self) -> None:
        """Defense-in-depth: even if two instances BOTH somehow learned of
        the same event_id (e.g. a stale/duplicate delivery), `claim()`'s
        underlying `state_service.claim_swarm_event` — the SAME function
        `PollTransport.claim` uses — guarantees exactly one winner."""
        fake = _FakeRedisStreams()
        event = _event()
        _transport(fake, "inst-publisher").publish(event)

        result_a = _transport(fake, "inst-a").claim(event.id)
        result_b = _transport(fake, "inst-b").claim(event.id)

        assert {result_a, result_b} == {True, False}

    def test_claim_acks_the_stream_entry_on_success(self) -> None:
        fake = _FakeRedisStreams()
        event = _event()
        _transport(fake, "inst-publisher").publish(event)
        instance_a = _transport(fake, "inst-a")
        list(instance_a.subscribe(["pr_ready"]))  # populate its msg-id map
        assert instance_a.claim(event.id) is True  # must not raise on xack

    def test_claim_unknown_event_returns_false(self) -> None:
        fake = _FakeRedisStreams()
        assert _transport(fake, "inst-a").claim("pr_ready:unknown") is False


class TestAckComplete:
    def test_ack_and_complete_are_safe_when_msg_id_unknown(self) -> None:
        fake = _FakeRedisStreams()
        transport = _transport(fake, "inst-a")
        transport.ack("pr_ready:unknown")  # must not raise
        transport.complete("pr_ready:unknown")  # must not raise
