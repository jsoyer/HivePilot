"""Unit tests for `hivepilot.outward` -- the run-scoped outward-action
permission carrier (propose -> ratify -> dispatch PRD, spec section 6 /
open question 4).

These cover the CARRIER itself. The end-to-end suppression at each engine
choke point lives in `tests/test_outward_runtime_enforcement.py`.
"""

from __future__ import annotations

import threading

import pytest

from hivepilot import outward

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_outward_actions_is_the_closed_vocabulary_from_the_spec() -> None:
    assert outward.OUTWARD_ACTIONS == frozenset(
        {
            "git_push",
            "forge_pr",
            "forge_merge",
            "forge_issue",
            "forge_release",
            "notify",
            "vault_write",
            "external_api",
        }
    )


def test_autopilot_queue_reexports_the_same_frozenset_object() -> None:
    """One vocabulary, one object -- the two can never drift apart."""
    from hivepilot.services import autopilot_queue

    assert autopilot_queue.OUTWARD_ACTIONS is outward.OUTWARD_ACTIONS


# ---------------------------------------------------------------------------
# OutwardPermission
# ---------------------------------------------------------------------------


def test_unrestricted_permits_every_known_action() -> None:
    perm = outward.OutwardPermission.unrestricted()
    assert perm.restricted is False
    for action in outward.OUTWARD_ACTIONS:
        assert perm.allows(action) is True


def test_unknown_action_is_always_denied_even_when_unrestricted() -> None:
    """Fail closed: a token outside the closed vocabulary is never allowed,
    in either mode. An unrecognised permission question must never answer
    'sure, go ahead'."""
    perm = outward.OutwardPermission.unrestricted()
    assert perm.allows("teleport") is False
    assert outward.OutwardPermission.restricted_to(["teleport"]).allows("teleport") is False


def test_restricted_to_empty_denies_everything() -> None:
    perm = outward.OutwardPermission.restricted_to([])
    assert perm.restricted is True
    for action in outward.OUTWARD_ACTIONS:
        assert perm.allows(action) is False


def test_restricted_to_drops_unknown_tokens_rather_than_honouring_them() -> None:
    perm = outward.OutwardPermission.restricted_to(["notify", "nonsense"])
    assert perm.allowed == frozenset({"notify"})
    assert perm.allows("notify") is True
    assert perm.allows("nonsense") is False


def test_restricted_permission_denies_what_it_does_not_list() -> None:
    perm = outward.OutwardPermission.restricted_to(["git_push"])
    assert perm.allows("git_push") is True
    assert perm.allows("notify") is False
    assert perm.allows("vault_write") is False
    assert perm.allows("external_api") is False


def test_permission_is_frozen() -> None:
    perm = outward.OutwardPermission.restricted_to(["notify"])
    with pytest.raises(Exception):
        perm.allowed = frozenset()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# narrow(): a scope can only ever SHRINK
# ---------------------------------------------------------------------------


def test_narrow_with_none_is_identity() -> None:
    perm = outward.OutwardPermission.restricted_to(["notify"])
    assert perm.narrow(None) is perm


def test_narrow_never_widens_an_active_restriction() -> None:
    restricted = outward.OutwardPermission.restricted_to(["notify"])
    assert restricted.narrow(outward.OutwardPermission.unrestricted()) is restricted


def test_narrow_intersects_two_restrictions() -> None:
    a = outward.OutwardPermission.restricted_to(["notify", "git_push"])
    b = outward.OutwardPermission.restricted_to(["git_push", "vault_write"])
    assert a.narrow(b).allowed == frozenset({"git_push"})


def test_unrestricted_narrowed_by_a_restriction_becomes_restricted() -> None:
    perm = outward.OutwardPermission.unrestricted().narrow(
        outward.OutwardPermission.restricted_to(["notify"])
    )
    assert perm.restricted is True
    assert perm.allowed == frozenset({"notify"})


# ---------------------------------------------------------------------------
# scope() / current()
# ---------------------------------------------------------------------------


def test_current_defaults_to_unrestricted_outside_any_scope() -> None:
    assert outward.current().restricted is False
    assert outward.allows("notify") is True


def test_scope_restricts_then_restores() -> None:
    with outward.scope(outward.OutwardPermission.restricted_to([])):
        assert outward.allows("notify") is False
    assert outward.allows("notify") is True


def test_nested_scope_can_only_shrink_never_widen() -> None:
    with outward.scope(outward.OutwardPermission.restricted_to([])):
        with outward.scope(outward.OutwardPermission.unrestricted()):
            assert outward.allows("notify") is False
        assert outward.allows("notify") is False


def test_scope_with_none_inherits_rather_than_resetting() -> None:
    """`run_pipeline(outward=None)` nested inside a partition-dispatched run
    must not clear the restriction -- that would be the exact absent-value
    fail-open this repo keeps re-learning."""
    with outward.scope(outward.OutwardPermission.restricted_to([])):
        with outward.scope(None):
            assert outward.allows("notify") is False


def test_scope_is_reset_even_when_the_body_raises() -> None:
    with pytest.raises(RuntimeError):
        with outward.scope(outward.OutwardPermission.restricted_to([])):
            raise RuntimeError("boom")
    assert outward.allows("notify") is True


def test_capture_and_adopt_carry_a_scope_across_a_thread_boundary() -> None:
    """contextvars are NOT inherited by a new thread -- the explicit
    capture/adopt pair is what makes the pool-worker hop safe."""
    seen: list[bool] = []

    with outward.scope(outward.OutwardPermission.restricted_to([])):
        captured = outward.capture()

        def _worker() -> None:
            with outward.adopt(captured):
                seen.append(outward.allows("notify"))

        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join()

    assert seen == [False]


def test_a_thread_without_adopt_does_not_inherit_the_scope() -> None:
    """Documents the exact hazard `adopt` exists to close: this is why the
    orchestrator's ThreadPoolExecutor wrapper must re-establish the scope."""
    seen: list[bool] = []

    with outward.scope(outward.OutwardPermission.restricted_to([])):
        thread = threading.Thread(target=lambda: seen.append(outward.allows("notify")))
        thread.start()
        thread.join()

    assert seen == [True]


# ---------------------------------------------------------------------------
# permits()/enforce(): never silent
# ---------------------------------------------------------------------------


def test_permits_returns_true_and_records_nothing_when_unrestricted() -> None:
    mark = outward.ledger_mark()
    assert outward.permits("notify", surface="test") is True
    assert outward.suppressions_since(mark) == ()


def test_permits_records_a_suppression_event_when_denied() -> None:
    mark = outward.ledger_mark()
    with outward.scope(outward.OutwardPermission.restricted_to([], label="partition p1")):
        assert not outward.permits(
            "notify", surface="notification_service.send", detail="2 channels"
        )
    events = outward.suppressions_since(mark)
    assert len(events) == 1
    assert events[0].action == "notify"
    assert events[0].surface == "notification_service.send"
    assert events[0].detail == "2 channels"
    assert events[0].scope_label == "partition p1"


def test_permits_logs_a_warning_when_it_suppresses(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        with outward.scope(outward.OutwardPermission.restricted_to([])):
            outward.permits("vault_write", surface="obsidian_service.write")
    assert "outward.suppressed" in caplog.text


def test_enforce_raises_and_records_when_denied() -> None:
    mark = outward.ledger_mark()
    with outward.scope(outward.OutwardPermission.restricted_to([])):
        with pytest.raises(outward.OutwardActionDenied) as excinfo:
            outward.enforce("external_api", surface="registry.resolve_runner_class", detail="acme")
    assert "external_api" in str(excinfo.value)
    assert len(outward.suppressions_since(mark)) == 1


def test_enforce_is_a_no_op_when_allowed() -> None:
    mark = outward.ledger_mark()
    with outward.scope(outward.OutwardPermission.restricted_to(["external_api"])):
        outward.enforce("external_api", surface="registry.resolve_runner_class")
    assert outward.suppressions_since(mark) == ()


def test_summarise_suppressions_counts_per_action() -> None:
    mark = outward.ledger_mark()
    with outward.scope(outward.OutwardPermission.restricted_to([])):
        outward.permits("notify", surface="a")
        outward.permits("notify", surface="b")
        outward.permits("vault_write", surface="c")
    assert outward.summarise_suppressions(mark) == {"notify": 2, "vault_write": 1}


def test_summarise_suppressions_is_empty_when_nothing_was_suppressed() -> None:
    mark = outward.ledger_mark()
    assert outward.summarise_suppressions(mark) == {}
