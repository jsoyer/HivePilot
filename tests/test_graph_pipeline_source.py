"""Tests for the built-in `pipeline` graph source (Mirador Graph View PRD,
Sprint 2) — `hivepilot/graph_sources/pipeline_source.py`.

`load_pipelines`/`load_tasks` are monkeypatched directly on the module
(mirrors `tests/test_orchestrator*.py`'s own patching of the SAME
functions) rather than writing real `pipelines.yaml`/`tasks.yaml` fixture
files — keeps each test's topology self-contained and explicit. `state.db`
reads/writes go through the REAL `state_service` against the autouse
per-test tmp DB (`tests/conftest.py`'s `_isolate_state_db`), so tenant
filtering is exercised end to end, not mocked away.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot import graph as graph_module
from hivepilot.graph_sources import pipeline_source
from hivepilot.graph_sources.pipeline_source import (
    PIPELINE_GRAPH_SOURCE,
    _build_graph,
    _node_detail,
)
from hivepilot.models import PipelineConfig, PipelinesFile, PipelineStage, TaskConfig, TasksFile
from hivepilot.roles import Role

_SECRET_VALUE = "sk-pipeline-secret-should-never-leak"  # noqa: S105 - test fixture value


def _pipeline(stage_names: list[str]) -> PipelinesFile:
    stages = [PipelineStage(name=name, task=f"task-{name}") for name in stage_names]
    return PipelinesFile(pipelines={"demo": PipelineConfig(description="d", stages=stages)})


def _tasks(
    stage_names: list[str], *, role: str | None = None, step_name: str = "step-1"
) -> TasksFile:
    tasks = {
        f"task-{name}": TaskConfig(
            description="d",
            role=role,
            steps=[{"name": step_name, "runner": "claude"}],
        )
        for name in stage_names
    }
    return TasksFile(tasks=tasks)


@pytest.fixture()
def ctx():
    return graph_module.GraphContext(tenant="default", role="read", params={"pipeline": "demo"})


@pytest.fixture()
def patched_config(monkeypatch):
    """Patch `pipeline_source.load_pipelines`/`.load_tasks` with a simple
    two-stage `demo` pipeline (`A` -> `B`), each stage's task having a
    single step named `step-1` and no role (keyed-routing tests override
    the role separately)."""
    stage_names = ["A", "B"]
    monkeypatch.setattr(pipeline_source, "load_pipelines", lambda: _pipeline(stage_names))
    monkeypatch.setattr(pipeline_source, "load_tasks", lambda: _tasks(stage_names))
    return stage_names


def _record_stage_run(tenant: str, *, step_status: str = "success") -> int:
    from hivepilot.services import state_service

    run_id = state_service.record_run_start("demo", "demo", status="complete", tenant=tenant)
    state_service.record_step(run_id, "step-1", step_status)
    return run_id


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestPipelineGraphSourceRegistration:
    def test_registered_under_pipeline_name(self):
        import hivepilot.graph_sources  # noqa: F401 - side-effect import

        assert graph_module.get_graph_source("pipeline") is PIPELINE_GRAPH_SOURCE

    def test_spec_shape(self):
        assert PIPELINE_GRAPH_SOURCE.name == "pipeline"
        assert PIPELINE_GRAPH_SOURCE.min_role == "read"
        assert PIPELINE_GRAPH_SOURCE.params == ("pipeline",)
        assert PIPELINE_GRAPH_SOURCE.node_detail is not None


# ---------------------------------------------------------------------------
# _build_graph
# ---------------------------------------------------------------------------


class TestBuildGraph:
    def test_missing_pipeline_param_raises(self, patched_config):
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        with pytest.raises(ValueError):
            _build_graph(ctx)

    def test_unknown_pipeline_raises(self, patched_config):
        ctx = graph_module.GraphContext(tenant="default", role="read", params={"pipeline": "nope"})
        with pytest.raises(ValueError):
            _build_graph(ctx)

    def test_unknown_pipeline_normalizes_to_error_graph_never_500(self, patched_config):
        ctx = graph_module.GraphContext(tenant="default", role="read", params={"pipeline": "nope"})
        data = graph_module.run_graph_fetch(PIPELINE_GRAPH_SOURCE, ctx)
        assert data.nodes[0].kind == "error"
        assert data.nodes[0].status == "error"

    def test_stage_nodes_present_in_order(self, ctx, patched_config):
        data = _build_graph(ctx)
        assert data.source == "pipeline"
        assert data.layout_hint == "dag"
        labels = [n.label for n in data.nodes]
        assert labels == ["A", "B"]
        assert all(n.kind == "stage" for n in data.nodes)
        assert all(n.group == "demo" for n in data.nodes)

    def test_flow_edge_between_consecutive_stages(self, ctx, patched_config):
        data = _build_graph(ctx)
        flow_edges = [e for e in data.edges if e.kind == "flow"]
        assert len(flow_edges) == 1
        assert flow_edges[0].source == "stage:demo:A"
        assert flow_edges[0].target == "stage:demo:B"

    def test_no_run_yet_status_is_none(self, ctx, patched_config):
        data = _build_graph(ctx)
        assert all(n.status is None for n in data.nodes)

    def test_last_run_all_success_status_ok(self, ctx, patched_config):
        _record_stage_run("default", step_status="success")
        data = _build_graph(ctx)
        assert all(n.status == "ok" for n in data.nodes)

    def test_last_run_failed_step_status_error(self, ctx, patched_config):
        _record_stage_run("default", step_status="failed")
        data = _build_graph(ctx)
        assert all(n.status == "error" for n in data.nodes)

    def test_run_with_no_matching_steps_status_warn(self, ctx, patched_config):
        from hivepilot.services import state_service

        # A run recorded for this pipeline exists, but no step named
        # 'step-1' was ever recorded against it.
        state_service.record_run_start("demo", "demo", status="complete", tenant="default")
        data = _build_graph(ctx)
        assert all(n.status == "warn" for n in data.nodes)

    def test_tenant_filters_last_run_never_cross_tenant(self, patched_config):
        """A tenant-A run's outcome must NEVER be visible to a tenant-B
        caller's `pipeline` graph — dedicated two-tenant test (acceptance
        criterion 2)."""
        _record_stage_run("acme", step_status="success")

        ctx_acme = graph_module.GraphContext(
            tenant="acme", role="read", params={"pipeline": "demo"}
        )
        ctx_globex = graph_module.GraphContext(
            tenant="globex", role="read", params={"pipeline": "demo"}
        )

        data_acme = _build_graph(ctx_acme)
        data_globex = _build_graph(ctx_globex)

        assert all(n.status == "ok" for n in data_acme.nodes)
        assert all(n.status is None for n in data_globex.nodes)

    def test_no_secret_value_in_graph_data(self, ctx, patched_config, monkeypatch):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("demo", "demo", status="complete", tenant="default")
        state_service.record_step(run_id, "step-1", "failed", detail=_SECRET_VALUE)
        data = _build_graph(ctx)
        assert _SECRET_VALUE not in str(data)


class TestKeyedContextEdges:
    def test_context_edge_when_keyed_and_keys_overlap(self, monkeypatch):
        stage_names = ["A", "B"]
        monkeypatch.setattr(pipeline_source, "load_pipelines", lambda: _pipeline(stage_names))
        monkeypatch.setattr(
            pipeline_source, "load_tasks", lambda: _tasks(stage_names, role="role_a_for_a")
        )
        # give each stage's task its OWN role name so inputs/outputs differ
        tasks = TasksFile(
            tasks={
                "task-A": TaskConfig(
                    description="d", role="role_a", steps=[{"name": "step-1", "runner": "claude"}]
                ),
                "task-B": TaskConfig(
                    description="d", role="role_b", steps=[{"name": "step-1", "runner": "claude"}]
                ),
            }
        )
        monkeypatch.setattr(pipeline_source, "load_tasks", lambda: tasks)

        fake_roles = {
            "role_a": Role(
                name="role_a",
                title="Role A",
                prompt_file=Path("role_a.md"),
                model_profile="coding",
                inputs=[],
                outputs=["shared_key"],
                can_block=False,
                order=1,
            ),
            "role_b": Role(
                name="role_b",
                title="Role B",
                prompt_file=Path("role_b.md"),
                model_profile="coding",
                inputs=["shared_key"],
                outputs=[],
                can_block=False,
                order=2,
            ),
        }
        import hivepilot.roles as roles_module

        monkeypatch.setattr(roles_module, "ROLES", fake_roles)
        monkeypatch.setattr(pipeline_source.settings, "context_routing_mode", "keyed")

        ctx = graph_module.GraphContext(tenant="default", role="read", params={"pipeline": "demo"})
        data = _build_graph(ctx)
        context_edges = [e for e in data.edges if e.kind == "context"]
        assert len(context_edges) == 1
        assert context_edges[0].source == "stage:demo:A"
        assert context_edges[0].target == "stage:demo:B"

    def test_no_context_edge_when_full_routing_mode(self, monkeypatch):
        stage_names = ["A", "B"]
        tasks = TasksFile(
            tasks={
                "task-A": TaskConfig(
                    description="d", role="role_a", steps=[{"name": "step-1", "runner": "claude"}]
                ),
                "task-B": TaskConfig(
                    description="d", role="role_b", steps=[{"name": "step-1", "runner": "claude"}]
                ),
            }
        )
        monkeypatch.setattr(pipeline_source, "load_pipelines", lambda: _pipeline(stage_names))
        monkeypatch.setattr(pipeline_source, "load_tasks", lambda: tasks)
        import hivepilot.roles as roles_module

        monkeypatch.setattr(
            roles_module,
            "ROLES",
            {
                "role_a": Role(
                    name="role_a",
                    title="Role A",
                    prompt_file=Path("role_a.md"),
                    model_profile="coding",
                    inputs=[],
                    outputs=["shared_key"],
                    can_block=False,
                    order=1,
                ),
                "role_b": Role(
                    name="role_b",
                    title="Role B",
                    prompt_file=Path("role_b.md"),
                    model_profile="coding",
                    inputs=["shared_key"],
                    outputs=[],
                    can_block=False,
                    order=2,
                ),
            },
        )
        # context_routing_mode left at its default ("full") — no monkeypatch.
        assert pipeline_source.settings.context_routing_mode == "full"

        ctx = graph_module.GraphContext(tenant="default", role="read", params={"pipeline": "demo"})
        data = _build_graph(ctx)
        assert not [e for e in data.edges if e.kind == "context"]


# ---------------------------------------------------------------------------
# _node_detail
# ---------------------------------------------------------------------------


class TestNodeDetail:
    def test_unknown_pipeline_returns_none(self, patched_config):
        ctx = graph_module.GraphContext(tenant="default", role="read")
        assert _node_detail(ctx, "stage:nope:A") is None

    def test_unknown_stage_returns_none(self, patched_config):
        ctx = graph_module.GraphContext(tenant="default", role="read")
        assert _node_detail(ctx, "stage:demo:nope") is None

    def test_malformed_node_id_returns_none(self, patched_config):
        ctx = graph_module.GraphContext(tenant="default", role="read")
        assert _node_detail(ctx, "not-a-stage-id") is None
        assert _node_detail(ctx, "stage:onlyname") is None

    def test_known_stage_detail_shape(self, patched_config):
        _record_stage_run("default", step_status="success")
        ctx = graph_module.GraphContext(tenant="default", role="read")
        detail = _node_detail(ctx, "stage:demo:A")
        assert detail is not None
        assert detail.title == "A"
        assert "stage" in detail.tags
        assert "ok" in detail.tags
        kinds = [s["kind"] for s in detail.sections]
        assert kinds.count("stat") == 2
        assert "text" in kinds

    def test_detail_tenant_isolated(self, patched_config):
        _record_stage_run("acme", step_status="success")
        ctx_globex = graph_module.GraphContext(tenant="globex", role="read")
        detail = _node_detail(ctx_globex, "stage:demo:A")
        assert detail is not None
        assert "ok" not in detail.tags


# ---------------------------------------------------------------------------
# Full API — GET /v1/graph/pipeline
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


class TestPipelineGraphApi:
    def test_get_pipeline_dag_via_api(self, api_client, tmp_tokens_file, patched_config):
        from hivepilot.services.token_service import add_token

        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/pipeline?pipeline=demo", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "pipeline"
        assert data["layout_hint"] == "dag"
        assert len(data["nodes"]) == 2

    def test_unknown_pipeline_via_api_never_500(self, api_client, tmp_tokens_file, patched_config):
        from hivepilot.services.token_service import add_token

        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/pipeline?pipeline=ghost", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["nodes"][0]["kind"] == "error"

    def test_tenant_isolation_via_api(self, api_client, tmp_tokens_file, patched_config):
        from hivepilot.services.token_service import add_token

        _record_stage_run("acme", step_status="success")

        raw_acme, _ = add_token("read", tenant="acme")
        raw_globex, _ = add_token("read", tenant="globex")

        resp_acme = api_client.get("/v1/graph/pipeline?pipeline=demo", headers=_auth(raw_acme))
        resp_globex = api_client.get("/v1/graph/pipeline?pipeline=demo", headers=_auth(raw_globex))

        assert all(n["status"] == "ok" for n in resp_acme.json()["nodes"])
        assert all(n["status"] is None for n in resp_globex.json()["nodes"])
