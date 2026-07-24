"""Headroom compression efficiency metrics store (Headroom Efficiency Panel
sprint).

`plugins/headroom.py`'s `before_step` hook compresses `prior_context`/
`extra_prompt` on every step but only ever logs a per-call
``logger.info("plugin.headroom.compressed", ...)`` line -- nothing is
aggregated or persisted, so there is no way to see cumulative savings across
runs. This module is that source of truth: one additive SQLite table
(``headroom_compressions``), written to by `plugins/headroom.py` after each
successful compression, and read by `plugins/headroom_panel.py`'s Mirador
panel.

**Additive-only, own table.** Mirrors `hivepilot.services.memory_service`'s
own migration/connection idioms exactly (``db.connect()`` / ``db.ph()`` /
``db.autoincrement_pk()``, same `state_service.py`-style idempotent
``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` inside
``init_db()``, called at the top of every public function below). No
existing table is ever touched -- this module does not import or modify
`state_service.py`/`memory_service.py`, it just shares the same underlying
``state.db`` file via `db.py`.

**Best-effort recording, never raises.** `record_compression` wraps its body
in a broad ``try/except Exception`` and logs-and-swallows any failure (bad
DB, bad input, whatever) -- mirrors `memory_service.record_search`'s "a hook
must never crash a run" discipline. Instrumenting a compression must never be
able to break the pipeline step that triggered it.

**Tenant scoping.** `efficiency_summary` takes ``tenant: str | None`` --
``None`` means unscoped (all tenants). `plugins/headroom_panel.py` hardcodes
``tenant="default"`` (a `PanelSpec["fetch"]` callable takes no arguments, so
it has no caller tenant to thread through -- the same documented limitation
`plugins/drift_panel.py`/`plugins/autopilot_panel.py` already carry).
`plugins/headroom.py`'s `before_step` hook has the same limitation
(`RunnerPayload` carries no `tenant` field -- see `plugins/mem0.py`'s own
`recall`/`store` hooks for the identical, already-investigated gap) so it
also always records under ``tenant="default"``.

**Zero-safe, never fabricated.** `efficiency_summary` returns all-zero values
when the table is empty (or has no rows for the given tenant) -- it never
invents a ratio/estimate from nothing.

**Token estimate.** ``est_tokens_saved`` is a best-effort ``chars / 4``
heuristic (a common, cheap approximation for English-ish text; no tokenizer
dependency is introduced for this) -- it is explicitly an estimate, not a
measured value.

**p95 method.** SQLite has no percentile aggregate, so, exactly like
`hivepilot.services.analytics_service`'s own `_percentile` (see that
module's docstring for the full rationale), the p95 ratio is computed in
Python from the fetched ratio list using the nearest-rank method -- always
an observed value from the sample, never a synthetic interpolation.
"""

from __future__ import annotations

import math
from typing import Any

from hivepilot.services import db
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Migration (mirrors state_service.init_db() / memory_service.init_db()'s
# idempotent CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS pattern).
# ---------------------------------------------------------------------------


def init_db() -> None:
    pk = db.autoincrement_pk()
    with db.connect() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS headroom_compressions (
                id {pk},
                tenant TEXT NOT NULL DEFAULT 'default',
                step TEXT,
                chars_before INTEGER NOT NULL,
                chars_after INTEGER NOT NULL,
                ratio REAL NOT NULL,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_headroom_compressions_tenant_ts "
            "ON headroom_compressions(tenant, ts)"
        )


# ---------------------------------------------------------------------------
# Recording -- best-effort, NEVER raise into the caller (see module docstring).
# ---------------------------------------------------------------------------


def record_compression(
    *,
    tenant: str = "default",
    step: str | None,
    chars_before: int,
    chars_after: int,
    ratio: float,
) -> None:
    """Record one successful headroom compression. Best-effort: NEVER raises."""
    try:
        init_db()
        with db.connect() as conn:
            conn.execute(
                db.ph(
                    "INSERT INTO headroom_compressions "
                    "(tenant, step, chars_before, chars_after, ratio) "
                    "VALUES (?, ?, ?, ?, ?)"
                ),
                (tenant, step, chars_before, chars_after, ratio),
            )
    except Exception as exc:  # noqa: BLE001 — instrumentation must never break the caller
        logger.warning("headroom_metrics.record_compression_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Percentile (nearest-rank method — mirrors analytics_service._percentile)
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    rank = math.ceil((pct / 100.0) * n)
    idx = max(0, min(n - 1, rank - 1))
    return sorted_values[idx]


# ---------------------------------------------------------------------------
# Aggregate reader -- read-only, zero-safe, tenant-scoped.
# ---------------------------------------------------------------------------


def efficiency_summary(*, tenant: str | None = "default") -> dict[str, Any]:
    """Return cumulative headroom compression efficiency for *tenant*
    (``None`` = unscoped, all tenants).

    Zero-safe: an empty table (or no rows for *tenant*) yields
    ``total_compressions=0``, ``chars_saved=0``, ``avg_ratio=0.0``,
    ``p95_ratio=0.0``, ``est_tokens_saved=0.0`` -- never fabricated.
    """
    init_db()
    where = " WHERE tenant = ?" if tenant is not None else ""
    params: tuple[Any, ...] = (tenant,) if tenant is not None else ()

    with db.connect() as conn:
        row = conn.execute(
            db.ph(
                "SELECT COUNT(*) AS cnt, "
                "SUM(chars_before - chars_after) AS saved, "
                "AVG(ratio) AS avg_ratio "
                f"FROM headroom_compressions{where}"
            ),
            params,
        ).fetchone()
        ratio_rows = conn.execute(
            db.ph(f"SELECT ratio FROM headroom_compressions{where} ORDER BY ratio"),
            params,
        ).fetchall()

    total_compressions = int(row["cnt"] or 0)
    chars_saved = int(row["saved"] or 0)
    avg_ratio = float(row["avg_ratio"]) if row["avg_ratio"] is not None else 0.0
    ratios = [float(r["ratio"]) for r in ratio_rows]
    p95_ratio = _percentile(ratios, 95)
    est_tokens_saved = chars_saved / 4.0

    return {
        "total_compressions": total_compressions,
        "chars_saved": chars_saved,
        "avg_ratio": round(avg_ratio, 4),
        "p95_ratio": round(p95_ratio, 4),
        "est_tokens_saved": est_tokens_saved,
    }
