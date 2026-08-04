"""Where the money actually goes, per session.

A cost table that counts 517 000 cache-read tokens the same way as 3 000
fresh input tokens tells the operator nothing true. Those are the real
numbers from one review dispatch, and cache reads are priced at a fraction
of input — so the volume that looks alarming is the cheap part, and the 20 000
output tokens that look modest are where the spend is.

That distinction is not cosmetic: it is the difference between "bound what
the reviewers read" (wrong, the reading is cached and cheap) and "bound what
they write" (the actual lever). Guessing it wrong sends a whole optimisation
effort at the wrong parameter, which is exactly what happened before this was
measured.

`estimate_cost` already weights cache correctly and refuses to price a model
with tokens it has no rate for. What was missing is the split.
"""

from __future__ import annotations

import pytest

from hivepilot.services import pricing


@pytest.fixture
def priced_model(monkeypatch: pytest.MonkeyPatch) -> str:
    """A model with all four rates, at deliberately distinct magnitudes so a
    component mix-up cannot pass unnoticed."""
    monkeypatch.setattr(
        pricing,
        "_effective_price_map",
        lambda: {
            "test-model": {
                "input": 10.0,
                "output": 100.0,
                "cache_read": 1.0,
                "cache_write": 12.5,
            }
        },
    )
    return "test-model"


class TestCostComponents:
    def test_it_splits_the_bill_by_component(self, priced_model: str) -> None:
        parts = pricing.cost_components(
            priced_model,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_creation_tokens=1_000_000,
        )

        assert parts == {
            "input": 10.0,
            "output": 100.0,
            "cache_read": 1.0,
            "cache_write": 12.5,
        }

    def test_the_components_sum_to_the_total(self, priced_model: str) -> None:
        """The split must reconcile with the number already shown elsewhere.

        A breakdown that does not add up to `estimate_cost` would put two
        contradictory figures on the same dashboard.
        """
        args = dict(
            input_tokens=3_040,
            output_tokens=20_455,
            cache_read_tokens=516_982,
            cache_creation_tokens=34_873,
        )

        parts = pricing.cost_components(priced_model, **args)
        total = pricing.estimate_cost(priced_model, **args)

        assert parts is not None and total is not None
        assert sum(parts.values()) == pytest.approx(total, rel=1e-9)

    def test_real_proportions_put_the_spend_where_it_belongs(self, priced_model: str) -> None:
        """The measured shape: cache reads dominate volume, output dominates cost."""
        parts = pricing.cost_components(
            priced_model,
            input_tokens=3_040,
            output_tokens=20_455,
            cache_read_tokens=516_982,
            cache_creation_tokens=34_873,
        )

        assert parts is not None
        assert parts["output"] > parts["cache_read"], (
            "20k output tokens cost more than 517k cache reads — the whole "
            "point of weighting them differently"
        )

    def test_an_unpriced_model_returns_none_not_zeros(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zeros would read as "this was free", which is a claim, not a gap."""
        monkeypatch.setattr(pricing, "_effective_price_map", dict)

        assert pricing.cost_components("unknown", input_tokens=1, output_tokens=1) is None

    def test_cache_tokens_without_a_cache_rate_refuse_to_price(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same contract as `estimate_cost`, and for the same reason.

        Pricing real billed cache volume at zero would undercount the bill
        while looking like a complete answer.
        """
        monkeypatch.setattr(
            pricing, "_effective_price_map", lambda: {"m": {"input": 1.0, "output": 1.0}}
        )

        assert (
            pricing.cost_components("m", input_tokens=1, output_tokens=1, cache_read_tokens=99)
            is None
        )

    def test_no_cache_activity_still_prices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A model with no cache rates is fine as long as no cache was used."""
        monkeypatch.setattr(
            pricing, "_effective_price_map", lambda: {"m": {"input": 10.0, "output": 100.0}}
        )

        parts = pricing.cost_components("m", input_tokens=1_000_000, output_tokens=0)

        assert parts is not None
        assert parts["input"] == 10.0
        assert parts["cache_read"] == 0.0
