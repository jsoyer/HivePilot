"""HP-104 Pollen skill-cycle panel — top/bottom measured skills."""

from __future__ import annotations

import importlib.util

from conftest import BUNDLED_PLUGINS

from hivepilot.config import settings
from hivepilot.skill_events import record_skill_event, revision_for_skill

_PLUGIN_PATH = BUNDLED_PLUGINS / "skill_events_panel.py"

_spec = importlib.util.spec_from_file_location(
    "hivepilot_test_skill_events_panel_plugin", _PLUGIN_PATH
)
assert _spec and _spec.loader
skill_events_panel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skill_events_panel)


def _seed(name: str, event_type: str, run_id: int, *, tenant: str = "default") -> None:
    logical, rev = revision_for_skill(name, {"SKILL.md": name})
    record_skill_event(
        event_type=event_type,
        revision_id=rev,
        logical_id=logical,
        skill_name=name,
        run_id=run_id,
        step="impl",
        tenant=tenant,
    )


class TestOptInGating:
    def test_disabled_by_default_contributes_nothing(self) -> None:
        assert settings.skill_events_panel_enabled is False
        assert skill_events_panel.register() == {}

    def test_enabled_contributes_skill_cycle_panel(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "skill_events_panel_enabled", True, raising=False)
        hooks = skill_events_panel.register()
        assert [p["name"] for p in hooks["panels"]] == ["skill-cycle"]
        spec = hooks["panels"][0]
        assert spec["title"] == "Skill cycle"
        assert spec["min_role"] == "read"
        assert callable(spec["fetch"])


class TestFetch:
    def test_empty_store_is_text_not_zero_stats(self) -> None:
        result = skill_events_panel._fetch()
        assert result["sections"][0]["kind"] == "text"
        assert "Unmeasured" in result["sections"][0]["content"]
        assert "0%" not in str(result)

    def test_top_and_bottom_tables(self) -> None:
        _seed("alpha", "selected", 1)
        _seed("alpha", "completed", 1)
        _seed("beta", "selected", 1)
        result = skill_events_panel._fetch()
        kinds = [section["kind"] for section in result["sections"]]
        assert kinds.count("table") == 2
        tables = [section for section in result["sections"] if section["kind"] == "table"]
        top_names = [row[0] for row in tables[0]["rows"]]
        bottom_names = [row[0] for row in tables[1]["rows"]]
        assert "alpha" in top_names
        assert "beta" in bottom_names
        alpha_rate = next(row[5] for row in tables[0]["rows"] if row[0] == "alpha")
        beta_rate = next(row[5] for row in tables[1]["rows"] if row[0] == "beta")
        assert alpha_rate == "100%"
        assert beta_rate == "0%"
        for cell in tables[0]["rows"][0]:
            assert isinstance(cell, str)

    def test_unmeasured_skill_absent_from_tables(self) -> None:
        _seed("only-excluded", "excluded", 1)
        result = skill_events_panel._fetch()
        blob = str(result)
        assert "only-excluded" not in blob
        stat = next(section for section in result["sections"] if section["kind"] == "stat")
        assert stat["label"] == "unmeasured skills"
        assert stat["value"] == "1"

    def test_tenant_scoping_denies_cross_tenant(self) -> None:
        _seed("secret", "selected", 1, tenant="other")
        _seed("secret", "completed", 1, tenant="other")
        result = skill_events_panel._fetch()
        assert result["sections"][0]["kind"] == "text"

    def test_never_calls_record(self, monkeypatch) -> None:
        from hivepilot import skill_events

        def _boom(*args, **kwargs):
            raise AssertionError("panel must not record skill events")

        monkeypatch.setattr(skill_events, "record_skill_event", _boom)
        result = skill_events_panel._fetch()
        assert result["sections"][0]["kind"] == "text"
