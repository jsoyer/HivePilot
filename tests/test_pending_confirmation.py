"""Tests for `hivepilot.services.pending_confirmation.PendingConfirmationStore`.

This module is the shared owner+TTL-bound pending-confirmation primitive
extracted while closing the THIRD instance of a bug class already fixed
twice independently in this repo: `slack_bot._PendingChallenge` and
`concierge_service._PendingOffer`. Both of those keep their own
hand-rolled dict; this store is the generic version used by the bot
modules' `_pending_concierge` (see `test_slack_bot.py`,
`test_telegram_bot.py`, `test_discord_bot.py`).
"""

from __future__ import annotations

import time

from hivepilot.services.pending_confirmation import PendingConfirmationStore


def _store(ttl: float = 60.0) -> PendingConfirmationStore[str]:
    return PendingConfirmationStore(ttl)


class TestStoreAndResolveHappyPath:
    def test_owner_resolves_stored_payload(self) -> None:
        store = _store()
        store.store("chat-1", "alice", "payload-a")
        assert store.resolve("chat-1", "alice") == "payload-a"

    def test_resolve_does_not_pop_on_success(self) -> None:
        """Callers that need an EXTRA check (e.g. a confirmation token) after
        the owner check must be able to re-validate before consuming — a
        successful `resolve()` leaves the entry in place until the caller
        explicitly `discard()`s it."""
        store = _store()
        store.store("chat-1", "alice", "payload-a")
        store.resolve("chat-1", "alice")
        assert "chat-1" in store
        store.discard("chat-1")
        assert "chat-1" not in store


class TestFailClosedOnAmbiguity:
    def test_store_with_missing_owner_stores_nothing(self) -> None:
        store = _store()
        store.store("chat-1", "", "payload-a")
        store.store("chat-2", None, "payload-b")
        assert "chat-1" not in store
        assert "chat-2" not in store

    def test_store_with_missing_key_stores_nothing(self) -> None:
        store = _store()
        store.store(None, "alice", "payload-a")
        assert len(store) == 0

    def test_resolve_missing_key_returns_none(self) -> None:
        store = _store()
        assert store.resolve("nope", "alice") is None

    def test_resolve_missing_requester_id_returns_none(self) -> None:
        """A caller that can't identify who is asking must never be treated
        as the owner — this is the fail-closed guard for a missing
        conversation id / owner / actor at the call site."""
        store = _store()
        store.store("chat-1", "alice", "payload-a")
        assert store.resolve("chat-1", None) is None
        assert store.resolve("chat-1", "") is None
        # Entry survives — a caller with no identity must not clear it.
        assert "chat-1" in store


class TestOwnerBinding:
    def test_different_owner_does_not_resolve_and_leaves_entry(self) -> None:
        store = _store()
        store.store("chat-1", "alice", "payload-a")
        assert store.resolve("chat-1", "bob") is None
        # Still pending — alice can still resolve it afterwards.
        assert store.resolve("chat-1", "alice") == "payload-a"


class TestTTLExpiry:
    def test_expired_entry_is_dropped_and_resolve_returns_none(self) -> None:
        store = _store(ttl=-1.0)  # already expired the instant it's stored
        store.store("chat-1", "alice", "payload-a")
        assert store.resolve("chat-1", "alice") is None
        assert "chat-1" not in store

    def test_unexpired_entry_within_ttl_resolves(self) -> None:
        store = _store(ttl=60.0)
        store.store("chat-1", "alice", "payload-a")
        assert store.resolve("chat-1", "alice") == "payload-a"

    def test_unparseable_expires_at_treated_as_expired(self) -> None:
        """Defensive fail-closed: an entry with a corrupted/non-numeric
        `expires_at` must never be treated as "never expires"."""
        store = _store()
        store.store("chat-1", "alice", "payload-a")
        # White-box: corrupt the internal record's expiry.
        pending = store._pending["chat-1"]
        store._pending["chat-1"] = pending.__class__(
            owner_id=pending.owner_id,
            payload=pending.payload,
            expires_at="not-a-number",  # type: ignore[arg-type]
        )
        assert store.resolve("chat-1", "alice") is None
        assert "chat-1" not in store


class TestDiscardAndClear:
    def test_discard_missing_key_is_a_no_op(self) -> None:
        store = _store()
        store.discard("nope")  # must not raise

    def test_discard_none_key_is_a_no_op(self) -> None:
        store = _store()
        store.discard(None)  # must not raise

    def test_clear_empties_every_entry(self) -> None:
        store = _store()
        store.store("chat-1", "alice", "payload-a")
        store.store("chat-2", "bob", "payload-b")
        store.clear()
        assert "chat-1" not in store
        assert "chat-2" not in store


class TestOverwrite:
    def test_storing_again_for_same_key_replaces_owner_and_payload(self) -> None:
        store = _store()
        store.store("chat-1", "alice", "payload-a")
        store.store("chat-1", "bob", "payload-b")
        assert store.resolve("chat-1", "alice") is None
        assert store.resolve("chat-1", "bob") == "payload-b"


class TestRealTimeTTL:
    def test_ttl_actually_elapses(self) -> None:
        store = _store(ttl=0.05)
        store.store("chat-1", "alice", "payload-a")
        time.sleep(0.1)
        assert store.resolve("chat-1", "alice") is None
