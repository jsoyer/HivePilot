"""An infrastructure failure must not be reported as "I didn't understand you".

THE LIVE DEFECT: an operator asked the Telegram concierge a real, substantive
question. The classifier call hit a hardcoded 30-second ceiling — the log shows
`concierge.classify_error` with `timed out after 30 seconds` — and the bot
replied:

    "I didn't quite get that. Try rephrasing your request, or use /help to see
     the available commands."

Both halves were wrong. The classifier had not read a single word, so the
message was not the problem; and rephrasing could not help, because the same
timeout would fire again. `prompts/concierge.md` even instructs the model
"NEVER reply with a generic 'I didn't understand' filler when you DID
understand the question" — the instruction was right, the model never got to
follow it, and the transport substituted exactly the filler the prompt forbids.

Two fixes, pinned here:

1. the ceiling is configurable and defaults higher (the prompt now carries the
   whole roster + context + instruction file, so 30s was firing on healthy
   calls, not hung ones), and
2. a transport failure gets its own message that names the real cause and tells
   the operator the useful thing — resend as-is.
"""

from __future__ import annotations

import pytest

from hivepilot.services import concierge_service as cs


class TestTimeoutIsConfigurableAndFailsSafe:
    def test_default_is_higher_than_the_thirty_seconds_that_broke(self) -> None:
        assert cs._CLASSIFIER_TIMEOUT_DEFAULT_SECONDS > 30

    def test_an_explicit_override_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cs.settings, "chatops_concierge_timeout_seconds", 150, raising=False)
        assert cs._classifier_timeout_seconds() == 150

    @pytest.mark.parametrize("bad", [0, -1, -300, None, "", "abc", "30s", [], {}])
    def test_a_malformed_or_nonpositive_override_never_means_unbounded(
        self, bad: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-SAFE, not fail-open-to-infinity.

        This is the repo's recurring empty-value bug class pointed at a
        timeout: reading a blank/garbage ceiling as "no ceiling" would hang the
        chat bot process on one bad message, which is precisely what the
        ceiling exists to prevent. Zero and negatives are the sharp cases — a
        naive `int(raw) or default` would let `0` through as falsy-to-default by
        luck, and `-1` through as a real value.
        """
        monkeypatch.setattr(cs.settings, "chatops_concierge_timeout_seconds", bad, raising=False)
        resolved = cs._classifier_timeout_seconds()
        assert resolved == cs._CLASSIFIER_TIMEOUT_DEFAULT_SECONDS
        assert resolved > 0

    def test_resolved_at_call_time_not_import_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An operator retuning the ceiling should not need a daemon restart."""
        monkeypatch.setattr(cs.settings, "chatops_concierge_timeout_seconds", 45, raising=False)
        first = cs._classifier_timeout_seconds()
        monkeypatch.setattr(cs.settings, "chatops_concierge_timeout_seconds", 200, raising=False)
        assert first == 45
        assert cs._classifier_timeout_seconds() == 200


class TestTheTwoFallbacksAreDistinct:
    def test_they_are_not_the_same_string(self) -> None:
        assert cs._INFRASTRUCTURE_FALLBACK_ANSWER != cs._FALLBACK_ANSWER

    def test_the_infrastructure_message_does_not_blame_the_operator(self) -> None:
        text = cs._INFRASTRUCTURE_FALLBACK_ANSWER.lower()
        # It must not ask for a rewrite — that was the wasted advice.
        assert "rephras" not in text
        assert "didn't quite get that" not in text

    def test_the_infrastructure_message_says_what_actually_helps(self) -> None:
        text = cs._INFRASTRUCTURE_FALLBACK_ANSWER.lower()
        # Names the real cause, tells them to resend unchanged, and points the
        # operator at the log event they can actually grep for.
        assert "my side" in text or "on my side" in text
        assert "again" in text
        assert "concierge.classify_error" in cs._INFRASTRUCTURE_FALLBACK_ANSWER

    def test_the_comprehension_fallback_is_left_alone(self) -> None:
        """The generic filler is still correct for its own case (the model
        answered, unparseably). This change narrows WHEN it is used, it does
        not reword it."""
        assert "rephrasing" in cs._FALLBACK_ANSWER


class TestClassifyErrorUsesTheInfrastructureMessage:
    def test_a_classifier_exception_degrades_to_the_honest_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact live path: `capture_definition` raising (a timeout is
        surfaced as an exception) must not produce the comprehension filler."""

        class _Boom:
            class registry:  # noqa: N801 - mirrors the attribute path under test
                @staticmethod
                def capture_definition(*_a: object, **_k: object) -> str:
                    raise TimeoutError("Command '['claude', ...]' timed out after 30 seconds")

        monkeypatch.setattr(cs, "_get_orchestrator", lambda: _Boom())

        monkeypatch.setattr(cs.settings, "chatops_concierge_enabled", True, raising=False)
        decision = cs.route(
            "j'ai eu un cas d'usage hier ou j'aimerais qu'on reflechisse",
            default_role="cto",
            default_target="acme",
        )

        assert decision.kind == "answer"
        assert decision.answer_text == cs._INFRASTRUCTURE_FALLBACK_ANSWER
        assert decision.answer_text != cs._FALLBACK_ANSWER

    def test_it_is_still_fail_closed_never_an_action(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Degrading more honestly must not degrade less safely: a transport
        failure still yields a non-destructive `answer`, never a fabricated
        route/action."""

        class _Boom:
            class registry:  # noqa: N801
                @staticmethod
                def capture_definition(*_a: object, **_k: object) -> str:
                    raise RuntimeError("transport gone")

        monkeypatch.setattr(cs, "_get_orchestrator", lambda: _Boom())

        monkeypatch.setattr(cs.settings, "chatops_concierge_enabled", True, raising=False)
        decision = cs.route(
            "run the pipeline on acme now",
            default_role="cto",
            default_target="acme",
        )

        assert decision.kind == "answer"
        assert not decision.destructive
        assert decision.action is None
