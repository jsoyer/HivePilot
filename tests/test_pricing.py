"""Tests for hivepilot.services.pricing (Phase 24b.2b — price-map).

`estimate_cost` is a pure function (no I/O, no DB) — these tests exercise it
directly plus the config-override merge behaviour via `settings.llm_price_map`.
"""

from __future__ import annotations

from hivepilot.services import pricing


class TestEstimateCostDefaults:
    def test_known_model_and_tokens_returns_exact_cost(self) -> None:
        # claude-sonnet-4-6 default rate: input=3.0, output=15.0 USD/Mtok.
        # 1_000_000 input tokens -> 3.0 USD; 500_000 output tokens -> 7.5 USD.
        cost = pricing.estimate_cost("claude-sonnet-4-6", 1_000_000, 500_000)
        assert cost == 10.5

    def test_zero_tokens_returns_zero_not_none(self) -> None:
        """Zero is a real (present) token count, distinct from missing."""
        cost = pricing.estimate_cost("claude-sonnet-4-6", 0, 0)
        assert cost == 0.0

    def test_unknown_model_returns_none(self) -> None:
        assert pricing.estimate_cost("some-unlisted-model-xyz", 1000, 1000) is None

    def test_none_model_returns_none(self) -> None:
        assert pricing.estimate_cost(None, 1000, 1000) is None

    def test_missing_input_tokens_returns_none(self) -> None:
        assert pricing.estimate_cost("claude-sonnet-4-6", None, 500) is None

    def test_missing_output_tokens_returns_none(self) -> None:
        assert pricing.estimate_cost("claude-sonnet-4-6", 500, None) is None

    def test_missing_both_tokens_returns_none(self) -> None:
        assert pricing.estimate_cost("claude-sonnet-4-6", None, None) is None


class TestEstimateCostConfigOverride:
    def test_override_replaces_default_rate_for_known_model(self, monkeypatch) -> None:
        from hivepilot.config import settings

        monkeypatch.setattr(
            settings,
            "llm_price_map",
            {"claude-sonnet-4-6": {"input": 1.0, "output": 2.0}},
            raising=False,
        )
        cost = pricing.estimate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000)
        assert cost == 3.0  # 1.0 + 2.0, not the default 3.0 + 15.0

    def test_override_adds_new_model_not_in_defaults(self, monkeypatch) -> None:
        from hivepilot.config import settings

        monkeypatch.setattr(
            settings,
            "llm_price_map",
            {"my-custom-model": {"input": 4.0, "output": 8.0}},
            raising=False,
        )
        cost = pricing.estimate_cost("my-custom-model", 1_000_000, 1_000_000)
        assert cost == 12.0

    def test_override_merges_over_defaults_not_replaces(self, monkeypatch) -> None:
        """An override for one model must not wipe out other default models —
        this is a merge, not a full replacement of the price table."""
        from hivepilot.config import settings

        monkeypatch.setattr(
            settings,
            "llm_price_map",
            {"my-custom-model": {"input": 1.0, "output": 1.0}},
            raising=False,
        )
        # A default model (untouched by the override) must still resolve.
        cost = pricing.estimate_cost("claude-sonnet-4-6", 1_000_000, 500_000)
        assert cost == 10.5

    def test_none_override_uses_defaults_only(self, monkeypatch) -> None:
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "llm_price_map", None, raising=False)
        cost = pricing.estimate_cost("claude-sonnet-4-6", 1_000_000, 500_000)
        assert cost == 10.5


class TestDefaultPriceMap:
    def test_default_price_map_is_non_empty(self) -> None:
        assert len(pricing.DEFAULT_PRICE_MAP) > 0

    def test_default_price_map_entries_have_input_and_output_rates(self) -> None:
        for model, rates in pricing.DEFAULT_PRICE_MAP.items():
            assert "input" in rates, model
            assert "output" in rates, model
            assert isinstance(rates["input"], (int, float))
            assert isinstance(rates["output"], (int, float))
