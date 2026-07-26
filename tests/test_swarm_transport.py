"""Tests for hivepilot.swarm.transport — the `Transport` plugin interface,
`TRANSPORT_MAP` registry, and fail-closed `resolve_transport` (mirrors
`hivepilot.forges.provider`/`hivepilot.registry`'s RunnerRegistry pattern).

Covers:
  - Built-ins ("poll", and "redis" when the `redis` package is installed)
    are registered at import time.
  - `resolve_transport` returns a working instance for a known name.
  - An UNKNOWN transport name raises `UnknownTransportError` at resolve time
    — never a silent fallback to "poll".
  - `TransportRegistry.register` collision guard (same-name, different class)
    raises `TransportCollisionError`, mirroring `ForgeCollisionError`/
    `RunnerKindCollisionError`.
"""

from __future__ import annotations

import pytest

from hivepilot.swarm.transport import (
    TRANSPORT_MAP,
    Transport,
    TransportCollisionError,
    TransportRegistry,
    UnknownTransportError,
    resolve_transport,
)


class TestBuiltinRegistration:
    def test_poll_is_registered(self) -> None:
        assert "poll" in TRANSPORT_MAP

    def test_known_kinds_includes_poll(self) -> None:
        assert "poll" in TransportRegistry.known_kinds()


class TestResolveTransport:
    def test_resolves_poll_by_name(self) -> None:
        transport = resolve_transport("poll", instance_id="inst-a")
        assert transport.name == "poll"

    def test_unknown_name_raises_fail_closed(self) -> None:
        with pytest.raises(UnknownTransportError):
            resolve_transport("smoke-signal", instance_id="inst-a")

    def test_unknown_name_error_names_available_kinds(self) -> None:
        with pytest.raises(UnknownTransportError) as exc_info:
            resolve_transport("carrier-pigeon", instance_id="inst-a")
        assert "poll" in str(exc_info.value)


class TestTransportRegistryCollision:
    def test_registering_different_class_under_same_name_raises(self) -> None:
        class _FakeA:
            name = "fake-dup"

        class _FakeB:
            name = "fake-dup"

        TransportRegistry.register("fake-dup", _FakeA)
        try:
            with pytest.raises(TransportCollisionError):
                TransportRegistry.register("fake-dup", _FakeB)
        finally:
            TRANSPORT_MAP.pop("fake-dup", None)

    def test_override_flag_allows_replacement(self) -> None:
        class _FakeA:
            name = "fake-dup2"

        class _FakeB:
            name = "fake-dup2"

        TransportRegistry.register("fake-dup2", _FakeA)
        try:
            TransportRegistry.register("fake-dup2", _FakeB, override=True)
            assert TRANSPORT_MAP["fake-dup2"] is _FakeB
        finally:
            TRANSPORT_MAP.pop("fake-dup2", None)

    def test_reregistering_same_object_is_not_a_collision(self) -> None:
        class _Fake:
            name = "fake-dup3"

        TransportRegistry.register("fake-dup3", _Fake)
        try:
            TransportRegistry.register("fake-dup3", _Fake)  # no raise
        finally:
            TRANSPORT_MAP.pop("fake-dup3", None)


def test_transport_protocol_shape() -> None:
    """Structural sanity: `Transport` declares the four PRD-mandated methods."""
    for method in ("publish", "subscribe", "claim", "ack", "complete"):
        assert hasattr(Transport, method)
