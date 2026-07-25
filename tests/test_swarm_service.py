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

    def test_wrong_tenant_never_claimed(self) -> None:
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

    def test_bad_signature_event_is_rejected_never_claimed(self) -> None:
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
        assert swarm_service.process_claimed_event(event, handler) is True
        handler.assert_called_once_with(event)

    def test_duplicate_processing_is_a_no_op(self) -> None:
        settings = _settings()
        event = self._claimed_event(settings)
        handler = MagicMock()
        assert swarm_service.process_claimed_event(event, handler) is True
        assert swarm_service.process_claimed_event(event, handler) is False
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
        assert swarm_service.process_claimed_event(unclaimed, handler) is False
        handler.assert_not_called()


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
        assert swarm_service.dispatch_claimed_event(result.event, orchestrator=orchestrator) is True
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
