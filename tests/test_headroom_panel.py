"""Tests for the Headroom efficiency metrics store + Mirador panel
(Headroom Efficiency Panel sprint).

Covers:
- `hivepilot.services.headroom_metrics`: idempotent `init_db()`,
  `record_compression` persistence (best-effort, never raises),
  `efficiency_summary` aggregate math -- zero-safe on an empty table,
  tenant-scoped.
- `plugins/headroom_panel.py`: opt-in gating (`settings.headroom_panel_enabled`,
  required by `tests/test_gating_conformance.py::TestAllPluginStemsHaveEnabledFlag`),
  `fetch()` returns a valid `PanelData` built from `efficiency_summary()`.

Loaded by file path (mirrors `tests/test_drift_panel.py` /
`tests/test_autopilot_panel.py`), never `import plugins.headroom_panel` --
that would insert a `plugins` package into `sys.modules` and leak across the
suite (see `tests/test_plugins.py`'s `assert "plugins" not in sys.modules`
isolation assumption).
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

from hivepilot.config import settings
from hivepilot.services import headroom_metrics

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_PATH = _REPO_ROOT / "plugins" / "headroom_panel.py"

_spec = importlib.util.spec_from_file_location(
    "hivepilot_test_headroom_panel_plugin", _PLUGIN_PATH
)
assert _spec and _spec.loader
headroom_panel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(headroom_panel)


# ---------------------------------------------------------------------------
# init_db idempotency
# ---------------------------------------------------------------------------


class TestInitDbIdempotent:
    def test_init_db_can_be_called_multiple_times_without_error(self) -> None:
        headroom_metrics.init_db()
        headroom_metrics.init_db()
        headroom_metrics.init_db()


# ---------------------------------------------------------------------------
# record_compression + efficiency_summary -- zero-safe, math, tenant scoping
# ---------------------------------------------------------------------------


class TestEfficiencySummaryZeroSafe:
    def test_zero_rows_yields_all_zero_summary(self) -> None:
        summary = headroom_metrics.efficiency_summary(tenant="default")
        assert summary["total_compressions"] == 0
        assert summary["chars_saved"] == 0
        assert summary["avg_ratio"] == 0.0
        assert summary["p95_ratio"] == 0.0
        assert summary["est_tokens_saved"] == 0.0


class TestRecordCompressionAndSummaryMath:
    def test_single_row_persists_and_summary_reflects_it(self) -> None:
        headroom_metrics.record_compression(
            tenant="default", step="s1", chars_before=1000, chars_after=250, ratio=0.25
        )
        summary = headroom_metrics.efficiency_summary(tenant="default")
        assert summary["total_compressions"] == 1
        assert summary["chars_saved"] == 750
        assert summary["avg_ratio"] == 0.25
        assert summary["p95_ratio"] == 0.25
        assert summary["est_tokens_saved"] == 750 / 4.0

    def test_multiple_rows_aggregate_chars_saved_and_avg_ratio(self) -> None:
        headroom_metrics.record_compression(
            tenant="default", step="a", chars_before=1000, chars_after=500, ratio=0.5
        )
        headroom_metrics.record_compression(
            tenant="default", step="b", chars_before=2000, chars_after=1000, ratio=0.5
        )
        summary = headroom_metrics.efficiency_summary(tenant="default")
        assert summary["total_compressions"] == 2
        assert summary["chars_saved"] == 1500
        assert summary["avg_ratio"] == 0.5
        assert summary["est_tokens_saved"] == 1500 / 4.0

    def test_p95_uses_nearest_rank_method_over_ratios(self) -> None:
        ratios = [0.1, 0.2, 0.3, 0.4, 0.9]
        for i, ratio in enumerate(ratios):
            headroom_metrics.record_compression(
                tenant="default",
                step=f"s{i}",
                chars_before=100,
                chars_after=int(100 * ratio),
                ratio=ratio,
            )
        summary = headroom_metrics.efficiency_summary(tenant="default")
        ordered = sorted(ratios)
        n = len(ordered)
        rank = math.ceil(0.95 * n)
        idx = max(0, min(n - 1, rank - 1))
        expected_p95 = ordered[idx]
        assert summary["p95_ratio"] == expected_p95

    def test_tenant_scoping_excludes_other_tenants(self) -> None:
        headroom_metrics.record_compression(
            tenant="tenant-a", step="x", chars_before=100, chars_after=10, ratio=0.1
        )
        summary = headroom_metrics.efficiency_summary(tenant="default")
        assert summary["total_compressions"] == 0
        assert summary["chars_saved"] == 0

    def test_record_compression_is_best_effort_never_raises(self, monkeypatch) -> None:
        def _boom() -> None:
            raise RuntimeError("db exploded")

        monkeypatch.setattr(headroom_metrics, "init_db", _boom)
        # Must not raise even when the underlying migration/connection blows up.
        headroom_metrics.record_compression(
            tenant="default", step="s", chars_before=10, chars_after=5, ratio=0.5
        )


# ---------------------------------------------------------------------------
# Panel opt-in gating
# ---------------------------------------------------------------------------


class TestOptInGating:
    def test_disabled_by_default_contributes_nothing(self) -> None:
        assert settings.headroom_panel_enabled is False
        assert headroom_panel.register() == {}

    def test_enabled_contributes_headroom_efficiency_panel(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "headroom_panel_enabled", True, raising=False)

        hooks = headroom_panel.register()
        assert [p["name"] for p in hooks["panels"]] == ["headroom-efficiency"]
        spec = hooks["panels"][0]
        assert spec["title"] == "Headroom efficiency"
        assert spec["min_role"] == "read"
        assert callable(spec["fetch"])


# ---------------------------------------------------------------------------
# _fetch -- valid PanelData, zero-safe, stat sections
# ---------------------------------------------------------------------------


class TestFetch:
    def test_no_data_yields_text_section(self) -> None:
        result = headroom_panel._fetch()
        assert result["sections"] == [
            {"kind": "text", "content": "No headroom compressions recorded yet."}
        ]

    def test_with_data_yields_stat_sections_with_string_values(self) -> None:
        headroom_metrics.record_compression(
            tenant="default", step="s1", chars_before=1000, chars_after=250, ratio=0.25
        )
        result = headroom_panel._fetch()
        sections = result["sections"]
        assert all(s["kind"] == "stat" for s in sections)
        assert len(sections) == 5
        for section in sections:
            assert isinstance(section["value"], str)
            assert isinstance(section["label"], str)
