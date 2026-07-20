"""Tests for `plugins/drift_panel.py` (Mirador Panels sprint) -- the
plugin-contributed `drift_history` `panel` capability contribution over the
drift-scan history table (`state_service.record_drift_scan`/
`get_recent_drift_scans`).

Loaded by file path (mirrors `tests/test_drift_graph_source.py`), never
`import plugins.drift_panel` -- that would insert a `plugins` package into
`sys.modules` and leak across the suite (see that file's own docstring for
the full rationale, `tests/test_plugins.py`'s
`assert "plugins" not in sys.modules` isolation assumption).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from hivepilot.config import settings

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_PATH = _REPO_ROOT / "plugins" / "drift_panel.py"

_spec = importlib.util.spec_from_file_location("hivepilot_test_drift_panel_plugin", _PLUGIN_PATH)
assert _spec and _spec.loader
drift_panel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drift_panel)


def _seed_scan(
    *,
    project: str = "demo-project",
    tenant: str = "default",
    drifted: bool = True,
    to_add: int | None = 1,
    to_change: int | None = 2,
    to_destroy: int | None = 3,
    error: str | None = None,
) -> int:
    from hivepilot.services import state_service
    from hivepilot.services.drift_service import DriftResult, DriftSummary

    summary = None
    if to_add is not None or to_change is not None or to_destroy is not None:
        summary = DriftSummary(
            to_add=to_add or 0, to_change=to_change or 0, to_destroy=to_destroy or 0
        )
    result = DriftResult(
        project=project, runner="opentofu", drifted=drifted, summary=summary, error=error
    )
    return state_service.record_drift_scan(result, tenant=tenant)


# ---------------------------------------------------------------------------
# Opt-in gating
# ---------------------------------------------------------------------------


class TestOptInGating:
    def test_disabled_by_default_contributes_nothing(self) -> None:
        assert settings.drift_panel_enabled is False
        assert drift_panel.register() == {}

    def test_enabled_contributes_drift_history_panel(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "drift_panel_enabled", True, raising=False)

        hooks = drift_panel.register()
        assert [p["name"] for p in hooks["panels"]] == ["drift_history"]
        spec = hooks["panels"][0]
        assert spec["title"] == "Drift History"
        assert spec["min_role"] == "read"
        assert callable(spec["fetch"])


# ---------------------------------------------------------------------------
# _fetch -- sections, counts, empty-data
# ---------------------------------------------------------------------------


class TestFetch:
    def test_no_scans_yields_text_section(self) -> None:
        result = drift_panel._fetch()
        assert result["sections"] == [{"kind": "text", "content": "No drift scans recorded."}]

    def test_builds_stat_and_table_sections_with_counts(self) -> None:
        _seed_scan(to_add=4, to_change=5, to_destroy=6)
        result = drift_panel._fetch()
        sections = result["sections"]
        kinds = [s["kind"] for s in sections]
        assert "stat" in kinds
        assert "table" in kinds

        table = next(s for s in sections if s["kind"] == "table")
        assert table["columns"] == [
            "project",
            "checked_at",
            "status",
            "to_add",
            "to_change",
            "to_destroy",
            "runner",
        ]
        assert len(table["rows"]) == 1
        row = table["rows"][0]
        assert row[0] == "demo-project"
        assert row[3] == "4"
        assert row[4] == "5"
        assert row[5] == "6"
        assert row[6] == "opentofu"
        for cell in row:
            assert isinstance(cell, str)

    def test_coerces_none_counts_to_zero(self) -> None:
        _seed_scan(drifted=False, to_add=None, to_change=None, to_destroy=None)
        result = drift_panel._fetch()
        table = next(s for s in result["sections"] if s["kind"] == "table")
        row = table["rows"][0]
        assert row[3] == "0"
        assert row[4] == "0"
        assert row[5] == "0"

    # -----------------------------------------------------------------
    # No-secret-leak
    # -----------------------------------------------------------------

    def test_never_leaks_detail_field(self) -> None:
        _seed_scan(drifted=False, error="LEAK_MARKER_SENSITIVE")
        result = drift_panel._fetch()
        assert "LEAK_MARKER_SENSITIVE" not in str(result)

    # -----------------------------------------------------------------
    # Tenant scoping
    # -----------------------------------------------------------------

    def test_tenant_scoping_denies_cross_tenant_scans(self) -> None:
        _seed_scan(project="tenant-a-project", tenant="tenant-a")
        result = drift_panel._fetch()
        assert result["sections"] == [{"kind": "text", "content": "No drift scans recorded."}]

    def test_calls_state_service_with_tenant_default_not_none(self, monkeypatch) -> None:
        from hivepilot.services import state_service

        captured: dict = {}

        def _spy(*args, **kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr(state_service, "get_recent_drift_scans", _spy)
        drift_panel._fetch()
        assert captured.get("tenant") == "default"

    # -----------------------------------------------------------------
    # Never calls a write function
    # -----------------------------------------------------------------

    def test_never_calls_a_state_service_write_function(self, monkeypatch) -> None:
        from hivepilot.services import state_service

        def _boom(*args, **kwargs):
            raise AssertionError("drift_panel must never write to state_service")

        monkeypatch.setattr(state_service, "record_drift_scan", _boom)
        # No scans seeded -- must not raise, must not touch the writer.
        result = drift_panel._fetch()
        assert result["sections"] == [{"kind": "text", "content": "No drift scans recorded."}]
