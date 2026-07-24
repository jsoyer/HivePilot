"""TS<->Python response-shape contract for the Mirador web UI (Sprint 4).

If you change these response keys, update the TS types in
`web/src/lib/mirador-api.ts`.

`web/src/lib/mirador-api.ts` hand-transcribes every Mirador data-source
response shape into TypeScript interfaces (see that file's own module
docstring: "Field names/shapes are transcribed directly from
`hivepilot/services/analytics_service.py` and `hivepilot/services/
api_service.py`"). Nothing in the frontend build guards against a future
backend field rename silently blanking a Mirador panel — Vitest tests mock
the shapes, they don't call the real API.

This module is that guard, from the Python side: it seeds real data through
`state_service`, calls each endpoint the web UI consumes via FastAPI's
`TestClient`, and asserts the **exact top-level key set** (plus key nested
shapes) of every response. A backend rename (e.g. `cost_usd` ->
`cost_usd_total`) makes one of these assertions fail loudly in Python CI,
instead of silently rendering `undefined`/blank fields in the browser.

Endpoints covered (every one `web/src/lib/mirador-api.ts` calls):
    GET /v1/analytics/summary
    GET /v1/analytics/trends
    GET /v1/analytics/durations
    GET /v1/analytics/steps/failures
    GET /v1/analytics/approvals/latency
    GET /v1/analytics/providers
    GET /v1/analytics/cost
    GET /v1/plugins/health
    GET /v1/memories
    GET /v1/panels
    GET /v1/panels/{name}

The last two guard the `panel` plugin type (Mirador Sprint 4): their exact
response shapes are hand-transcribed as `PanelSummary`/`PanelsResponse` and
`PanelData`/`PanelStatSection`/`PanelTableSection`/`PanelTextSection` in
`web/src/lib/mirador-api.ts` — see that file's own comment block just above
those interfaces.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services.token_service import add_token

# ---------------------------------------------------------------------------
# Shared fixtures (mirrors tests/test_api_service.py's analytics fixtures —
# duplicated locally rather than moved to conftest.py, since this file must
# stay a standalone, easy-to-scan contract manifest).
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _auth(raw_token: str) -> dict:
    return {"Authorization": f"Bearer {raw_token}"}


@pytest.fixture()
def read_token(tmp_tokens_file):
    raw, _ = add_token("read")
    return raw


@pytest.fixture()
def admin_token(tmp_tokens_file):
    raw, _ = add_token("admin")
    return raw


@pytest.fixture()
def seeded_run():
    """One finished run with a step (provider+model+cost+tokens) and an
    actioned approval — enough real data for every analytics endpoint below
    to return a non-empty, shape-checkable payload instead of the
    all-zeros/empty-list degenerate case."""
    from hivepilot.services import state_service

    run_id = state_service.record_run_start("mirador-p", "mirador-t", status="running")
    state_service.record_step(
        run_id,
        "deploy",
        "success",
        provider="claude",
        model="claude-sonnet-4-6",
        cost_usd=1.25,
        input_tokens=100,
        output_tokens=50,
    )
    state_service.record_step(run_id, "deploy", "failed")
    state_service.record_approval_request(run_id, "mirador-p", "mirador-t", {})
    state_service.update_approval(run_id, "approved")
    state_service.complete_run(run_id, "success")
    return run_id


# ---------------------------------------------------------------------------
# GET /v1/analytics/summary
# ---------------------------------------------------------------------------


class TestAnalyticsSummaryContract:
    def test_top_level_keys(self, api_client, read_token, seeded_run):
        resp = api_client.get("/v1/analytics/summary", headers=_auth(read_token))
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {
            "total",
            "outcomes",
            "outcome_rates",
            "success_rate",
            "by_project",
            "by_task",
            "by_raw_status",
        }
        assert set(data["outcomes"].keys()) == {"succeeded", "failed", "skipped", "other"}
        assert set(data["outcome_rates"].keys()) == {"succeeded", "failed", "skipped", "other"}
        group = data["by_project"]["mirador-p"]
        assert set(group.keys()) == {"total", "outcomes", "outcome_rates", "success_rate"}


# ---------------------------------------------------------------------------
# GET /v1/analytics/trends
# ---------------------------------------------------------------------------


class TestAnalyticsTrendsContract:
    def test_top_level_keys(self, api_client, read_token, seeded_run):
        resp = api_client.get("/v1/analytics/trends", headers=_auth(read_token))
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"bucket", "series"}
        assert len(data["series"]) >= 1
        point = data["series"][0]
        assert set(point.keys()) == {"bucket", "total", "outcomes"}
        assert set(point["outcomes"].keys()) == {"succeeded", "failed", "skipped", "other"}


# ---------------------------------------------------------------------------
# GET /v1/analytics/durations
# ---------------------------------------------------------------------------

_DURATION_STATS_KEYS = {"count", "min", "max", "avg", "p50", "p95", "p99"}


class TestAnalyticsDurationsContract:
    def test_top_level_keys(self, api_client, read_token, seeded_run):
        resp = api_client.get("/v1/analytics/durations", headers=_auth(read_token))
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"overall", "by_project", "by_task"}
        assert set(data["overall"].keys()) == _DURATION_STATS_KEYS
        assert set(data["by_project"]["mirador-p"].keys()) == _DURATION_STATS_KEYS


# ---------------------------------------------------------------------------
# GET /v1/analytics/steps/failures
# ---------------------------------------------------------------------------


class TestAnalyticsStepFailuresContract:
    def test_top_level_keys(self, api_client, read_token, seeded_run):
        resp = api_client.get("/v1/analytics/steps/failures", headers=_auth(read_token))
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"hotspots"}
        assert len(data["hotspots"]) >= 1
        assert set(data["hotspots"][0].keys()) == {"step", "status", "count"}


# ---------------------------------------------------------------------------
# GET /v1/analytics/approvals/latency
# ---------------------------------------------------------------------------


class TestAnalyticsApprovalLatencyContract:
    def test_top_level_keys(self, api_client, read_token, seeded_run):
        resp = api_client.get("/v1/analytics/approvals/latency", headers=_auth(read_token))
        assert resp.status_code == 200
        data = resp.json()
        # Not wrapped in an envelope — the endpoint returns `_duration_stats(...)` directly.
        assert set(data.keys()) == _DURATION_STATS_KEYS


# ---------------------------------------------------------------------------
# GET /v1/analytics/providers
# ---------------------------------------------------------------------------


class TestAnalyticsProvidersContract:
    def test_top_level_keys(self, api_client, read_token, seeded_run):
        resp = api_client.get("/v1/analytics/providers", headers=_auth(read_token))
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"by_provider", "by_model"}
        assert len(data["by_provider"]) >= 1
        provider_row = data["by_provider"][0]
        assert set(provider_row.keys()) == {
            "provider",
            "total",
            "outcomes",
            "outcome_rates",
            "success_rate",
        }
        model_row = data["by_model"][0]
        assert set(model_row.keys()) == {
            "model",
            "total",
            "outcomes",
            "outcome_rates",
            "success_rate",
        }


# ---------------------------------------------------------------------------
# GET /v1/analytics/cost
# ---------------------------------------------------------------------------

_COST_ACCUMULATION_KEYS = {
    "total_steps",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "unpriced_steps",
}


class TestAnalyticsCostContract:
    def test_top_level_keys(self, api_client, read_token, seeded_run):
        resp = api_client.get("/v1/analytics/cost", headers=_auth(read_token))
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"overall", "by_provider", "by_model"}
        assert set(data["overall"].keys()) == _COST_ACCUMULATION_KEYS
        assert "unpriced_steps" in data["overall"]
        provider_row = data["by_provider"][0]
        assert set(provider_row.keys()) == _COST_ACCUMULATION_KEYS | {"provider"}
        model_row = data["by_model"][0]
        assert set(model_row.keys()) == _COST_ACCUMULATION_KEYS | {"model"}


# ---------------------------------------------------------------------------
# GET /v1/plugins/health
# ---------------------------------------------------------------------------


class TestPluginsHealthContract:
    def test_top_level_keys(self, api_client, read_token, monkeypatch):
        from hivepilot.plugins import HealthStatus
        from hivepilot.services import api_service

        fake_plugins = SimpleNamespace(check_all=lambda: {"mem0": HealthStatus("ok", "self-host")})
        monkeypatch.setattr(
            api_service, "_get_orchestrator", lambda: SimpleNamespace(plugins=fake_plugins)
        )
        resp = api_client.get("/v1/plugins/health", headers=_auth(read_token))
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"plugins", "disabled"}
        assert len(data["plugins"]) == 1
        assert set(data["plugins"][0].keys()) == {"name", "status", "detail"}


# ---------------------------------------------------------------------------
# GET /v1/memories
# ---------------------------------------------------------------------------


class TestMemoriesContract:
    def test_configured_shape(self, api_client, admin_token, monkeypatch):
        from hivepilot.services import api_service

        mock_client = MagicMock()
        mock_client.search.return_value = {
            "results": [
                {
                    "id": "1",
                    "memory": "prefers dark mode",
                    "metadata": {"project": "acme-api", "task": "t1"},
                    "score": 0.9,
                },
            ]
        }
        monkeypatch.setattr(api_service, "_get_mem0_client", lambda: mock_client)
        resp = api_client.get("/v1/memories?query=dark+mode", headers=_auth(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) >= {"configured", "memories"}
        assert data["configured"] is True
        assert len(data["memories"]) == 1
        item = data["memories"][0]
        assert set(item.keys()) >= {"memory"}
        assert set(item.keys()) <= {"memory", "id", "metadata", "score"}

    def test_unconfigured_shape(self, api_client, admin_token):
        resp = api_client.get("/v1/memories?query=hello", headers=_auth(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"configured", "memories", "detail"}
        assert data["configured"] is False
        assert data["memories"] == []


# ---------------------------------------------------------------------------
# GET /v1/panels, GET /v1/panels/{name} (Mirador Sprint 4 — panel plugin type)
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded_panel_plugin_manager(tmp_path, monkeypatch):
    """A REAL `PluginManager`, constructed by loading a temp `plugins/`
    directory containing one panel plugin — mirrors
    `tests/test_panels.py::TestPanelRegistration.test_local_plugin_panel_is_collected`'s
    seeding technique (monkeypatch `settings.base_dir`, then instantiate the
    real `PluginManager()`) rather than hand-building a `PanelSpec` dict, so
    this contract test exercises the actual registration + `run_panel_fetch`
    path end-to-end, exactly like `TestPluginsHealthContract` exercises the
    real health-check path elsewhere in this repo's test suite.
    """
    from hivepilot import plugins as plugins_mod

    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "contract_panel.py").write_text(
        "def _fetch():\n"
        "    return {\n"
        "        'sections': [\n"
        "            {'kind': 'stat', 'label': 'steps run', 'value': '42', 'status': 'ok'},\n"
        "            {'kind': 'table', 'columns': ['a'], 'rows': [['1']]},\n"
        "            {'kind': 'text', 'content': 'hello'},\n"
        "        ]\n"
        "    }\n"
        "def register():\n"
        "    return {\n"
        "        'panels': [\n"
        "            {'name': 'contract_panel', 'title': 'Contract Panel', 'fetch': _fetch}\n"
        "        ]\n"
        "    }\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
    return plugins_mod.PluginManager()


def _patch_orchestrator_panels(monkeypatch, plugin_manager) -> None:
    from hivepilot.services import api_service

    monkeypatch.setattr(
        api_service, "_get_orchestrator", lambda: SimpleNamespace(plugins=plugin_manager)
    )


class TestPanelsListContract:
    def test_top_level_keys(self, api_client, read_token, seeded_panel_plugin_manager, monkeypatch):
        """Matches `PanelsResponse`/`PanelSummary` in
        `web/src/lib/mirador-api.ts`: `{"panels": [{"name", "title",
        "min_role"}, ...]}` — a rename of any of these three keys must fail
        this assertion."""
        _patch_orchestrator_panels(monkeypatch, seeded_panel_plugin_manager)
        resp = api_client.get("/v1/panels", headers=_auth(read_token))
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"panels"}
        assert len(data["panels"]) == 1
        assert set(data["panels"][0].keys()) == {"name", "title", "min_role"}
        assert data["panels"][0] == {
            "name": "contract_panel",
            "title": "Contract Panel",
            "min_role": "read",
        }


class TestPanelFetchContract:
    def test_top_level_keys(self, api_client, read_token, seeded_panel_plugin_manager, monkeypatch):
        """Matches `PanelData`/`PanelStatSection`/`PanelTableSection`/
        `PanelTextSection` in `web/src/lib/mirador-api.ts`: a top-level
        `{"sections": [...]}` with one section of each closed kind, each
        carrying exactly its documented fields — a rename of any of these
        keys, or of the `kind` values themselves, must fail this
        assertion."""
        _patch_orchestrator_panels(monkeypatch, seeded_panel_plugin_manager)
        resp = api_client.get("/v1/panels/contract_panel", headers=_auth(read_token))
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"sections"}
        assert len(data["sections"]) == 3

        stat, table, text = data["sections"]
        assert stat["kind"] == "stat"
        assert set(stat.keys()) == {"kind", "label", "value", "status"}
        assert table["kind"] == "table"
        assert set(table.keys()) == {"kind", "columns", "rows"}
        assert text["kind"] == "text"
        assert set(text.keys()) == {"kind", "content"}
