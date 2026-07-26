"""Tests for hivepilot.services.swarm_service — the Swarm Phase 1 engine
service: publish_event (sign + dedupe), claim_next (verify + tenant-scope +
exactly-once claim), process_claimed_event/dispatch_claimed_event (handler
idempotency), publish_pr_ready (best-effort), and get_signing_key (secret
resolution + masking).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from hivepilot.config import Settings
from hivepilot.services import state_service, swarm_service
from hivepilot.services.config_provenance import redact_text, registered_secret_values
from hivepilot.swarm.models import Event, compute_event_id


def _settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = dict(
        swarm_transport="poll",
        swarm_instance_id="inst-a",
        swarm_key="fleet-shared-key",
        swarm_served_tenants=["default"],
    )
    defaults.update(overrides)
    return Settings(**defaults)


class TestGetInstanceId:
    def test_returns_configured_instance_id(self) -> None:
        assert swarm_service.get_instance_id(_settings(swarm_instance_id="inst-x")) == "inst-x"


class TestGetSigningKey:
    def test_literal_key_returned_and_masked(self) -> None:
        key = swarm_service.get_signing_key(_settings(swarm_key="super-secret-value"))
        assert key == "super-secret-value"
        assert "super-secret-value" in registered_secret_values()
        assert redact_text("leaked super-secret-value here") == "leaked REDACTED here"

    def test_unconfigured_key_returns_none(self) -> None:
        assert swarm_service.get_signing_key(_settings(swarm_key=None)) is None

    def test_secret_ref_resolved_via_catalog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SWARM_KEY_ENV_VAR", "resolved-from-env")
        settings = _settings(
            swarm_key="${secret:SWARM_KEY}",
            swarm_secrets={"SWARM_KEY": {"source": "env", "key": "SWARM_KEY_ENV_VAR"}},
        )
        assert swarm_service.get_signing_key(settings) == "resolved-from-env"

    def test_unresolvable_secret_ref_degrades_to_none_not_raise(self) -> None:
        settings = _settings(swarm_key="${secret:MISSING}", swarm_secrets={})
        assert swarm_service.get_signing_key(settings) is None

    def test_secret_ref_resolving_to_empty_string_is_treated_as_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HIGH #1 fix (opus security review): a `${secret:NAME}` reference
        whose backing value is `""` (a rotation window, a blank `.env` line,
        ...) must degrade to `None` -- IDENTICALLY to "unconfigured" -- never
        become a real, guessable, empty-string HMAC key."""
        monkeypatch.setenv("SWARM_KEY_ENV_VAR", "")
        settings = _settings(
            swarm_key="${secret:SWARM_KEY}",
            swarm_secrets={"SWARM_KEY": {"source": "env", "key": "SWARM_KEY_ENV_VAR"}},
        )
        assert swarm_service.get_signing_key(settings) is None
        assert "" not in registered_secret_values()

    def test_secret_ref_resolving_to_whitespace_only_is_treated_as_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same as above for a whitespace-only resolved value -- `"   "` is
        just as unusable/guessable-adjacent as `""` and must fail closed
        identically."""
        monkeypatch.setenv("SWARM_KEY_ENV_VAR", "   ")
        settings = _settings(
            swarm_key="${secret:SWARM_KEY}",
            swarm_secrets={"SWARM_KEY": {"source": "env", "key": "SWARM_KEY_ENV_VAR"}},
        )
        assert swarm_service.get_signing_key(settings) is None
        assert "   " not in registered_secret_values()

    def test_empty_literal_swarm_key_never_registered_as_secret(self) -> None:
        """The literal (non-`${secret:}`) path already guarded `if not raw`
        before this fix -- confirm it still never registers an empty value
        for masking (an empty "secret" would poison the masker)."""
        assert swarm_service.get_signing_key(_settings(swarm_key="")) is None
        assert swarm_service.get_signing_key(_settings(swarm_key="   ")) is None
        assert "" not in registered_secret_values()


class TestPublishEvent:
    def test_first_publish_returns_published(self) -> None:
        settings = _settings()
        result = swarm_service.publish_event(
            "pr_ready", {"repo": "acme/widgets"}, "default", dedupe_key="r:b:s", settings=settings
        )
        assert result.status == swarm_service.PublishStatus.PUBLISHED

    def test_republish_same_dedupe_key_returns_deduped(self) -> None:
        settings = _settings()
        swarm_service.publish_event(
            "pr_ready", {"repo": "acme/widgets"}, "default", dedupe_key="r:b:s", settings=settings
        )
        result = swarm_service.publish_event(
            "pr_ready", {"repo": "acme/widgets"}, "default", dedupe_key="r:b:s", settings=settings
        )
        assert result.status == swarm_service.PublishStatus.DEDUPED

    def test_publish_without_key_is_skipped_not_raised(self) -> None:
        settings = _settings(swarm_key=None)
        result = swarm_service.publish_event(
            "pr_ready", {"repo": "acme/widgets"}, "default", dedupe_key="r:b:s", settings=settings
        )
        assert result.status == swarm_service.PublishStatus.SKIPPED
        assert state_service.get_swarm_event(result.event_id) is None

    def test_publish_with_secret_ref_resolving_empty_is_skipped_never_signed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HIGH #1 fix, end-to-end: a `${secret:NAME}` reference that
        resolves to `""` must never reach `sign_event` -- `publish_event`
        degrades to `SKIPPED` (same as no key at all), and NOTHING is ever
        persisted/signed with an empty-string HMAC key."""
        monkeypatch.setenv("SWARM_KEY_ENV_VAR", "")
        settings = _settings(
            swarm_key="${secret:SWARM_KEY}",
            swarm_secrets={"SWARM_KEY": {"source": "env", "key": "SWARM_KEY_ENV_VAR"}},
        )
        result = swarm_service.publish_event(
            "pr_ready", {"repo": "acme/widgets"}, "default", dedupe_key="r:b:s", settings=settings
        )
        assert result.status == swarm_service.PublishStatus.SKIPPED
        assert state_service.get_swarm_event(result.event_id) is None

    def test_published_event_is_persisted_and_signed(self) -> None:
        settings = _settings()
        result = swarm_service.publish_event(
            "pr_ready", {"repo": "acme/widgets"}, "default", dedupe_key="r:b:s", settings=settings
        )
        row = state_service.get_swarm_event(result.event_id)
        assert row is not None
        assert row["sig"]
        assert row["status"] == "pending"

    def test_transport_error_is_swallowed_best_effort(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings()

        def _boom(*a, **k):
            raise RuntimeError("transport exploded")

        monkeypatch.setattr(swarm_service, "resolve_transport", _boom)
        result = swarm_service.publish_event(
            "pr_ready", {"repo": "acme/widgets"}, "default", dedupe_key="r:b:s", settings=settings
        )
        # The row is still persisted (poll fallback can still pick it up) —
        # only the live broker hand-off failed.
        assert result.status == swarm_service.PublishStatus.PUBLISHED
        assert state_service.get_swarm_event(result.event_id) is not None


class TestClaimNext:
    def test_claims_eligible_pending_event(self) -> None:
        settings = _settings()
        published = swarm_service.publish_event(
            "pr_ready", {"repo": "acme/widgets"}, "default", dedupe_key="r:b:s", settings=settings
        )
        result = swarm_service.claim_next(["pr_ready"], settings=settings)
        assert result.status == swarm_service.ClaimStatus.CLAIMED
        assert result.event is not None
        assert result.event.id == published.event_id

    def test_no_signing_key_refuses_to_claim(self) -> None:
        publish_settings = _settings()
        swarm_service.publish_event(
            "pr_ready",
            {"repo": "acme/widgets"},
            "default",
            dedupe_key="r:b:s",
            settings=publish_settings,
        )
        claim_settings = _settings(swarm_key=None)
        result = swarm_service.claim_next(["pr_ready"], settings=claim_settings)
        assert result.status != swarm_service.ClaimStatus.CLAIMED

    def test_empty_resolved_secret_ref_refuses_to_claim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HIGH #1 fix, claim-side: a `${secret:NAME}` reference resolving to
        `""` must be treated identically to "no key configured" -- `claim_next`
        never attempts to verify/claim anything."""
        publish_settings = _settings()
        swarm_service.publish_event(
            "pr_ready",
            {"repo": "acme/widgets"},
            "default",
            dedupe_key="r:b:s",
            settings=publish_settings,
        )
        monkeypatch.setenv("SWARM_KEY_ENV_VAR", "")
        claim_settings = _settings(
            swarm_key="${secret:SWARM_KEY}",
            swarm_secrets={"SWARM_KEY": {"source": "env", "key": "SWARM_KEY_ENV_VAR"}},
        )
        result = swarm_service.claim_next(["pr_ready"], settings=claim_settings)
        assert result.status == swarm_service.ClaimStatus.NONE
        assert result.event is None
        # Still pending — an unconfigured/empty key must never consume it.
        assert len(state_service.list_pending_swarm_events()) == 1

    def test_empty_queue_returns_none_status(self) -> None:
        result = swarm_service.claim_next(["pr_ready"], settings=_settings())
        assert result.status == swarm_service.ClaimStatus.NONE
        assert result.event is None

    def test_two_instances_race_exactly_one_claimed_other_deduped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulates a TRUE race: both instances already saw the event as
        `pending` in an earlier poll cycle (a real `PollTransport.subscribe`
        would no longer yield it to a SECOND caller once the first has
        claimed it — see `test_swarm_poll_transport.py`'s own exactly-once
        coverage of that). A fake transport that always yields the SAME
        static event (regardless of its current DB status) reproduces that
        race window deterministically, while `claim()` still delegates to
        the REAL `state_service.claim_swarm_event` atomic update — the exact
        function both real transports call."""
        settings = _settings()
        published = swarm_service.publish_event(
            "pr_ready", {"repo": "acme/widgets"}, "default", dedupe_key="r:b:s", settings=settings
        )
        stale_event = Event(
            id=published.event_id,
            type="pr_ready",
            payload={"repo": "acme/widgets"},
            tenant="default",
            origin_instance="inst-publisher",
            sig=swarm_service.sign_event(
                Event(
                    id=published.event_id,
                    type="pr_ready",
                    payload={"repo": "acme/widgets"},
                    tenant="default",
                    origin_instance="inst-publisher",
                ),
                "fleet-shared-key",
            ),
        )

        class _StaticTransport:
            name = "poll"

            def __init__(self, *, settings=None, instance_id) -> None:
                self._instance_id = instance_id

            def subscribe(self, types):
                yield stale_event

            def claim(self, event_id: str) -> bool:
                return state_service.claim_swarm_event(event_id, claimed_by=self._instance_id)

            def ack(self, event_id: str) -> None:
                pass

            def complete(self, event_id: str) -> None:
                pass

        monkeypatch.setattr(
            swarm_service, "resolve_transport", lambda *a, **k: _StaticTransport(**k)
        )

        settings_a = _settings(swarm_instance_id="inst-a")
        settings_b = _settings(swarm_instance_id="inst-b")

        result_a = swarm_service.claim_next(["pr_ready"], settings=settings_a)
        result_b = swarm_service.claim_next(["pr_ready"], settings=settings_b)

        statuses = {result_a.status, result_b.status}
        assert statuses == {swarm_service.ClaimStatus.CLAIMED, swarm_service.ClaimStatus.DEDUPED}

    def test_wrong_tenant_never_claimed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        handler = MagicMock()
        monkeypatch.setitem(swarm_service._EVENT_HANDLERS, "pr_ready", lambda e, o: handler(e))
        publish_settings = _settings()
        swarm_service.publish_event(
            "pr_ready",
            {"repo": "acme/widgets"},
            "tenant-b",
            dedupe_key="r:b:s",
            settings=publish_settings,
        )
        claim_settings = _settings(swarm_served_tenants=["tenant-a"])
        result = swarm_service.claim_next(["pr_ready"], settings=claim_settings)
        assert result.status != swarm_service.ClaimStatus.CLAIMED
        # Still pending — available for a tenant-b-serving instance.
        pending = state_service.list_pending_swarm_events(tenants=["tenant-b"])
        assert len(pending) == 1
        if result.event is not None:
            swarm_service.dispatch_claimed_event(result.event, orchestrator=MagicMock())
        handler.assert_not_called()

    def test_correct_tenant_can_claim_after_wrong_tenant_checked(self) -> None:
        publish_settings = _settings()
        swarm_service.publish_event(
            "pr_ready",
            {"repo": "acme/widgets"},
            "tenant-b",
            dedupe_key="r:b:s",
            settings=publish_settings,
        )
        wrong_tenant_settings = _settings(swarm_served_tenants=["tenant-a"])
        swarm_service.claim_next(["pr_ready"], settings=wrong_tenant_settings)

        right_tenant_settings = _settings(swarm_served_tenants=["tenant-b"])
        result = swarm_service.claim_next(["pr_ready"], settings=right_tenant_settings)
        assert result.status == swarm_service.ClaimStatus.CLAIMED

    def test_bad_signature_event_is_rejected_never_claimed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler = MagicMock()
        monkeypatch.setitem(swarm_service._EVENT_HANDLERS, "pr_ready", lambda e, o: handler(e))
        publish_settings = _settings(swarm_key="key-one")
        swarm_service.publish_event(
            "pr_ready",
            {"repo": "acme/widgets"},
            "default",
            dedupe_key="r:b:s",
            settings=publish_settings,
        )
        # A different instance with a DIFFERENT key can never verify it.
        claim_settings = _settings(swarm_key="key-two")
        result = swarm_service.claim_next(["pr_ready"], settings=claim_settings)
        assert result.status != swarm_service.ClaimStatus.CLAIMED

        row_before = state_service.list_pending_swarm_events()
        assert row_before == []  # marked skipped, not left claimable forever

        # A bad-signature event must never reach a handler — there is no
        # event object to dispatch at all (`result.event is None`, proven
        # below), but assert directly against the registered handler too so
        # this test fails loudly if that claim-result contract ever regresses.
        if result.event is not None:
            swarm_service.dispatch_claimed_event(result.event, orchestrator=MagicMock())
        handler.assert_not_called()

    def test_key_never_appears_in_any_exception_or_log_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[str] = []

        def _capture_warning(event_name, **kwargs):
            captured.append(str(kwargs))

        publish_settings = _settings(swarm_key="ultra-secret-fleet-key")
        swarm_service.publish_event(
            "pr_ready",
            {"repo": "acme/widgets"},
            "default",
            dedupe_key="r:b:s",
            settings=publish_settings,
        )
        claim_settings = _settings(swarm_key="a-totally-different-key")
        monkeypatch.setattr(swarm_service.logger, "warning", _capture_warning)
        swarm_service.claim_next(["pr_ready"], settings=claim_settings)
        for entry in captured:
            assert "ultra-secret-fleet-key" not in entry
            assert "a-totally-different-key" not in entry


class TestSwarmOutcomeLogging:
    """Bug-debt fix: every publish/claim outcome must log `event_id`,
    `type`, `tenant`, `instance_id`, `outcome` (CLAIMED/DEDUPED/SKIPPED/
    REJECTED) and, for anything other than CLAIMED, the REASON (bad
    signature / no key / unserved tenant / not pending)."""

    def _rendered(self, caplog: pytest.LogCaptureFixture) -> str:
        return "\n".join(
            [r.getMessage() for r in caplog.records] + [str(r.msg) for r in caplog.records]
        )

    def test_publish_success_logs_published_outcome(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _settings()
        with caplog.at_level("INFO"):
            result = swarm_service.publish_event(
                "pr_ready",
                {"repo": "acme/widgets"},
                "default",
                dedupe_key="r:pub:s",
                settings=settings,
            )
        rendered = self._rendered(caplog)
        assert result.event_id in rendered
        assert '"tenant": "default"' in rendered
        assert '"instance_id": "inst-a"' in rendered
        assert '"outcome": "PUBLISHED"' in rendered

    def test_publish_deduped_logs_deduped_outcome(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _settings()
        swarm_service.publish_event(
            "pr_ready", {"repo": "acme/widgets"}, "default", dedupe_key="r:dup:s", settings=settings
        )
        with caplog.at_level("INFO"):
            swarm_service.publish_event(
                "pr_ready",
                {"repo": "acme/widgets"},
                "default",
                dedupe_key="r:dup:s",
                settings=settings,
            )
        rendered = self._rendered(caplog)
        assert '"outcome": "DEDUPED"' in rendered
        assert '"reason": "already_published"' in rendered

    def test_publish_no_key_logs_skipped_with_no_key_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = _settings(swarm_key=None)
        with caplog.at_level("WARNING"):
            swarm_service.publish_event(
                "pr_ready",
                {"repo": "acme/widgets"},
                "default",
                dedupe_key="r:nokey:s",
                settings=settings,
            )
        rendered = self._rendered(caplog)
        assert '"outcome": "SKIPPED"' in rendered
        assert '"reason": "no_key"' in rendered

    def test_claim_success_logs_claimed_outcome(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _settings()
        swarm_service.publish_event(
            "pr_ready",
            {"repo": "acme/widgets"},
            "default",
            dedupe_key="r:claim:s",
            settings=settings,
        )
        with caplog.at_level("INFO"):
            result = swarm_service.claim_next(["pr_ready"], settings=settings)
        rendered = self._rendered(caplog)
        assert result.event is not None
        assert result.event.id in rendered
        assert '"outcome": "CLAIMED"' in rendered
        assert '"instance_id": "inst-a"' in rendered

    def test_unserved_tenant_logs_rejected_with_reason(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`PollTransport.subscribe` already filters unserved tenants out in
        SQL (item 1's fail-closed fix) -- so this instance's own
        `claim_next` loop never even SEES an unserved-tenant candidate via
        the real transport. A minimal transport double that yields one
        anyway (mirrors `hivepilot.swarm.redis_transport.RedisTransport`,
        which has NO tenant filter of its own -- see its `subscribe`
        docstring) exercises the loop's own defense-in-depth rejection
        branch directly."""
        publish_settings = _settings()
        swarm_service.publish_event(
            "pr_ready",
            {"repo": "acme/widgets"},
            "tenant-b",
            dedupe_key="r:unserved:s",
            settings=publish_settings,
        )
        unserved_event = next(
            e
            for e in swarm_service.resolve_transport(
                "poll",
                instance_id="inst-x",
                settings=publish_settings.model_copy(update={"swarm_served_tenants": ["tenant-b"]}),
            ).subscribe(["pr_ready"])
        )

        class _UnfilteredTransport:
            name = "poll"

            def __init__(self, *, settings=None, instance_id) -> None:
                pass

            def subscribe(self, types):
                yield unserved_event

            def claim(self, event_id: str) -> bool:
                raise AssertionError("must never attempt to claim a rejected candidate")

            def ack(self, event_id: str) -> None:
                pass

            def complete(self, event_id: str) -> None:
                pass

        monkeypatch.setattr(
            swarm_service, "resolve_transport", lambda *a, **k: _UnfilteredTransport(**k)
        )
        claim_settings = _settings(swarm_served_tenants=["tenant-a"])
        with caplog.at_level("WARNING"):
            swarm_service.claim_next(["pr_ready"], settings=claim_settings)
        rendered = self._rendered(caplog)
        assert '"outcome": "REJECTED"' in rendered
        assert '"reason": "unserved_tenant"' in rendered
        assert '"tenant": "tenant-b"' in rendered

    def test_bad_signature_logs_rejected_with_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        publish_settings = _settings(swarm_key="key-one")
        swarm_service.publish_event(
            "pr_ready",
            {"repo": "acme/widgets"},
            "default",
            dedupe_key="r:badsig:s",
            settings=publish_settings,
        )
        claim_settings = _settings(swarm_key="key-two")
        with caplog.at_level("WARNING"):
            swarm_service.claim_next(["pr_ready"], settings=claim_settings)
        rendered = self._rendered(caplog)
        assert '"outcome": "REJECTED"' in rendered
        assert '"reason": "bad_signature"' in rendered

    def test_claim_race_lost_logs_deduped_with_not_pending_reason(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Mirrors `TestClaimNext.test_two_instances_race_exactly_one_claimed_
        other_deduped`'s stale-transport double: a real `PollTransport.
        subscribe` would no longer even yield an already-claimed row (it's
        no longer `pending`), so a static transport that always yields the
        SAME event regardless of DB status reproduces the exact race window
        where `transport.claim` (delegating to the REAL, atomic
        `state_service.claim_swarm_event`) loses."""
        settings = _settings()
        published = swarm_service.publish_event(
            "pr_ready",
            {"repo": "acme/widgets"},
            "default",
            dedupe_key="r:race:s",
            settings=settings,
        )
        stale_event = Event(
            id=published.event_id,
            type="pr_ready",
            payload={"repo": "acme/widgets"},
            tenant="default",
            origin_instance="inst-publisher",
            sig=swarm_service.sign_event(
                Event(
                    id=published.event_id,
                    type="pr_ready",
                    payload={"repo": "acme/widgets"},
                    tenant="default",
                    origin_instance="inst-publisher",
                ),
                "fleet-shared-key",
            ),
        )
        # The event is already claimed by someone else by the time THIS
        # instance tries -- `transport.claim` (the REAL, atomic
        # `state_service.claim_swarm_event`) returns False (not pending).
        state_service.claim_swarm_event(published.event_id, claimed_by="inst-other")

        class _StaticTransport:
            name = "poll"

            def __init__(self, *, settings=None, instance_id) -> None:
                self._instance_id = instance_id

            def subscribe(self, types):
                yield stale_event

            def claim(self, event_id: str) -> bool:
                return state_service.claim_swarm_event(event_id, claimed_by=self._instance_id)

            def ack(self, event_id: str) -> None:
                pass

            def complete(self, event_id: str) -> None:
                pass

        monkeypatch.setattr(
            swarm_service, "resolve_transport", lambda *a, **k: _StaticTransport(**k)
        )

        with caplog.at_level("INFO"):
            result = swarm_service.claim_next(["pr_ready"], settings=settings)
        assert result.status == swarm_service.ClaimStatus.DEDUPED
        rendered = self._rendered(caplog)
        assert '"outcome": "DEDUPED"' in rendered
        assert '"reason": "not_pending"' in rendered

    def test_claim_no_key_logs_skipped_with_no_key_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        claim_settings = _settings(swarm_key=None)
        with caplog.at_level("WARNING"):
            swarm_service.claim_next(["pr_ready"], settings=claim_settings)
        rendered = self._rendered(caplog)
        assert '"outcome": "SKIPPED"' in rendered
        assert '"reason": "no_key"' in rendered


class TestProcessClaimedEvent:
    def _claimed_event(self, settings) -> Event:
        swarm_service.publish_event(
            "pr_ready", {"repo": "acme/widgets"}, "default", dedupe_key="r:b:s", settings=settings
        )
        result = swarm_service.claim_next(["pr_ready"], settings=settings)
        assert result.event is not None
        return result.event

    def test_handler_invoked_once_for_a_claimed_event(self) -> None:
        settings = _settings()
        event = self._claimed_event(settings)
        handler = MagicMock()
        # `settings.swarm_instance_id` ("inst-a" per `_settings()`'s
        # defaults) is who actually holds this claim (see `_claimed_event`'s
        # `claim_next(settings=settings)` call) -- the atomic `claimed_by`
        # gate (HIGH #2 fix) requires the SAME instance identity here.
        assert swarm_service.process_claimed_event(event, handler, settings=settings) is True
        handler.assert_called_once_with(event)

    def test_duplicate_processing_is_a_no_op(self) -> None:
        settings = _settings()
        event = self._claimed_event(settings)
        handler = MagicMock()
        assert swarm_service.process_claimed_event(event, handler, settings=settings) is True
        assert swarm_service.process_claimed_event(event, handler, settings=settings) is False
        handler.assert_called_once()  # NOT called twice

    def test_unclaimed_event_is_never_processed(self) -> None:
        settings = _settings()
        published = swarm_service.publish_event(
            "pr_ready", {"repo": "acme/widgets"}, "default", dedupe_key="r:b:s", settings=settings
        )
        unclaimed = Event(
            id=published.event_id,
            type="pr_ready",
            payload={},
            tenant="default",
            origin_instance="inst-a",
        )
        handler = MagicMock()
        assert swarm_service.process_claimed_event(unclaimed, handler, settings=settings) is False
        handler.assert_not_called()

    def test_race_loser_instance_never_invokes_handler(self) -> None:
        """HIGH #2 fix, direct proof: instance A wins the claim; instance B
        (a DIFFERENT `instance_id`) must NEVER get to run the handler for it,
        even if it somehow got hold of the event object (e.g. a caller that
        ignored `ClaimResult.event is None` on a `DEDUPED` result, or any
        other path that hands B the same `Event`). Before this fix,
        `process_claimed_event` only checked `status == 'claimed'` — it never
        checked WHO claimed it — so B would have run the handler too."""
        winner_settings = _settings(swarm_instance_id="inst-a")
        event = self._claimed_event(winner_settings)  # inst-a is the true owner

        handler = MagicMock()
        # inst-b never won this claim — the atomic `claimed_by`-gated
        # transition must reject it outright.
        assert swarm_service.process_claimed_event(event, handler, instance_id="inst-b") is False
        handler.assert_not_called()

        # The TRUE owner can still process it afterwards — the race-loser's
        # failed attempt must not have poisoned the row for the real owner.
        assert swarm_service.process_claimed_event(event, handler, instance_id="inst-a") is True
        handler.assert_called_once_with(event)


class TestProcessClaimedEventHandlerFailure:
    """Bug-debt fix: a crashing handler must never leave the row `running`
    forever (dead, unclaimable, silent) -- it must be logged explicitly and
    land in the documented terminal state, `failed`, and that event can
    never be silently re-claimed by anyone afterwards."""

    def _claimed_event(self, settings) -> Event:
        swarm_service.publish_event(
            "pr_ready",
            {"repo": "acme/widgets"},
            "default",
            dedupe_key="r:boom:s",
            settings=settings,
        )
        result = swarm_service.claim_next(["pr_ready"], settings=settings)
        assert result.event is not None
        return result.event

    def test_raising_handler_logs_and_lands_in_failed_not_running(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = _settings()
        event = self._claimed_event(settings)

        def _boom(_event: Event) -> None:
            raise RuntimeError("handler exploded")

        with caplog.at_level("ERROR"):
            with pytest.raises(RuntimeError, match="handler exploded"):
                swarm_service.process_claimed_event(event, _boom, settings=settings)

        row = state_service.get_swarm_event(event.id)
        assert row is not None
        assert row["status"] == "failed"
        assert row["status"] != "running"

        failure_records = [r for r in caplog.records if "swarm.handler_failed" in r.message]
        assert failure_records

    def test_failed_event_can_never_be_silently_reclaimed(self) -> None:
        settings = _settings()
        event = self._claimed_event(settings)

        def _boom(_event: Event) -> None:
            raise RuntimeError("handler exploded")

        with pytest.raises(RuntimeError):
            swarm_service.process_claimed_event(event, _boom, settings=settings)

        # Neither this instance nor any other can ever claim it again — the
        # row is `failed`, not `pending`, so `claim_swarm_event`'s
        # `WHERE status='pending'` gate structurally forbids it.
        assert state_service.claim_swarm_event(event.id, claimed_by="inst-a") is False
        assert state_service.claim_swarm_event(event.id, claimed_by="inst-b") is False

    def test_a_handler_that_succeeds_after_a_failure_on_a_different_event_is_unaffected(
        self,
    ) -> None:
        """Sanity: the fix is scoped to the FAILING event's own row -- it
        must not somehow poison a completely different, healthy claim."""
        settings = _settings()
        failing_event = self._claimed_event(settings)

        def _boom(_event: Event) -> None:
            raise RuntimeError("handler exploded")

        with pytest.raises(RuntimeError):
            swarm_service.process_claimed_event(failing_event, _boom, settings=settings)

        swarm_service.publish_event(
            "pr_ready", {"repo": "acme/widgets"}, "default", dedupe_key="r:ok:s", settings=settings
        )
        ok_result = swarm_service.claim_next(["pr_ready"], settings=settings)
        assert ok_result.event is not None
        handler = MagicMock()
        assert (
            swarm_service.process_claimed_event(ok_result.event, handler, settings=settings) is True
        )
        handler.assert_called_once_with(ok_result.event)


class TestDispatchClaimedEvent:
    def test_pr_ready_dispatches_to_run_pipeline(self) -> None:
        settings = _settings()
        swarm_service.publish_event(
            "pr_ready",
            {"project": "widgets", "branch": "hivepilot/x"},
            "default",
            dedupe_key="r:b:s",
            settings=settings,
        )
        result = swarm_service.claim_next(["pr_ready"], settings=settings)
        assert result.event is not None
        orchestrator = MagicMock()
        assert (
            swarm_service.dispatch_claimed_event(
                result.event, orchestrator=orchestrator, settings=settings
            )
            is True
        )
        orchestrator.run_pipeline.assert_called_once()
        call_kwargs = orchestrator.run_pipeline.call_args.kwargs
        assert call_kwargs["project_names"] == ["widgets"]
        assert call_kwargs["auto_git"] is False

    def test_unknown_event_type_returns_false_no_crash(self) -> None:
        event = Event(
            id=compute_event_id("mystery_type", "x"),
            type="mystery_type",
            payload={},
            tenant="default",
            origin_instance="inst-a",
        )
        assert swarm_service.dispatch_claimed_event(event, orchestrator=MagicMock()) is False

    def test_race_loser_instance_never_dispatches_to_handler(self) -> None:
        """HIGH #2 fix, at the `dispatch_claimed_event` layer (the seam a real
        fleet worker actually calls): a race-LOSER instance id must never
        trigger `orchestrator.run_pipeline` for an event another instance
        owns."""
        settings = _settings(swarm_instance_id="inst-a")
        swarm_service.publish_event(
            "pr_ready",
            {"project": "widgets", "branch": "hivepilot/x"},
            "default",
            dedupe_key="r:b:s",
            settings=settings,
        )
        result = swarm_service.claim_next(["pr_ready"], settings=settings)
        assert result.event is not None

        orchestrator = MagicMock()
        assert (
            swarm_service.dispatch_claimed_event(
                result.event, orchestrator=orchestrator, instance_id="inst-b"
            )
            is False
        )
        orchestrator.run_pipeline.assert_not_called()


class TestPublishPrReady:
    def test_publishes_with_dedupe_key_from_repo_branch_sha(self) -> None:
        settings = _settings()
        result = swarm_service.publish_pr_ready(
            project_name="widgets",
            owner_repo="acme/widgets",
            branch="hivepilot/widgets",
            sha="deadbeef",
            settings=settings,
        )
        assert result.status == swarm_service.PublishStatus.PUBLISHED

    def test_never_raises_even_on_internal_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*a, **k):
            raise RuntimeError("state db exploded")

        monkeypatch.setattr(swarm_service, "publish_event", _boom)
        # Must not raise — best-effort contract for the git_service caller.
        result = swarm_service.publish_pr_ready(
            project_name="widgets",
            owner_repo="acme/widgets",
            branch="hivepilot/widgets",
            sha="deadbeef",
            settings=_settings(),
        )
        assert result.status == swarm_service.PublishStatus.SKIPPED
