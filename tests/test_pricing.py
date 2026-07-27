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

    def test_malformed_override_entry_is_skipped_not_raised(self, monkeypatch) -> None:
        """A malformed per-model override value (not a dict — e.g. a typo'd
        JSON string/int instead of {"input":.., "output":..}) must degrade
        gracefully: the bad entry is skipped (never merged in), the model
        resolves as unpriced (None), and no exception propagates. Locks in
        the `isinstance(rates, dict)` guard in `_effective_price_map`."""
        from hivepilot.config import settings

        monkeypatch.setattr(
            settings,
            "llm_price_map",
            {"model-x": "not-a-dict"},
            raising=False,
        )
        cost = pricing.estimate_cost("model-x", 1_000_000, 500_000)
        assert cost is None

    def test_malformed_override_entry_does_not_break_other_models(self, monkeypatch) -> None:
        """A malformed entry for one model must not prevent a well-formed
        override (or a default) for another model from resolving."""
        from hivepilot.config import settings

        monkeypatch.setattr(
            settings,
            "llm_price_map",
            {
                "model-x": "not-a-dict",
                "model-y": {"input": 2.0, "output": 4.0},
            },
            raising=False,
        )
        assert pricing.estimate_cost("model-x", 1_000_000, 500_000) is None
        assert pricing.estimate_cost("model-y", 1_000_000, 1_000_000) == 6.0
        # A default model, untouched by either override entry, still resolves.
        assert pricing.estimate_cost("claude-sonnet-4-6", 1_000_000, 500_000) == 10.5


class TestDefaultPriceMap:
    def test_default_price_map_is_non_empty(self) -> None:
        assert len(pricing.DEFAULT_PRICE_MAP) > 0

    def test_default_price_map_entries_have_input_and_output_rates(self) -> None:
        for model, rates in pricing.DEFAULT_PRICE_MAP.items():
            assert "input" in rates, model
            assert "output" in rates, model
            assert isinstance(rates["input"], (int, float))
            assert isinstance(rates["output"], (int, float))

    def test_default_price_map_covers_real_recorded_canonical_ids(self) -> None:
        """Real operator boxes report canonical ids like `claude-haiku-4-5`
        (from `modelUsage`'s `canonicalModel` field) -- not just the newest
        `-4-6` generation already in the map. Locks in the usage-capture-
        modelusage fix's price-map reconciliation."""
        for model in ("claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5"):
            assert model in pricing.DEFAULT_PRICE_MAP, model

    def test_default_price_map_covers_bare_cli_aliases(self) -> None:
        """`--model sonnet/opus/haiku` (the bare alias form) is a real,
        accepted CLI value (see `model_profiles.yaml`) and is exactly what
        historical `steps.model` rows persisted before this fix (the CLI
        never echoed a `model` field, so `_record_step_success` fell back
        to the alias). Pricing these keeps `backfill_unpriced_costs` useful
        for pre-fix rows."""
        for alias in ("sonnet", "opus", "haiku"):
            assert alias in pricing.DEFAULT_PRICE_MAP, alias


class TestEstimateCostCacheTokens:
    """Prompt-cache tokens (`cache_read_tokens`/`cache_creation_tokens`) are
    billed at DIFFERENT rates than base input/output tokens -- must be priced
    as distinct quantities, never folded into the base input rate."""

    def test_cache_tokens_default_to_zero_backward_compatible(self) -> None:
        """Existing 2-arg call sites (no cache kwargs) must be byte-identical
        to pre-fix behaviour."""
        cost = pricing.estimate_cost("claude-sonnet-4-6", 1_000_000, 500_000)
        assert cost == 10.5

    def test_cache_read_tokens_priced_at_their_own_rate(self, monkeypatch) -> None:
        from hivepilot.config import settings

        monkeypatch.setattr(
            settings,
            "llm_price_map",
            {"m": {"input": 1.0, "output": 1.0, "cache_read": 0.5, "cache_write": 2.0}},
            raising=False,
        )
        cost = pricing.estimate_cost("m", 0, 0, cache_read_tokens=1_000_000)
        assert cost == 0.5

    def test_cache_creation_tokens_priced_at_their_own_rate(self, monkeypatch) -> None:
        from hivepilot.config import settings

        monkeypatch.setattr(
            settings,
            "llm_price_map",
            {"m": {"input": 1.0, "output": 1.0, "cache_read": 0.5, "cache_write": 2.0}},
            raising=False,
        )
        cost = pricing.estimate_cost("m", 0, 0, cache_creation_tokens=1_000_000)
        assert cost == 2.0

    def test_nonzero_cache_tokens_without_a_cache_rate_is_unpriced(self, monkeypatch) -> None:
        """A model whose price-map entry has no `cache_read`/`cache_write`
        rate cannot honestly be priced once cache tokens are involved --
        must return None (unpriced) rather than silently ignoring the cache
        volume and under-reporting cost."""
        from hivepilot.config import settings

        monkeypatch.setattr(
            settings,
            "llm_price_map",
            {"m": {"input": 1.0, "output": 1.0}},
            raising=False,
        )
        assert pricing.estimate_cost("m", 100, 100, cache_read_tokens=1_000_000) is None
        assert pricing.estimate_cost("m", 100, 100, cache_creation_tokens=1_000_000) is None

    def test_zero_cache_tokens_never_requires_a_cache_rate(self, monkeypatch) -> None:
        """Zero cache tokens must not trigger the missing-rate guard --
        a model with no cache rates configured still prices fine when this
        particular call has no cache activity."""
        from hivepilot.config import settings

        monkeypatch.setattr(
            settings,
            "llm_price_map",
            {"m": {"input": 1.0, "output": 1.0}},
            raising=False,
        )
        cost = pricing.estimate_cost(
            "m", 1_000_000, 1_000_000, cache_read_tokens=0, cache_creation_tokens=0
        )
        assert cost == 2.0
