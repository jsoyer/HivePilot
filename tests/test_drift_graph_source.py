"""Tests for `plugins/drift_graph_source.py` (Mirador GraphSources plugin
sprint) -- the plugin-contributed `drift` graph source over the drift-scan
history table (`state_service.record_drift_scan`/`get_recent_drift_scans`/
`get_drift_baseline`).

Loaded by file path (mirrors `tests/test_example_graph_source.py`), never
`import plugins.drift_graph_source` -- that would insert a `plugins` package
into `sys.modules` and leak across the suite (see that file's own docstring
for the full rationale, `tests/test_plugins.py`'s
`assert "plugins" not in sys.modules` isolation assumption).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import hivepilot.graph_sources  # noqa: F401 - side-effect import, registers built-ins
from hivepilot import graph as graph_module
from hivepilot.config import settings

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_PATH = _REPO_ROOT / "plugins" / "drift_graph_source.py"

_spec = importlib.util.spec_from_file_location(
    "hivepilot_test_drift_graph_source_plugin", _PLUGIN_PATH
)
assert _spec and _spec.loader
drift_graph_source = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drift_graph_source)


@pytest.fixture()
def isolated_graph_sources(monkeypatch):
    monkeypatch.setattr(graph_module, "_GRAPH_SOURCES", dict(graph_module._GRAPH_SOURCES))
    return graph_module._GRAPH_SOURCES


def _seed_scan(
    *,
    project: str = "demo-project",
    tenant: str = "default",
    drifted: bool = True,
    to_add: int = 1,
    to_change: int = 2,
    to_destroy: int = 3,
) -> int:
    from hivepilot.services import state_service
    from hivepilot.services.drift_service import DriftResult, DriftSummary

    summary = DriftSummary(to_add=to_add, to_change=to_change, to_destroy=to_destroy)
    result = DriftResult(project=project, runner="opentofu", drifted=drifted, summary=summary)
    return state_service.record_drift_scan(result, tenant=tenant)


# ---------------------------------------------------------------------------
# Opt-in gating
# ---------------------------------------------------------------------------


class TestOptInGating:
    def test_disabled_by_default_contributes_nothing(self) -> None:
        assert settings.drift_graph_source_enabled is False
        assert drift_graph_source.register() == {}

    def test_enabled_contributes_drift_source(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "drift_graph_source_enabled", True, raising=False)

        hooks = drift_graph_source.register()
        assert [s.name for s in hooks["graph_sources"]] == ["drift"]
        spec = hooks["graph_sources"][0]
        assert spec.min_role == "read"
        assert spec.node_detail is not None

    def test_never_calls_a_state_service_write_function(self, monkeypatch) -> None:
        from hivepilot.services import state_service

        def _boom(*args, **kwargs):
            raise AssertionError("drift_graph_source must never write to state_service")

        for writer in ("record_drift_scan",):
            monkeypatch.setattr(state_service, writer, _boom)

        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        # No scans seeded -- must not raise, must not touch a writer.
        data = drift_graph_source._build_graph(ctx)
        assert data.nodes == ()


class TestRealPluginManagerScan:
    def test_registers_drift_when_enabled(self, monkeypatch, isolated_graph_sources) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(settings, "drift_graph_source_enabled", True, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "base_dir", _REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert graph_module.get_graph_source("drift") is not None
        record = next(r for r in pm.loaded if r.name == "drift_graph_source")
        assert record.contributions.get("graph_sources") == ["drift"]

    def test_absent_when_disabled_by_default(self, monkeypatch, isolated_graph_sources) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", _REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert graph_module.get_graph_source("drift") is None
        record = next(r for r in pm.loaded if r.name == "drift_graph_source")
        assert record.contributions == {}


# ---------------------------------------------------------------------------
# _build_graph -- project/scan nodes, counts, tenant scoping
# ---------------------------------------------------------------------------


class TestBuildGraph:
    def test_no_scans_yields_empty_graph(self) -> None:
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        data = drift_graph_source._build_graph(ctx)
        assert data.source == "drift"
        assert data.nodes == ()
        assert data.edges == ()

    def test_builds_project_and_scan_nodes_with_counts(self) -> None:
        scan_id = _seed_scan(to_add=4, to_change=5, to_destroy=6)
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        data = drift_graph_source._build_graph(ctx)

        project_nodes = [n for n in data.nodes if n.kind == "project"]
        scan_nodes = [n for n in data.nodes if n.kind == "drift_scan"]
        assert len(project_nodes) == 1
        assert project_nodes[0].label == "demo-project"
        assert len(scan_nodes) == 1
        scan_node = scan_nodes[0]
        assert scan_node.id == f"scan:{scan_id}"
        assert scan_node.status == "drift"
        assert scan_node.meta["to_add"] == 4
        assert scan_node.meta["to_change"] == 5
        assert scan_node.meta["to_destroy"] == 6
        assert isinstance(scan_node.meta["to_add"], int)
        assert any(e.source == project_nodes[0].id and e.target == scan_node.id for e in data.edges)

    def test_ok_scan_status_rendered(self) -> None:
        _seed_scan(drifted=False, to_add=0, to_change=0, to_destroy=0)
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        data = drift_graph_source._build_graph(ctx)
        scan_node = next(n for n in data.nodes if n.kind == "drift_scan")
        assert scan_node.status == "ok"

    def test_tenant_scoping_denies_cross_tenant_scans(self) -> None:
        _seed_scan(project="tenant-a-project", tenant="tenant-a")
        ctx = graph_module.GraphContext(tenant="tenant-b", role="read", params={})
        data = drift_graph_source._build_graph(ctx)
        assert data.nodes == ()

    def test_project_param_filters(self) -> None:
        _seed_scan(project="p1")
        _seed_scan(project="p2")
        ctx = graph_module.GraphContext(tenant="default", role="read", params={"project": "p1"})
        data = drift_graph_source._build_graph(ctx)
        project_nodes = [n for n in data.nodes if n.kind == "project"]
        assert [n.label for n in project_nodes] == ["p1"]

    def test_raising_source_normalizes_to_error_graph_never_500(self, monkeypatch) -> None:
        from hivepilot.services import state_service

        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(state_service, "get_recent_drift_scans", _boom)
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        spec = graph_module.GraphSourceSpec(
            name="drift-test",
            data=drift_graph_source._build_graph,
            node_detail=drift_graph_source._node_detail,
        )
        data = graph_module.run_graph_fetch(spec, ctx)
        assert data.nodes[0].kind == "error"
        assert data.nodes[0].status == "error"


class TestNodeDetail:
    def test_scan_node_detail(self) -> None:
        scan_id = _seed_scan(to_add=1, to_change=2, to_destroy=3)
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        detail = drift_graph_source._node_detail(ctx, f"scan:{scan_id}")
        assert detail is not None
        assert "demo-project" in detail.title

    def test_scan_node_detail_denies_cross_tenant(self) -> None:
        scan_id = _seed_scan(tenant="tenant-a")
        ctx = graph_module.GraphContext(tenant="tenant-b", role="read", params={})
        assert drift_graph_source._node_detail(ctx, f"scan:{scan_id}") is None

    def test_unknown_node_prefix_returns_none(self) -> None:
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        assert drift_graph_source._node_detail(ctx, "nope:1") is None

    def test_project_node_detail_returns_none(self) -> None:
        _seed_scan()
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        assert drift_graph_source._node_detail(ctx, "project:demo-project") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
