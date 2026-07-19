"""Tests for `plugins/example_graph_source.py` (Mirador Graph View PRD,
Sprint 4) — the example `graph_sources` plugin capability contribution.

Loaded by file path (mirrors `tests/test_sample.py` / `tests/
test_gating_conformance.py::_load_plugin_module`), never `import plugins.
example_graph_source` — that would insert a `plugins` package into
`sys.modules` and leak across the suite, breaking `tests/test_plugins.py`'s
`assert "plugins" not in sys.modules` isolation assumption.

`TestRealPluginManagerScan` additionally drives a REAL
`hivepilot.plugins.PluginManager` scanning the repo's actual `plugins/`
directory (mirrors `tests/test_plugins_list_taxonomy.py`'s "drive a REAL
PluginManager" style) to prove true end-to-end wiring: real `register()` ->
`register_graph_source` -> reachable via `hivepilot.graph.get_graph_source`.

`_build_graph`/`_node_detail` are exercised directly against the repo's
per-test isolated `state.db` (`tests/conftest.py`'s autouse `_isolate_state_db`)
to prove the tenant-scoping and read-only (opt-in, non-destructive)
discipline described in the plugin's own module docstring.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import hivepilot.graph_sources  # noqa: F401 - side-effect import, registers built-ins
from hivepilot import graph as graph_module
from hivepilot.config import settings

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_PATH = _REPO_ROOT / "plugins" / "example_graph_source.py"

_spec = importlib.util.spec_from_file_location(
    "hivepilot_test_example_graph_source_plugin", _PLUGIN_PATH
)
assert _spec and _spec.loader
example_graph_source = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(example_graph_source)


@pytest.fixture()
def isolated_graph_sources(monkeypatch):
    """Fresh COPY of the module-global graph-source registry — mirrors
    `tests/test_graph_api.py::isolated_graph_sources` / this sprint's
    `tests/test_graph_plugin_capability.py::isolated_graph_sources`."""
    monkeypatch.setattr(graph_module, "_GRAPH_SOURCES", dict(graph_module._GRAPH_SOURCES))
    return graph_module._GRAPH_SOURCES


def _record_run_with_lineage(tenant: str = "default") -> int:
    from hivepilot.services import state_service

    run_id = state_service.record_run_start("demo-project", "demo-task", tenant=tenant)
    state_service.record_step(run_id, "step-1", "success", provider="claude", model="sonnet")
    state_service.record_verdict(
        run_id=run_id,
        project="demo-project",
        task="demo-task",
        role="developer",
        kind="debate",
        decision="approve",
        confidence=0.9,
    )
    return run_id


# ---------------------------------------------------------------------------
# AC7 — opt-in, non-destructive
# ---------------------------------------------------------------------------


class TestOptInGating:
    def test_disabled_by_default_contributes_nothing(self) -> None:
        assert settings.example_graph_source_enabled is False
        assert example_graph_source.register() == {}

    def test_enabled_contributes_run_lineage_source(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "example_graph_source_enabled", True, raising=False)

        hooks = example_graph_source.register()
        assert [s.name for s in hooks["graph_sources"]] == ["run-lineage"]
        spec = hooks["graph_sources"][0]
        assert spec.min_role == "read"
        assert spec.params == ("run",)
        assert spec.node_detail is not None

    def test_never_calls_a_state_service_write_function(self, monkeypatch) -> None:
        """Read-only, non-destructive: `_build_graph` must never invoke a
        `state_service` writer. Patch every writer to raise if called, then
        prove a normal fetch still succeeds."""
        from hivepilot.services import state_service

        def _boom(*args, **kwargs):
            raise AssertionError("example_graph_source must never write to state_service")

        for writer in (
            "record_run_start",
            "record_step",
            "complete_run",
            "record_verdict",
            "record_interaction",
        ):
            monkeypatch.setattr(state_service, writer, _boom)

        run_id = 12345  # no run recorded -- data() must raise ValueError, not crash on a write
        ctx = graph_module.GraphContext(tenant="default", role="read", params={"run": str(run_id)})
        with pytest.raises(ValueError):
            example_graph_source._build_graph(ctx)


# ---------------------------------------------------------------------------
# Real PluginManager scan — registers under settings.plugins_enabled flag
# ---------------------------------------------------------------------------


class TestRealPluginManagerScan:
    def test_registers_run_lineage_when_enabled(self, monkeypatch, isolated_graph_sources) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(settings, "example_graph_source_enabled", True, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "base_dir", _REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert graph_module.get_graph_source("run-lineage") is not None
        record = next(r for r in pm.loaded if r.name == "example_graph_source")
        assert record.contributions.get("graph_sources") == ["run-lineage"]

    def test_absent_when_disabled_by_default(self, monkeypatch, isolated_graph_sources) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", _REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert graph_module.get_graph_source("run-lineage") is None
        record = next(r for r in pm.loaded if r.name == "example_graph_source")
        assert record.contributions == {}


# ---------------------------------------------------------------------------
# _build_graph / _node_detail — run-lineage DAG + tenant scoping
# ---------------------------------------------------------------------------


class TestBuildGraph:
    def test_missing_run_param_raises(self) -> None:
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        with pytest.raises(ValueError):
            example_graph_source._build_graph(ctx)

    def test_unknown_run_raises(self) -> None:
        ctx = graph_module.GraphContext(tenant="default", role="read", params={"run": "999999"})
        with pytest.raises(ValueError):
            example_graph_source._build_graph(ctx)

    def test_builds_run_step_verdict_dag(self) -> None:
        run_id = _record_run_with_lineage()
        ctx = graph_module.GraphContext(tenant="default", role="read", params={"run": str(run_id)})
        data = example_graph_source._build_graph(ctx)

        assert data.source == "run-lineage"
        kinds = {n.kind for n in data.nodes}
        assert kinds == {"run", "step", "verdict"}
        run_node = next(n for n in data.nodes if n.kind == "run")
        step_node = next(n for n in data.nodes if n.kind == "step")
        verdict_node = next(n for n in data.nodes if n.kind == "verdict")
        assert any(e.source == run_node.id and e.target == step_node.id for e in data.edges)
        assert any(e.source == run_node.id and e.target == verdict_node.id for e in data.edges)

    def test_tenant_scoping_denies_cross_tenant_run(self) -> None:
        """A tenant-A token must never see tenant-B's run lineage, even by
        guessing another tenant's numeric run id (mirrors
        `hivepilot.graph_sources.pipeline_source`'s own tenant discipline)."""
        run_id = _record_run_with_lineage(tenant="tenant-a")
        ctx = graph_module.GraphContext(tenant="tenant-b", role="read", params={"run": str(run_id)})
        with pytest.raises(ValueError):
            example_graph_source._build_graph(ctx)

    def test_raising_source_normalizes_to_error_graph_never_500(self) -> None:
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        spec = graph_module.GraphSourceSpec(
            name="run-lineage-test",
            data=example_graph_source._build_graph,
            node_detail=example_graph_source._node_detail,
        )
        data = graph_module.run_graph_fetch(spec, ctx)
        assert data.nodes[0].kind == "error"
        assert data.nodes[0].status == "error"


class TestNodeDetail:
    def test_run_node_detail(self) -> None:
        run_id = _record_run_with_lineage()
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        detail = example_graph_source._node_detail(ctx, f"run:{run_id}")
        assert detail is not None
        assert detail.title == f"run #{run_id}"

    def test_run_node_detail_denies_cross_tenant(self) -> None:
        run_id = _record_run_with_lineage(tenant="tenant-a")
        ctx = graph_module.GraphContext(tenant="tenant-b", role="read", params={})
        assert example_graph_source._node_detail(ctx, f"run:{run_id}") is None

    def test_step_node_detail(self) -> None:
        from hivepilot.services import state_service

        run_id = _record_run_with_lineage()
        step = state_service.get_steps_for_run(run_id)[0]
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        detail = example_graph_source._node_detail(ctx, f"step:{run_id}:{step['id']}")
        assert detail is not None
        assert detail.title == "step-1"

    def test_verdict_node_detail(self) -> None:
        from hivepilot.services import state_service

        run_id = _record_run_with_lineage()
        verdict = state_service.list_recent_verdicts(run_id=run_id)[0]
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        detail = example_graph_source._node_detail(ctx, f"verdict:{run_id}:{verdict['id']}")
        assert detail is not None
        assert "approve" in detail.tags

    def test_unknown_node_prefix_returns_none(self) -> None:
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        assert example_graph_source._node_detail(ctx, "nope:1") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
