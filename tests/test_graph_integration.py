"""End-to-end integration tests for the Mirador Graph View PRD (Sprint 5).

Exercises every BUILT-IN graph source (`plugins`, `pipeline`, `skills`)
through the real FastAPI app (`TestClient`), spanning source -> API ->
web-consumable (JSON-serializable) shape, rather than unit-testing any
single module in isolation. Mirrors the auth/fixture patterns already used
by `tests/test_graph_api.py`, `tests/test_graph_pipeline_source.py`,
`tests/test_graph_skills_source.py`, and the no-secret-leak seeding style
of `tests/test_graph_no_secret.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot import graph as graph_module
from hivepilot.graph_sources import pipeline_source
from hivepilot.models import (
    PipelineConfig,
    PipelinesFile,
    PipelineStage,
    TaskConfig,
    TasksFile,
    TaskStep,
)
from hivepilot.services.token_service import add_token

_SECRET_VALUE = "sk-graph-integration-secret-should-never-leak"  # noqa: S105 - test fixture value

_PLUGIN_SOURCE = f'''
class _LeakySecretsBackend:
    def resolve(self, ref, settings):
        return "{_SECRET_VALUE}"


def register():
    return {{
        "secrets": {{"gi_leaky_secret": _LeakySecretsBackend()}},
    }}
'''


# ---------------------------------------------------------------------------
# Shared fixtures
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


def _pipeline(stage_names: list[str]) -> PipelinesFile:
    stages = [PipelineStage(name=name, task=f"task-{name}") for name in stage_names]
    return PipelinesFile(pipelines={"demo": PipelineConfig(description="d", stages=stages)})


def _tasks(stage_names: list[str]) -> TasksFile:
    tasks = {
        f"task-{name}": TaskConfig(
            description="d",
            role=None,
            steps=[TaskStep(name="step-1", runner="claude")],
        )
        for name in stage_names
    }
    return TasksFile(tasks=tasks)


@pytest.fixture()
def patched_pipeline(monkeypatch):
    """A minimal two-stage `demo` pipeline (`A` -> `B`) so `?pipeline=demo`
    resolves — mirrors `tests/test_graph_pipeline_source.py`'s own fixture."""
    stage_names = ["A", "B"]
    monkeypatch.setattr(pipeline_source, "load_pipelines", lambda: _pipeline(stage_names))
    monkeypatch.setattr(pipeline_source, "load_tasks", lambda: _tasks(stage_names))
    return stage_names


@pytest.fixture()
def seeded_secret_plugin_manager(tmp_path, monkeypatch):
    from hivepilot import plugins as plugins_mod

    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "gi_leaky_secret_plugin.py").write_text(_PLUGIN_SOURCE, encoding="utf-8")

    monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
    return plugins_mod.PluginManager()


@pytest.fixture()
def patched_orchestrator(monkeypatch, seeded_secret_plugin_manager):
    from hivepilot.services import api_service

    monkeypatch.setattr(
        api_service,
        "_get_orchestrator",
        lambda: SimpleNamespace(plugins=seeded_secret_plugin_manager),
    )
    return seeded_secret_plugin_manager


# ---------------------------------------------------------------------------
# GET /v1/graph/sources — lists every built-in with correct min_role
# ---------------------------------------------------------------------------


class TestGraphSourcesListing:
    def test_lists_all_three_builtins_with_correct_min_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("admin")
        resp = api_client.get("/v1/graph/sources", headers=_auth(raw))
        assert resp.status_code == 200
        by_name = {s["name"]: s for s in resp.json()["sources"]}
        assert by_name["plugins"]["min_role"] == "read"
        assert by_name["pipeline"]["min_role"] == "read"
        assert by_name["pipeline"]["params"] == ["pipeline"]
        assert by_name["skills"]["min_role"] == "admin"


# ---------------------------------------------------------------------------
# GET /v1/graph/{source} — JSON-serializable GraphData per source
# ---------------------------------------------------------------------------


class TestGraphDataPerSource:
    def test_plugins_source_json_serializable(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/plugins", headers=_auth(raw))
        assert resp.status_code == 200
        body = resp.json()
        assert json.dumps(body)  # web-consumable shape
        assert body["source"] == "plugins"
        assert len(body["nodes"]) >= 1

    def test_pipeline_source_with_param_json_serializable(
        self, api_client, tmp_tokens_file, patched_pipeline
    ):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/pipeline?pipeline=demo", headers=_auth(raw))
        assert resp.status_code == 200
        body = resp.json()
        assert json.dumps(body)
        assert body["source"] == "pipeline"
        assert body["layout_hint"] == "dag"
        stage_kinds = {n["kind"] for n in body["nodes"]}
        assert "stage" in stage_kinds

    def test_skills_source_admin_json_serializable(self, api_client, tmp_tokens_file):
        raw, _ = add_token("admin")
        resp = api_client.get("/v1/graph/skills", headers=_auth(raw))
        assert resp.status_code == 200
        assert json.dumps(resp.json())


# ---------------------------------------------------------------------------
# GET /v1/graph/{source}/node/{id} — GraphDetail.sections match the
# PanelData section contract the web PanelRenderer consumes
# ---------------------------------------------------------------------------


class TestGraphNodeDetailPanelContract:
    def test_plugins_node_detail_sections_match_panel_contract(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/plugins/node/role:developer", headers=_auth(raw))
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"title", "tags", "sections"}
        assert body["sections"], "expected at least one section"
        for section in body["sections"]:
            assert section["kind"] in {"stat", "table", "text"}

    def test_pipeline_node_detail_sections_match_panel_contract(
        self, api_client, tmp_tokens_file, patched_pipeline
    ):
        raw, _ = add_token("read")
        list_resp = api_client.get("/v1/graph/pipeline?pipeline=demo", headers=_auth(raw))
        stage_node_id = next(n["id"] for n in list_resp.json()["nodes"] if n["kind"] == "stage")
        resp = api_client.get(f"/v1/graph/pipeline/node/{stage_node_id}", headers=_auth(raw))
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"title", "tags", "sections"}
        for section in body["sections"]:
            assert section["kind"] in {"stat", "table", "text"}


# ---------------------------------------------------------------------------
# Role-gating end to end + unknown source
# ---------------------------------------------------------------------------


class TestRoleGatingEndToEnd:
    def test_read_token_200_on_plugins_and_pipeline(
        self, api_client, tmp_tokens_file, patched_pipeline
    ):
        raw, _ = add_token("read")
        assert api_client.get("/v1/graph/plugins", headers=_auth(raw)).status_code == 200
        assert (
            api_client.get("/v1/graph/pipeline?pipeline=demo", headers=_auth(raw)).status_code
            == 200
        )

    def test_read_token_403_on_skills(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/skills", headers=_auth(raw))
        assert resp.status_code == 403

    def test_admin_token_200_on_skills(self, api_client, tmp_tokens_file):
        raw, _ = add_token("admin")
        resp = api_client.get("/v1/graph/skills", headers=_auth(raw))
        assert resp.status_code == 200

    def test_unknown_source_404(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        assert api_client.get("/v1/graph/nope", headers=_auth(raw)).status_code == 404
        assert api_client.get("/v1/graph/nope/node/x", headers=_auth(raw)).status_code == 404


# ---------------------------------------------------------------------------
# No secret VALUE ever appears in any /v1/graph/* response body
# ---------------------------------------------------------------------------


class TestNoSecretValueLeak:
    def test_no_secret_value_in_plugins_graph_or_node_detail(
        self, api_client, tmp_tokens_file, patched_orchestrator
    ):
        raw, _ = add_token("read")
        graph_resp = api_client.get("/v1/graph/plugins", headers=_auth(raw))
        assert graph_resp.status_code == 200
        assert _SECRET_VALUE not in graph_resp.text

        secret_detail_resp = api_client.get(
            "/v1/graph/plugins/node/secret:gi_leaky_secret", headers=_auth(raw)
        )
        assert secret_detail_resp.status_code == 200
        assert _SECRET_VALUE not in secret_detail_resp.text

        plugin_detail_resp = api_client.get(
            "/v1/graph/plugins/node/plugin:gi_leaky_secret_plugin", headers=_auth(raw)
        )
        assert plugin_detail_resp.status_code == 200
        assert _SECRET_VALUE not in plugin_detail_resp.text

    def test_no_secret_value_in_pipeline_or_skills_responses(
        self, api_client, tmp_tokens_file, patched_pipeline
    ):
        raw_read, _ = add_token("read")
        pipeline_resp = api_client.get("/v1/graph/pipeline?pipeline=demo", headers=_auth(raw_read))
        assert _SECRET_VALUE not in pipeline_resp.text

        raw_admin, _ = add_token("admin")
        skills_resp = api_client.get("/v1/graph/skills", headers=_auth(raw_admin))
        assert _SECRET_VALUE not in skills_resp.text


# ---------------------------------------------------------------------------
# `run-lineage` — the plugin-CONTRIBUTED graph source (Sprint 4's
# `plugins/example_graph_source.py`), driven end to end through the real
# API + a REAL `PluginManager` scan of the repo's own `plugins/` directory
# (not a synthetic plugin file). Mirrors the fixture pattern in
# `tests/test_graph_plugin_capability.py::isolated_graph_sources` and
# `tests/test_example_graph_source.py::TestRealPluginManagerScan`.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def isolated_graph_sources(monkeypatch):
    """Fresh COPY of the module-global graph-source registry, seeded with
    whatever built-ins are already registered — mirrors
    `tests/test_graph_api.py::isolated_graph_sources` /
    `tests/test_graph_plugin_capability.py::isolated_graph_sources` so a
    plugin-contributed source registered by this test never leaks into
    another test module."""
    monkeypatch.setattr(graph_module, "_GRAPH_SOURCES", dict(graph_module._GRAPH_SOURCES))
    return graph_module._GRAPH_SOURCES


@pytest.fixture()
def run_lineage_registered(monkeypatch, isolated_graph_sources):
    """Opts in `example_graph_source_enabled` and drives a REAL
    `PluginManager` scan of the repo's actual `plugins/` directory so
    `run-lineage` registers through the genuine load path, exactly as it
    would in production -- not a hand-written test-only plugin file."""
    from hivepilot import plugins as plugins_mod
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "example_graph_source_enabled", True, raising=False)
    monkeypatch.setattr(plugins_mod.settings, "base_dir", _REPO_ROOT, raising=False)
    plugins_mod.PluginManager()
    assert graph_module.get_graph_source("run-lineage") is not None


def _record_lineage_run(tenant: str) -> int:
    from hivepilot.services import state_service

    run_id = state_service.record_run_start("gi-lineage-project", "gi-lineage-task", tenant=tenant)
    state_service.record_step(run_id, "step-1", "success", provider="claude", model="sonnet")
    return run_id


class TestRunLineageGraphSourceIntegration:
    def test_run_lineage_listed_in_sources_with_min_role_and_params(
        self, api_client, tmp_tokens_file, run_lineage_registered
    ):
        raw, _ = add_token("admin")
        resp = api_client.get("/v1/graph/sources", headers=_auth(raw))
        assert resp.status_code == 200
        by_name = {s["name"]: s for s in resp.json()["sources"]}
        assert by_name["run-lineage"]["min_role"] == "read"
        assert by_name["run-lineage"]["params"] == ["run"]

    def test_tenant_b_cannot_see_tenant_a_run_gets_normalized_error_node_never_500(
        self, api_client, tmp_tokens_file, run_lineage_registered
    ):
        run_id = _record_lineage_run(tenant="tenant-a")
        raw_b, _ = add_token("read", tenant="tenant-b")

        resp = api_client.get(f"/v1/graph/run-lineage?run={run_id}", headers=_auth(raw_b))

        assert resp.status_code == 200  # never a 500 -- run_graph_fetch normalizes
        body = resp.json()
        assert body["nodes"][0]["kind"] == "error"
        assert body["nodes"][0]["status"] == "error"
        # No tenant-A run data (project/task/step names) ever reached the response.
        dumped = json.dumps(body)
        assert "gi-lineage-project" not in dumped
        assert "gi-lineage-task" not in dumped
        assert "step-1" not in dumped

    def test_no_secret_value_in_run_lineage_response(
        self, api_client, tmp_tokens_file, run_lineage_registered, patched_orchestrator
    ):
        run_id = _record_lineage_run(tenant="default")
        raw, _ = add_token("read")

        resp = api_client.get(f"/v1/graph/run-lineage?run={run_id}", headers=_auth(raw))

        assert resp.status_code == 200
        assert _SECRET_VALUE not in resp.text


# ---------------------------------------------------------------------------
# Sanity: the graph module's own registry is untouched by these tests
# (built-ins are registered once at import time, never manager-owned).
# ---------------------------------------------------------------------------


def test_all_three_builtins_remain_registered_after_suite():
    assert graph_module.get_graph_source("plugins") is not None
    assert graph_module.get_graph_source("pipeline") is not None
    assert graph_module.get_graph_source("skills") is not None
