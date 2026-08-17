"""Compute the budget from the target model's window, don't guess a constant.

#526 raised `max_prior_context_chars` from 8 000 to 120 000 and made every cut
legible. That fixed the symptom and left the cause: the number is still a
guess about a model whose real window the engine never consults, so it is
wrong by construction for any deployment that does not happen to match it.

The operator's framing, and it is the right one: optimisation is a balance,
and economising into a result that produces nothing is not an optimisation.
So the cut must follow from what actually fits, and when the engine cannot
know, it must say it is falling back rather than pretend the constant is a
measurement.

Two honest limits, both stated in the code rather than hidden:

- characters-per-token is an ESTIMATE (3, deliberately low — French prose and
  code both tokenise worse than English);
- the share of the window given to prior context is a JUDGEMENT (a quarter),
  because the rest must hold the system and role prompts, the task, the
  agent's own tool results — which dominate for anything that reads files —
  and the response.

Neither is a measurement, and neither is presented as one.
"""

from __future__ import annotations

import pytest

from hivepilot.services.context_budget import resolve_context_budget


class TestAKnownModelDerivesItsOwnBudget:
    def test_a_200k_window_yields_a_budget_far_above_the_old_constant(self):
        budget, basis = resolve_context_budget("claude-opus-5", fallback=120_000)

        assert basis == "derived"
        assert budget > 120_000

    def test_a_1m_window_yields_more_than_a_200k_one(self):
        small, _ = resolve_context_budget("claude-opus-5", fallback=120_000)
        large, _ = resolve_context_budget("claude-opus-5[1m]", fallback=120_000)

        assert large > small

    def test_the_whole_of_run_639_fits_in_the_smallest_known_window(self):
        """75 728 characters over eight stages. The measurement this exists
        for: nothing about that run should ever have been cut."""
        budget, _ = resolve_context_budget("claude-haiku-4-5", fallback=120_000)

        assert budget > 75_728

    @pytest.mark.parametrize(
        "model", ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"]
    )
    def test_the_family_is_recognised_by_prefix_not_exact_string(self, model):
        """Dated model ids ship constantly; matching exactly would silently
        demote every one of them to the fallback."""
        _, basis = resolve_context_budget(model, fallback=120_000)

        assert basis == "derived"


class TestAnUnknownModelSaysSoInsteadOfGuessing:
    @pytest.mark.parametrize("model", [None, "", "  ", "some-openrouter/model-x"])
    def test_it_falls_back_and_names_the_fallback(self, model):
        budget, basis = resolve_context_budget(model, fallback=120_000)

        assert budget == 120_000
        assert basis == "fallback"

    def test_the_fallback_is_the_caller_s_number_not_a_second_constant(self):
        """A module-private default here would be a THIRD arbitrary number,
        silently disagreeing with settings."""
        budget, _ = resolve_context_budget(None, fallback=7)

        assert budget == 7


class TestTheDerivationIsNotAbsurd:
    def test_a_derived_budget_never_undercuts_the_fallback(self):
        """A 'calculation' that shrinks the window below the hand-tuned
        constant would be a regression wearing the word 'computed'."""
        for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
            budget, _ = resolve_context_budget(model, fallback=120_000)
            assert budget >= 120_000

    def test_a_quarter_of_the_window_is_left_to_prior_context(self):
        """Pinned so a later change to the share is a deliberate edit with a
        failing test, not a quiet drift."""
        budget, _ = resolve_context_budget("claude-opus-5", fallback=1)

        assert budget == (200_000 // 4) * 3
