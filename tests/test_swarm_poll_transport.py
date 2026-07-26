"""Tests for hivepilot.swarm.poll_transport.PollTransport — the zero-infra
default transport (Swarm Phase 1): reads pending work straight from the
local `swarm_events` state-DB table (no broker), so a solo deployment works
out of the box with no redis/network dependency whatsoever.

Covers:
  - `publish` persists the event into `swarm_events` (via
    `state_service.insert_swarm_event`).
  - `subscribe` yields pending rows as `Event` objects, filtered by type.
  - `claim` delegates to `state_service.claim_swarm_event` — exactly-once
    across two `PollTransport` instances (simulating two fleet instances)
    racing for the same event.
  - `ack`/`complete` are safe no-ops (poll has no broker-level bookkeeping).
  - Works with NO `redis` import anywhere in this module (zero-infra).
"""

from __future__ import annotations

from typing import Any

from hivepilot.config import settings
from hivepilot.services import state_service
from hivepilot.swarm.models import Event, compute_event_id
from hivepilot.swarm.poll_transport import PollTransport


def _event(dedupe_key: str = "acme/widgets:hivepilot/x:deadbeef", **overrides: Any) -> Event:
    defaults: dict[str, Any] = dict(
        id=compute_event_id("pr_ready", dedupe_key),
        type="pr_ready",
        payload={"repo": "acme/widgets"},
        tenant="default",
        origin_instance="inst-a",
        ts=1_700_000_000.0,
        sig="sig",
    )
    defaults.update(overrides)
    return Event(**defaults)


def _transport(instance_id: str = "inst-a") -> PollTransport:
    return PollTransport(settings=settings, instance_id=instance_id)


class TestName:
    def test_name_is_poll(self) -> None:
        assert PollTransport.name == "poll"


class TestPublish:
    def test_publish_persists_event(self) -> None:
        event = _event()
        _transport().publish(event)
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["status"] == "pending"

    def test_republish_same_id_is_idempotent(self) -> None:
        event = _event()
        transport = _transport()
        transport.publish(event)
        transport.publish(event)  # no raise, no duplicate row
        rows = state_service.list_pending_swarm_events()
        assert len([r for r in rows if r["id"] == event.id]) == 1


class TestSubscribe:
    def test_yields_pending_events(self) -> None:
        event = _event()
        transport = _transport()
        transport.publish(event)
        seen = list(transport.subscribe(["pr_ready"]))
        assert any(e.id == event.id for e in seen)

    def test_filters_by_type(self) -> None:
        pr_event = _event(dedupe_key="r:b1:s1", type="pr_ready")
        other_event = _event(dedupe_key="r:b2:s2", type="other_type")
        transport = _transport()
        transport.publish(pr_event)
        transport.publish(other_event)
        seen_ids = {e.id for e in transport.subscribe(["pr_ready"])}
        assert pr_event.id in seen_ids
        assert other_event.id not in seen_ids

    def test_does_not_yield_already_claimed_events(self) -> None:
        event = _event()
        transport = _transport()
        transport.publish(event)
        transport.claim(event.id)
        seen_ids = {e.id for e in transport.subscribe(["pr_ready"])}
        assert event.id not in seen_ids


class TestClaimExactlyOnce:
    def test_two_instances_race_exactly_one_wins(self) -> None:
        event = _event()
        publisher = _transport("inst-publisher")
        publisher.publish(event)

        instance_a = _transport("inst-a")
        instance_b = _transport("inst-b")

        result_a = instance_a.claim(event.id)
        result_b = instance_b.claim(event.id)

        assert {result_a, result_b} == {True, False}
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["claimed_by"] in ("inst-a", "inst-b")

    def test_claim_unknown_event_returns_false(self) -> None:
        assert _transport().claim("pr_ready:unknown") is False


class TestAckComplete:
    def test_ack_is_a_safe_no_op(self) -> None:
        event = _event()
        transport = _transport()
        transport.publish(event)
        transport.ack(event.id)  # must not raise
        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["status"] == "pending"  # ack alone doesn't change status

    def test_complete_is_a_safe_no_op(self) -> None:
        transport = _transport()
        transport.complete("pr_ready:unknown")  # must not raise


class TestZeroInfra:
    def test_module_never_imports_redis(self) -> None:
        import hivepilot.swarm.poll_transport as mod

        assert not hasattr(mod, "redis")


class TestTenantScopedSubscribe:
    """MEDIUM #3 fix (opus security review): `subscribe` must filter by
    served tenant(s) IN SQL (`list_pending_swarm_events(tenants=...)`), not
    just rely on a caller-side post-filter -- otherwise a burst of pending
    events for a tenant this instance does NOT serve can fill the whole
    `limit=50` window and permanently starve a genuinely-served tenant's
    events out of ever appearing as a `subscribe()` candidate at all."""

    def test_subscribe_only_yields_served_tenant_events(self) -> None:
        transport = PollTransport(
            settings=settings.model_copy(update={"swarm_served_tenants": ["tenant-a"]}),
            instance_id="inst-a",
        )
        served_event = _event(dedupe_key="r:served:s", tenant="tenant-a")
        other_event = _event(dedupe_key="r:other:s", tenant="tenant-b")
        state_service.insert_swarm_event(served_event)
        state_service.insert_swarm_event(other_event)

        seen_ids = {e.id for e in transport.subscribe(["pr_ready"])}
        assert served_event.id in seen_ids
        assert other_event.id not in seen_ids

    def test_unserved_tenant_burst_does_not_starve_served_tenant(self) -> None:
        """Reproduces the exact MEDIUM #3 exploit: 60 pending events for an
        UNSERVED tenant (more than `list_pending_swarm_events`'s default
        `limit=50`) must never crowd out a single served-tenant event -- the
        tenant filter has to happen in SQL, before the `LIMIT`, or the
        served-tenant event would never even reach this instance's
        `subscribe()` window to be skipped-past, let alone claimed."""
        for i in range(60):
            state_service.insert_swarm_event(
                _event(dedupe_key=f"r:unserved:{i}", tenant="tenant-unserved")
            )
        served_event = _event(dedupe_key="r:served:s", tenant="tenant-served")
        state_service.insert_swarm_event(served_event)

        transport = PollTransport(
            settings=settings.model_copy(update={"swarm_served_tenants": ["tenant-served"]}),
            instance_id="inst-a",
        )
        seen_ids = {e.id for e in transport.subscribe(["pr_ready"])}
        assert served_event.id in seen_ids
        assert transport.claim(served_event.id) is True
