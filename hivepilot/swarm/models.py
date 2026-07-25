"""Swarm Phase 1 — the wire-level `Event` model + deterministic id + HMAC
sign/verify helpers.

`Event.id` is DETERMINISTIC from `(type, dedupe_key)` (see `compute_event_id`)
so a re-publish of the same logical event (e.g. `pr_ready` fired twice for the
same repo/branch/sha, because a step retried) collides on the same primary
key in `swarm_events` and is therefore a DEDUPE, never a second unit of work
-- see `hivepilot.services.swarm_service.publish_event`.

SECURITY: every event that leaves this process is signed with HMAC-SHA256
(`sign_event`) using the fleet's shared `${secret:SWARM_KEY}` (resolved via
`hivepilot.services.swarm_service.get_signing_key` -- the existing secrets
mechanism, never plaintext, masked). `verify_event` is the single choke point
every claim path must call before a claimed event is ever handed to a
handler -- an event that fails verification is REJECTED, never executed (see
`hivepilot.services.swarm_service.claim_next`).

The signed material is deliberately `(id, type, tenant, origin_instance,
payload)` -- NOT `ts`. `ts` is purely informational (when this instance
believed it published); excluding it means a legitimate re-publish of
identical content still verifies against the ORIGINAL signature even though
`ts` differs, while any change to the security-relevant fields (who claims to
have originated it, for which tenant, doing what) invalidates the signature.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from pydantic import BaseModel, Field


def compute_event_id(event_type: str, dedupe_key: str) -> str:
    """Deterministic event id: `f"{event_type}:{dedupe_key}"`.

    `dedupe_key` is caller-supplied and should embed whatever makes this
    event's UNIT OF WORK unique (e.g. for `pr_ready`:
    `f"{repo}:{branch}:{sha}"`). Two publishes with the same
    `(event_type, dedupe_key)` always yield the same id, regardless of
    payload/timestamp -- that's the whole dedupe contract.
    """
    return f"{event_type}:{dedupe_key}"


class Event(BaseModel):
    """A single swarm bus event, per the Phase 1 PRD shape:
    `{id, type, payload, tenant, origin_instance, ts}` (+ `sig`, added at
    publish time by `swarm_service.publish_event` before it ever reaches a
    `Transport`)."""

    id: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    tenant: str = "default"
    origin_instance: str
    ts: float = Field(default_factory=time.time)
    # HMAC-SHA256 hex digest over the signed material (see module docstring).
    # `None` until `swarm_service.publish_event` signs it -- an event with no
    # signature always fails `verify_event` (fail-closed).
    sig: str | None = None


def _signed_material(event: Event) -> bytes:
    """Canonical JSON bytes of the security-relevant fields — stable key
    order (`sort_keys=True`) so the same logical content always produces the
    same bytes regardless of dict insertion order."""
    payload = {
        "id": event.id,
        "type": event.type,
        "tenant": event.tenant,
        "origin_instance": event.origin_instance,
        "payload": event.payload,
    }
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")


def sign_event(event: Event, key: str) -> str:
    """Return the hex HMAC-SHA256 digest of *event*'s signed material under
    *key*. Never logs or embeds *key* itself — the returned digest cannot be
    reversed to recover it."""
    return hmac.new(key.encode("utf-8"), _signed_material(event), hashlib.sha256).hexdigest()


def verify_event(event: Event, key: str) -> bool:
    """Fail-closed signature check: `False` for a missing/empty `event.sig`,
    a wrong *key*, or ANY tampering with the signed material (id/type/
    tenant/origin_instance/payload). Uses `hmac.compare_digest` — constant
    time, no signature-length/content oracle."""
    if not event.sig:
        return False
    expected = sign_event(event, key)
    return hmac.compare_digest(expected, event.sig)
