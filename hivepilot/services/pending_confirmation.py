"""Shared owner+TTL-bound pending-confirmation store.

THE BUG CLASS THIS CLOSES: a destructive-action confirmation (a Yes/No
button, an inline keyboard, or a text "yes <token>" reply) keyed by
conversation/channel ONLY — no binding to the person who triggered it, no
expiry. That shape has already been independently reinvented, found broken,
and fixed TWICE in this repo:

  * `slack_bot._PendingChallenge` (F3 fix, PR #335 follow-up) — an Opus
    review found the Challenge/Ask follow-up keyed by channel alone let
    Alice press Challenge and have Bob's NEXT channel message consumed and
    dispatched *with Alice recorded as the author* — attribution forgery in
    an authorization-bearing audit log — and let an abandoned pending entry
    silently capture an unrelated message hours later.
  * `concierge_service._PendingOffer` (99285ef9) — the same shape for a
    bare "yes"/"oui" answering a concierge follow-up offer.

Both fixes hand-rolled the same owner-binding + TTL + fail-closed dict from
scratch. This module is the ONE place that logic now lives, so the THIRD
occurrence — `_pending_concierge` in `telegram_bot.py` / `slack_bot.py` /
`discord_bot.py` — uses it instead of pasting a fourth near-identical copy.
The two existing fixes are left as-is (already shipped, already correct,
out of scope for this change) — a future cleanup could point them at this
same store.

FAIL-CLOSED CONTRACT: `store()` records nothing without both a key and a
non-empty owner id — an entry nobody can be matched against must never
become an entry anybody can resolve. `resolve()` treats a missing key, a
missing/empty requester id, an expired entry, or an owner mismatch
identically from the caller's point of view: `None`, meaning "do not
execute". An expired (or ownerless/corrupted) entry is dropped as a side
effect of the failed resolve; an owner-mismatched entry is left completely
untouched so the actual owner can still resolve it afterwards.

`resolve()` deliberately does NOT pop the entry on a successful owner+TTL
match — callers that layer an additional check on top (e.g. the concierge
flow's confirmation token, which guards against a *newer* destructive
decision from the SAME owner silently overwriting a still-displayed stale
keyboard) must be able to fail that check without having already consumed
an otherwise still-valid entry. Callers call `discard()` once every check
they need has passed.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class _Pending(Generic[T]):
    owner_id: str
    payload: T
    expires_at: float


class PendingConfirmationStore(Generic[T]):
    """Keyed store of owner+TTL bound pending confirmations.

    One instance per confirmation *flow* (e.g. one per bot module's
    `_pending_concierge`) — never a shared global singleton — so one flow's
    entries can never leak into, or be resolved against, another's. Backed
    by a `threading.Lock`: webhook-mode handlers (Slack/Discord) can run on
    separate request threads, and `concierge_service._pending_offers`
    already needed the same protection.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._pending: dict[object, _Pending[T]] = {}

    def store(self, key: object | None, owner_id: str | None, payload: T) -> None:
        """Record *payload* for *key*, owned by *owner_id*, expiring after
        this store's TTL. A missing *key* or a falsy *owner_id* stores
        NOTHING (fail closed) — storing anyway would create an entry no
        legitimate `resolve()` call could ever be bound to, which is exactly
        the shape of this bug class. Overwrites any existing entry for
        *key* outright (matches the pre-existing "a newer destructive
        message supersedes the previous pending one" behaviour)."""
        if key is None or not owner_id:
            return
        with self._lock:
            self._pending[key] = _Pending(
                owner_id=owner_id,
                payload=payload,
                expires_at=time.time() + self._ttl_seconds,
            )

    def resolve(self, key: object | None, requester_id: str | None) -> T | None:
        """Return the payload stored for *key* iff it exists, has not
        expired, and is owned by *requester_id* — otherwise `None`.

        A missing *key*/*requester_id*, a missing entry, an expired entry
        (dropped as a side effect), or an owner mismatch (entry left
        untouched) are all indistinguishable to the caller: `None` means
        "do not execute", full stop. Does NOT pop the entry on a match —
        see the module docstring."""
        if key is None or not requester_id:
            return None
        with self._lock:
            pending = self._pending.get(key)
            if pending is None:
                return None
            try:
                expired = time.time() > float(pending.expires_at)
            except (TypeError, ValueError):
                expired = True  # unparseable expiry means expired, never "never expires"
            if not pending.owner_id or expired:
                self._pending.pop(key, None)
                return None
            if pending.owner_id != requester_id:
                # Someone else's press/reply. Never consume -- leave the
                # entry pending so the actual owner can still resolve it.
                return None
            return pending.payload

    def discard(self, key: object | None) -> None:
        """Drop the pending entry for *key*, if any. No-op for a missing
        key or a *key* of `None`."""
        if key is None:
            return
        with self._lock:
            self._pending.pop(key, None)

    def clear(self) -> None:
        """Test/ops helper: drop every pending entry."""
        with self._lock:
            self._pending.clear()

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._pending

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)
