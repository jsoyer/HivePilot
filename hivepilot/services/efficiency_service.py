"""Efficiency data sources for the Pollen `/v1/efficiency` endpoint
(Pollen data endpoints sprint).

Composes two independent, best-effort "token/context savings" signals:

- **headroom**: delegates to
  `hivepilot.services.headroom_metrics.efficiency_summary` -- an already
  real, tenant-scoped SQLite aggregate written by `plugins/headroom.py`'s
  `before_step` hook (see that module's own docstring for the full
  recording/aggregation contract). This IS genuinely queryable
  programmatic data (not an estimate, not an external source that might be
  absent), so `headroom_summary` always returns a real dict -- all-zero
  when nothing has been recorded yet, mirroring every other zero-safe
  aggregate in this codebase (`memory_service.reality_summary`,
  `analytics_service.cost_summary`, ...) -- NEVER `None` and NEVER a
  fabricated number like the frontend's placeholder "66M tokens saved".

- **rtk**: the `rtk` CLI (a companion dev-tool token-savings proxy --
  see `plugins/rtk.py`) tracks GLOBAL, machine-level command savings, via
  `rtk gain -a -f json` -- this is NOT hivepilot run/tenant data (it has
  no concept of a HivePilot tenant at all), so it is intentionally never
  tenant-scoped, mirroring `GET /v1/plugins/health`'s own "process-global,
  not tenant-partitioned" precedent (see `api_service.py`
  `plugins_health_endpoint`'s docstring). Best-effort: the `rtk` binary
  being absent, a non-zero exit, a timeout, or unparseable/unexpected JSON
  all degrade to `rtk: None` -- NEVER a fabricated number, NEVER raises
  into the caller.

  Investigated by hand (`rtk --version` -> 0.43.0 present on this
  machine): `rtk gain -a -f json` returns
  ``{"summary": {...}, "daily": [...], "weekly": [...], "monthly":
  [...]}``. The per-command "By Command" breakdown table is ONLY produced
  by the plain-text/`-H` (history) output -- there is no equivalent JSON
  field -- so `top_commands` is always `None` here, never scraped/
  fabricated from the text table.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Shell-out safety: fixed argv list (never shell=True) + a bounded timeout so
# a hung/misbehaving `rtk` binary can never block a request indefinitely.
_RTK_TIMEOUT_SECONDS = 10.0


def headroom_summary(tenant: str | None = "default") -> dict[str, Any]:
    """Real, always-present headroom compression efficiency for *tenant*
    (``None`` = unscoped, all tenants) -- see module docstring. Never
    ``None``; zero-safe when nothing has been recorded."""
    from hivepilot.services import headroom_metrics

    return headroom_metrics.efficiency_summary(tenant=tenant)


def _parse_rtk_gain_json(raw: str) -> dict[str, Any] | None:
    """Parse `rtk gain -a -f json` stdout into this module's `rtk` shape,
    or ``None`` if the JSON is missing/malformed/not the expected shape
    (never raises -- see `rtk_summary`'s docstring for the full
    best-effort contract)."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    summary = parsed.get("summary")
    if not isinstance(summary, dict):
        return None
    try:
        gain_pct = round(float(summary.get("avg_savings_pct", 0.0)), 2)
        tokens_saved = int(summary.get("total_saved", 0))
        total_commands = int(summary.get("total_commands", 0))
    except (TypeError, ValueError):
        return None

    saved_series: list[dict[str, Any]] = []
    daily = parsed.get("daily")
    if isinstance(daily, list):
        for entry in daily:
            if not isinstance(entry, dict):
                continue
            date = entry.get("date")
            saved = entry.get("saved_tokens")
            if isinstance(date, str) and isinstance(saved, (int, float)):
                saved_series.append({"date": date, "saved_tokens": int(saved)})

    return {
        "gain_pct": gain_pct,
        "tokens_saved": tokens_saved,
        "total_commands": total_commands,
        "saved_series": saved_series,
        # rtk's `-f json` output has no per-command breakdown -- only the
        # text/`-H` "By Command" table does. Never fabricated from that.
        "top_commands": None,
    }


def rtk_summary(*, days: int | None = 30) -> dict[str, Any] | None:
    """Best-effort global rtk token-savings summary via `rtk gain -a -f
    json`. Returns ``None`` (never raises, never fabricates a number) when
    the `rtk` binary isn't on PATH, the process errors/times out, or its
    stdout isn't the expected JSON shape.

    `days` truncates ``saved_series`` to the most recent N daily entries
    (mirrors the `days` window every other analytics endpoint in this
    codebase takes); ``None`` returns the full available history.
    """
    rtk_path = shutil.which("rtk")
    if not rtk_path:
        return None
    try:
        result = subprocess.run(  # noqa: S603 -- fixed argv list, never shell=True
            ["rtk", "gain", "-a", "-f", "json"],
            capture_output=True,
            text=True,
            timeout=_RTK_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("efficiency_service.rtk_shellout_failed", error=str(exc))
        return None

    if result.returncode != 0:
        logger.warning("efficiency_service.rtk_nonzero_exit", returncode=result.returncode)
        return None

    parsed = _parse_rtk_gain_json(result.stdout)
    if parsed is None:
        return None
    if days is not None and parsed["saved_series"]:
        parsed["saved_series"] = parsed["saved_series"][-days:]
    return parsed


def efficiency_summary(*, tenant: str | None = "default", days: int | None = 30) -> dict[str, Any]:
    """``{"headroom": <real dict, never null>, "rtk": <real dict or null>}``
    -- see module docstring for the investigation behind each source."""
    return {
        "headroom": headroom_summary(tenant=tenant),
        "rtk": rtk_summary(days=days),
    }
