"""Price map + cost estimation (Phase 24b.2b).

`steps.cost_usd` is populated by the runner ONLY when it self-reports a
`total_cost_usd` (Phase 24b.2a — `claude_capture_usage`). Most runners/models
never do this, so a purely self-reported cost total silently understates
reality. This module supplies a **fallback** estimate from a static price
map, used only when nothing self-reported is available.

Precedence (enforced by callers, not here): self-reported `cost_usd` >
`estimate_cost(...)` from this module > "unpriced" (no cost signal at all).

The default table below is **indicative and deliberately small** — a
starting point pulled from public provider pricing pages as of 2026-07-15.
Providers change pricing without notice and this project does not track
that automatically. Operators running this in anger should override rates
(or add missing models) via `HIVEPILOT_LLM_PRICE_MAP` (JSON), which is
merged OVER these defaults (per-model, not a wholesale replacement) — see
`hivepilot.config.Settings.llm_price_map`.
"""

from __future__ import annotations

# USD per 1,000,000 (1 Mtok) tokens. Indicative only, dated 2026-07-15 —
# NOT a guarantee of current provider pricing. Override via
# HIVEPILOT_LLM_PRICE_MAP. Intentionally small: only a few common models,
# not an exhaustive/maintained catalogue.
DEFAULT_PRICE_MAP: dict[str, dict[str, float]] = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-6": {"input": 0.8, "output": 4.0},
    "gpt-5.5": {"input": 5.0, "output": 15.0},
    "gpt-5.5-mini": {"input": 0.5, "output": 2.0},
}


def _effective_price_map() -> dict[str, dict[str, float]]:
    """`DEFAULT_PRICE_MAP` with `settings.llm_price_map` merged over it.

    Merge, not replace: an override for one model doesn't drop the other
    default models, and a partial per-model override (e.g. only `input`)
    is merged onto that model's existing rates rather than discarding the
    other rate. Imported lazily to avoid a module-level import cycle with
    `hivepilot.config`.
    """
    from hivepilot.config import settings

    merged: dict[str, dict[str, float]] = {
        model: dict(rates) for model, rates in DEFAULT_PRICE_MAP.items()
    }
    override = settings.llm_price_map
    if override:
        for model, rates in override.items():
            if isinstance(rates, dict):
                merged[model] = {**merged.get(model, {}), **rates}
    return merged


def estimate_cost(
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    """Estimate USD cost for a step from token counts + the price map.

    Returns `None` when the model isn't in the (possibly overridden) price
    map, or when either token count is missing (`None`). A token count of
    `0` is a present value (not missing) and yields a `0.0` contribution
    from that side, not `None`. Pure function — no I/O, no DB access.
    """
    if not model or input_tokens is None or output_tokens is None:
        return None
    rates = _effective_price_map().get(model)
    if rates is None:
        return None
    in_rate = rates.get("input", 0.0)
    out_rate = rates.get("output", 0.0)
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


__all__: list[str] = ["DEFAULT_PRICE_MAP", "estimate_cost"]
