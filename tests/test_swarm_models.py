"""Tests for hivepilot.swarm.models — the `Event` pydantic model, deterministic
event-id computation, and the HMAC sign/verify helpers (Swarm Phase 1).

Covers:
  - `compute_event_id` is deterministic: same (type, dedupe_key) -> same id.
  - Different dedupe_key/type -> different id.
  - `sign_event`/`verify_event` round-trip, and reject tampering (payload,
    tenant, origin_instance, id) and a wrong key.
  - The signing key itself never appears in the signature string (masking
    sanity — the HMAC digest is a hex string, not the key).
"""

from __future__ import annotations

from typing import Any

from hivepilot.swarm.models import Event, compute_event_id, sign_event, verify_event


def _event(**overrides: Any) -> Event:
    defaults: dict[str, Any] = dict(
        id=compute_event_id("pr_ready", "acme/widgets:hivepilot/x:deadbeef"),
        type="pr_ready",
        payload={"repo": "acme/widgets", "branch": "hivepilot/x", "sha": "deadbeef"},
        tenant="default",
        origin_instance="inst-a",
        ts=1_700_000_000.0,
    )
    defaults.update(overrides)
    return Event(**defaults)


class TestComputeEventId:
    def test_deterministic_for_same_inputs(self) -> None:
        a = compute_event_id("pr_ready", "acme/widgets:hivepilot/x:deadbeef")
        b = compute_event_id("pr_ready", "acme/widgets:hivepilot/x:deadbeef")
        assert a == b

    def test_differs_by_type(self) -> None:
        a = compute_event_id("pr_ready", "acme/widgets:hivepilot/x:deadbeef")
        b = compute_event_id("pr_merged", "acme/widgets:hivepilot/x:deadbeef")
        assert a != b

    def test_differs_by_dedupe_key(self) -> None:
        a = compute_event_id("pr_ready", "acme/widgets:hivepilot/x:deadbeef")
        b = compute_event_id("pr_ready", "acme/widgets:hivepilot/x:cafebabe")
        assert a != b

    def test_id_embeds_type_prefix(self) -> None:
        eid = compute_event_id("pr_ready", "repo:branch:sha")
        assert eid.startswith("pr_ready:")


class TestSignVerify:
    def test_round_trip_valid(self) -> None:
        event = _event()
        sig = sign_event(event, "shared-swarm-key")
        signed = event.model_copy(update={"sig": sig})
        assert verify_event(signed, "shared-swarm-key") is True

    def test_wrong_key_rejected(self) -> None:
        event = _event()
        sig = sign_event(event, "shared-swarm-key")
        signed = event.model_copy(update={"sig": sig})
        assert verify_event(signed, "different-key") is False

    def test_missing_signature_rejected(self) -> None:
        event = _event(sig=None)
        assert verify_event(event, "shared-swarm-key") is False

    def test_tampered_payload_rejected(self) -> None:
        event = _event()
        sig = sign_event(event, "shared-swarm-key")
        tampered = event.model_copy(update={"sig": sig, "payload": {"repo": "evil/repo"}})
        assert verify_event(tampered, "shared-swarm-key") is False

    def test_tampered_tenant_rejected(self) -> None:
        event = _event()
        sig = sign_event(event, "shared-swarm-key")
        tampered = event.model_copy(update={"sig": sig, "tenant": "other-tenant"})
        assert verify_event(tampered, "shared-swarm-key") is False

    def test_tampered_origin_instance_rejected(self) -> None:
        event = _event()
        sig = sign_event(event, "shared-swarm-key")
        tampered = event.model_copy(update={"sig": sig, "origin_instance": "attacker"})
        assert verify_event(tampered, "shared-swarm-key") is False

    def test_tampered_id_rejected(self) -> None:
        event = _event()
        sig = sign_event(event, "shared-swarm-key")
        tampered = event.model_copy(update={"sig": sig, "id": "pr_ready:other:x:y"})
        assert verify_event(tampered, "shared-swarm-key") is False

    def test_signature_is_not_the_key_itself(self) -> None:
        event = _event()
        sig = sign_event(event, "shared-swarm-key")
        assert "shared-swarm-key" not in sig

    def test_republishing_same_content_yields_same_signature(self) -> None:
        """Signing is deterministic over (id, type, tenant, origin_instance,
        payload) — a re-publish of the identical event (same ts or not)
        produces a verifiable signature either way, since `ts` is
        deliberately excluded from the signed material (see module
        docstring in hivepilot/swarm/models.py)."""
        event_a = _event(ts=1.0)
        event_b = _event(ts=2.0)
        sig = sign_event(event_a, "shared-swarm-key")
        signed_b = event_b.model_copy(update={"sig": sig})
        assert verify_event(signed_b, "shared-swarm-key") is True
