"""Price map + cost estimation (Phase 24b.2b, extended by the
usage-capture-modelusage fix for prompt-cache tokens + canonical-model
reconciliation).

`steps.cost_usd` is populated by the runner ONLY when it self-reports a cost
(Phase 24b.2a — `claude_capture_usage`; the CLI's own `costUSD`, summed across
`modelUsage` entries, is now the primary self-reported source -- see
`claude_runner._parse_usage_envelope`). Most runners/models never self-report,
so a purely self-reported cost total silently understates reality. This
module supplies a **fallback** estimate from a static price map, used only
when nothing self-reported is available.

Precedence (enforced by callers, not here): self-reported `cost_usd` >
`estimate_cost(...)` from this module > "unpriced" (no cost signal at all).

The default table below is **indicative and deliberately small** — a
starting point pulled from public provider pricing pages as of 2026-07-15.
Providers change pricing without notice and this project does not track
that automatically. Operators running this in anger should override rates
(or add missing models) via `HIVEPILOT_LLM_PRICE_MAP` (JSON), which is
merged OVER these defaults (per-model, not a wholesale replacement) — see
`hivepilot.config.Settings.llm_price_map`.

Cache rates (``cache_read`` / ``cache_write``): Anthropic bills a prompt-
cache READ far below the base input rate (indicative ~0.1x here) and a cache
WRITE somewhat above it (indicative ~1.25x here, the 5-minute-TTL rate) —
these multipliers are estimates, not a verified current price sheet, and are
just as overridable via `HIVEPILOT_LLM_PRICE_MAP` as `input`/`output`. A
model entry with no `cache_read`/`cache_write` key is treated as NOT priceable
for cache tokens (see `estimate_cost`) rather than silently charged $0 for
real, billed cache volume.

Bare CLI aliases (``sonnet``/``opus``/``haiku``): `--model <alias>` is a real,
accepted Claude CLI value (see `model_profiles.yaml`) that Anthropic resolves
server-side to whatever the current generation is. Historical `steps.model`
rows persisted exactly this alias (the CLI's JSON envelope never echoed a
top-level `model` field, so `_record_step_success` fell back to the
runner-definition's configured alias) — pricing the alias itself (at today's
resolved generation's rate) is what makes `backfill_unpriced_costs` useful
for that pre-fix history. Like every other entry here, it goes stale as
Anthropic rotates which generation an alias resolves to and must be
re-verified periodically.
"""

from __future__ import annotations

# USD per 1,000,000 (1 Mtok) tokens. Indicative only, dated 2026-07-15 —
# NOT a guarantee of current provider pricing. Override via
# HIVEPILOT_LLM_PRICE_MAP. Intentionally small: only a few common models,
# not an exhaustive/maintained catalogue.
DEFAULT_PRICE_MAP: dict[str, dict[str, float]] = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-haiku-4-6": {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    # Previous generation -- still what real operator boxes report via
    # `modelUsage`'s `canonicalModel` field until they upgrade (usage-
    # capture-modelusage fix: these were entirely absent before, so every
    # recorded `-4-5` id was unpriced regardless of token counts).
    "claude-opus-4-5": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-haiku-4-5": {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    # Bare CLI aliases (see module docstring) -- priced at the CURRENT
    # (`-4-6`) generation's rate, the alias's present-day resolution.
    "opus": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "haiku": {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
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
    cache_read_tokens: int | None = 0,
    cache_creation_tokens: int | None = 0,
) -> float | None:
    """Estimate USD cost for a step from token counts + the price map.

    Returns `None` when the model isn't in the (possibly overridden) price
    map, or when either base token count is missing (`None`). A token count
    of `0` is a present value (not missing) and yields a `0.0` contribution
    from that side, not `None`. Pure function — no I/O, no DB access.

    `cache_read_tokens`/`cache_creation_tokens` default to `0` (backward
    compatible with every pre-existing 2-positional-arg call site — a
    request with no cache activity is priced exactly as before). When either
    is genuinely non-zero, the model's `cache_read`/`cache_write` rate is
    REQUIRED: a model priced for base input/output but with no cache rate on
    record cannot be honestly priced once cache tokens are involved, so this
    returns `None` (unpriced) rather than silently dropping real, billed
    cache volume from the total — undercounting it would be exactly the
    "silently understated" failure mode this module exists to avoid.
    """
    if not model or input_tokens is None or output_tokens is None:
        return None
    rates = _effective_price_map().get(model)
    if rates is None:
        return None
    in_rate = rates.get("input", 0.0)
    out_rate = rates.get("output", 0.0)
    cost = (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate

    if cache_read_tokens:
        cache_read_rate = rates.get("cache_read")
        if cache_read_rate is None:
            return None
        cost += (cache_read_tokens / 1_000_000) * cache_read_rate

    if cache_creation_tokens:
        cache_write_rate = rates.get("cache_write")
        if cache_write_rate is None:
            return None
        cost += (cache_creation_tokens / 1_000_000) * cache_write_rate

    return cost


__all__: list[str] = ["DEFAULT_PRICE_MAP", "estimate_cost"]
