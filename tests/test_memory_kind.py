"""Memory backends are peers, and the engine refuses combinations that corrupt.

Today mem0 and obsidian are plain `hook` plugins and `MemoryBackendsView`
hardcodes "the two memory backends". There is no memory *kind*, so a third
backend — honcho, Hindsight, or one an operator writes — has no seat at the
table. That contradicts the engine's own rule: generic for ANY organisation,
noxys is one consumer. Pinning mem0 as the only option is the coupling that
rule exists to prevent.

Two things this must get right, and they are separate:

**Recall semantics cannot be inferred.** obsidian appends
(`f"{existing}\\n\\n{block}"`, verified in its source) and so composes with
anything. A backend that REPLACES the field composes with nothing. You cannot
read that off a third party's code, so it is declared, and the engine composes
on the declaration.

**A refusal, not a warning.** The failure being prevented is silent corpus
contamination — one backend storing another's recalls, which stays plausible
while it compounds. A warning someone learns to click through is worth nothing
against that. Either the combination is safe and we say nothing, or it is not
and `enable` refuses, naming the other backend and the reason.
"""

from __future__ import annotations

import pytest

from hivepilot.services.memory_kind import (
    MemoryBackend,
    RecallSemantics,
    describe_conflict,
    resolve_composition,
)

ADDITIVE = MemoryBackend(name="obsidian", semantics=RecallSemantics.ADDITIVE)
EXCLUSIVE = MemoryBackend(name="mem0", semantics=RecallSemantics.EXCLUSIVE)
OTHER_EXCLUSIVE = MemoryBackend(name="honcho", semantics=RecallSemantics.EXCLUSIVE)
OTHER_ADDITIVE = MemoryBackend(name="notes", semantics=RecallSemantics.ADDITIVE)


class TestWhatComposes:
    def test_nothing_enabled_is_allowed(self):
        """The engine ships no memory backend enabled. An empty set is the
        default, not an error."""
        assert resolve_composition([]).allowed is True

    def test_one_backend_of_either_kind_is_allowed(self):
        assert resolve_composition([ADDITIVE]).allowed is True
        assert resolve_composition([EXCLUSIVE]).allowed is True

    def test_additive_backends_compose_freely(self):
        """obsidian's contract, generalised: appending cannot destroy what
        another backend wrote."""
        assert resolve_composition([ADDITIVE, OTHER_ADDITIVE]).allowed is True

    def test_one_exclusive_with_additives_is_allowed(self):
        assert resolve_composition([EXCLUSIVE, ADDITIVE]).allowed is True
        assert resolve_composition([ADDITIVE, EXCLUSIVE, OTHER_ADDITIVE]).allowed is True


class TestWhatIsRefused:
    def test_two_exclusive_backends_are_refused(self):
        decision = resolve_composition([EXCLUSIVE, OTHER_EXCLUSIVE])

        assert decision.allowed is False

    def test_the_refusal_names_both_backends(self):
        """'Invalid combination' sends an operator nowhere. The message has to
        say which two, so the choice is theirs to make."""
        decision = resolve_composition([EXCLUSIVE, OTHER_EXCLUSIVE])

        assert "mem0" in decision.reason
        assert "honcho" in decision.reason

    def test_the_refusal_says_why_not_just_that(self):
        decision = resolve_composition([EXCLUSIVE, OTHER_EXCLUSIVE])

        assert "extra_prompt" in decision.reason

    def test_three_exclusives_still_name_a_concrete_pair(self):
        """A list of every offender is unreadable; one concrete pair is
        actionable, and resolving it re-runs the check."""
        third = MemoryBackend(name="hindsight", semantics=RecallSemantics.EXCLUSIVE)

        decision = resolve_composition([EXCLUSIVE, OTHER_EXCLUSIVE, third])

        assert decision.allowed is False
        assert decision.reason.count(" and ") >= 1


class TestTheDecisionIsOrderIndependent:
    """A composition that depends on declaration order is a trap: the same
    config would be accepted or refused depending on how it was written."""

    @pytest.mark.parametrize(
        "backends",
        [
            [EXCLUSIVE, OTHER_EXCLUSIVE],
            [OTHER_EXCLUSIVE, EXCLUSIVE],
        ],
    )
    def test_two_exclusives_are_refused_either_way(self, backends):
        assert resolve_composition(backends).allowed is False

    @pytest.mark.parametrize(
        "backends",
        [
            [ADDITIVE, EXCLUSIVE],
            [EXCLUSIVE, ADDITIVE],
        ],
    )
    def test_a_mixed_pair_is_allowed_either_way(self, backends):
        assert resolve_composition(backends).allowed is True


class TestSemanticsMustBeDeclared:
    def test_an_undeclared_semantics_is_rejected_at_construction(self):
        """Defaulting to 'additive' would be the dangerous guess: a replacing
        backend silently wiping another's recall is exactly the contamination
        this module exists to stop."""
        with pytest.raises((TypeError, ValueError)):
            MemoryBackend(name="mystery")  # type: ignore[call-arg]

    def test_a_bogus_semantics_string_is_rejected(self):
        with pytest.raises(ValueError):
            MemoryBackend(name="mystery", semantics="probably-fine")  # type: ignore[arg-type]


class TestTheOperatorFacingExplanation:
    def test_a_safe_pair_explains_nothing(self):
        """Silence when nothing is wrong. A notice on every enable teaches
        people to ignore notices."""
        assert describe_conflict(ADDITIVE, OTHER_ADDITIVE) is None

    def test_an_unsafe_pair_explains_the_mechanism(self):
        message = describe_conflict(EXCLUSIVE, OTHER_EXCLUSIVE)

        assert message is not None
        assert "mem0" in message and "honcho" in message
