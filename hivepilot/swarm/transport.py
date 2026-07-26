"""``Transport`` — the swarm bus plugin type (Swarm Phase 1).

Mirrors ``hivepilot.registry``'s ``RunnerRegistry``/``RUNNER_MAP`` pattern
(class registry, resolved-per-use) rather than ``hivepilot.forges.provider``'s
``ForgeRegistry``/``FORGE_MAP`` (stateless singleton instances): a transport
needs PER-INSTANCE state (which fleet ``instance_id`` is asking, a redis
client handle, ...), so ``TRANSPORT_MAP`` stores CLASSES and
``resolve_transport`` constructs a fresh instance for the caller, exactly like
``hivepilot.registry.resolve_runner_class`` + ``RunnerRegistry.get_runner``.

Fail-closed resolution: an unregistered/unknown transport name raises
``UnknownTransportError`` at config-load/call time -- NEVER a silent fallback
to ``"poll"`` (a fleet operator who typo'd ``HIVEPILOT_SWARM_TRANSPORT`` must
find out immediately, not discover months later that every instance silently
downgraded to solo poll mode and never actually federated).
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, Protocol, Type

from hivepilot.config import Settings
from hivepilot.swarm.models import Event


class Transport(Protocol):
    """Structural interface every swarm transport plugin implements.

    ``__init__`` is deliberately NOT part of the structural contract (Protocol
    can't usefully enforce constructor signatures) -- every concrete
    implementation accepts ``(*, settings, instance_id)`` keyword args, mirrored
    by ``resolve_transport`` below.
    """

    name: str

    def publish(self, event: Event) -> None:
        """Hand *event* (already signed) to the broker for delivery.

        Best-effort from the CALLER's perspective (see
        ``hivepilot.services.swarm_service.publish_event``) -- an
        implementation should raise on a genuine transport failure rather
        than swallow it; the engine service is what decides "best-effort
        never breaks a run", not the transport itself.
        """
        ...

    def subscribe(self, types: list[str]) -> Iterator[Event]:
        """Yield candidate events of the given *types* this instance MIGHT be
        able to claim. Yielding an event is NOT a claim -- see ``claim()``.
        """
        ...

    def claim(self, event_id: str) -> bool:
        """Atomically claim *event_id* for this transport's ``instance_id``.

        Returns ``True`` iff THIS call won the claim (exactly-once across
        every instance in the fleet, regardless of how many instances call
        this concurrently for the same ``event_id``). ``False`` means another
        instance already claimed (or completed) it first.
        """
        ...

    def ack(self, event_id: str) -> None:
        """Acknowledge *event_id* was received/claimed (broker-level, e.g.
        Redis ``XACK``). No-op for a broker with nothing to acknowledge."""
        ...

    def complete(self, event_id: str) -> None:
        """Mark *event_id* fully processed (broker-level cleanup). No-op for
        a broker with nothing to clean up."""
        ...


class UnknownTransportError(RuntimeError):
    """Raised by ``resolve_transport`` when *name* isn't a registered
    transport. Fail-closed -- never a silent fallback to another transport."""


class TransportCollisionError(RuntimeError):
    """Raised by ``TransportRegistry.register`` when a DIFFERENT class tries
    to silently replace an already-registered name (mirrors
    ``RunnerKindCollisionError``/``ForgeCollisionError``)."""


TRANSPORT_MAP: Dict[str, Type[Any]] = {}


class TransportRegistry:
    """Process-global transport registry -- mirrors ``RunnerRegistry``."""

    @staticmethod
    def register(name: str, cls: Type[Any], *, override: bool = False) -> None:
        if name in TRANSPORT_MAP and TRANSPORT_MAP[name] is not cls and not override:
            raise TransportCollisionError(
                f"Transport '{name}' is already registered to "
                f"{TRANSPORT_MAP[name].__name__}; refusing to silently replace it "
                f"with {cls.__name__}"
            )
        TRANSPORT_MAP[name] = cls

    @staticmethod
    def known_kinds() -> frozenset[str]:
        return frozenset(TRANSPORT_MAP)


def resolve_transport(
    name: str, *, instance_id: str, settings: Settings | None = None
) -> Transport:
    """Construct a fresh transport instance for *name*, scoped to
    *instance_id* and *settings*.

    *settings* defaults to the process-global singleton ONLY when the caller
    doesn't pass one -- previously this function unconditionally imported and
    used the global singleton, silently ignoring any per-call ``Settings``
    object a caller (e.g. ``swarm_service.claim_next``/``publish_event``, both
    of which already accept an optional ``settings`` override) had actually
    resolved. That was harmless while no transport read anything off
    ``settings`` beyond what it was constructed with, but once
    ``PollTransport`` started filtering by ``settings.swarm_served_tenants``
    in SQL (MEDIUM #3 fix, opus security review) it became a real bug: a
    caller's explicit tenant scope would be silently discarded in favor of the
    global default. Threading *settings* through here is what makes a custom
    ``Settings`` object (per-tenant test fixtures, a multi-tenant dispatcher
    iterating several identities, ...) actually take effect.

    Fail-closed: raises ``UnknownTransportError`` (never a silent fallback)
    when *name* isn't registered in ``TRANSPORT_MAP`` -- e.g. ``"redis"`` was
    requested but the ``redis`` package isn't installed, so it never
    registered itself (see ``hivepilot/swarm/__init__.py``).
    """
    if settings is None:
        from hivepilot.config import settings as _settings

        settings = _settings

    cls = TRANSPORT_MAP.get(name)
    if cls is None:
        raise UnknownTransportError(
            f"Unknown swarm transport {name!r}; available: {sorted(TRANSPORT_MAP)}"
        )
    return cls(settings=settings, instance_id=instance_id)
