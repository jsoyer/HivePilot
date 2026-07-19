"""Contract tests for `hivepilot/graph.py` (Mirador Graph View PRD, Sprint
1): the frozen dataclass shapes, the module-level registry, and the
never-raise `normalize_graph_data` / `run_graph_fetch` /
`run_graph_node_detail` wrappers — mirrors the coverage
`tests/test_panels.py` gives `hivepilot/plugins.py`'s `PanelSpec` /
`normalize_panel_data` / `PluginManager.run_panel_fetch`.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from hivepilot import graph as graph_module
from hivepilot.graph import (
    GraphContext,
    GraphData,
    GraphDataError,
    GraphDetail,
    GraphEdge,
    GraphNode,
    GraphSourceNameCollisionError,
    GraphSourceSpec,
    get_graph_source,
    list_graph_sources,
    normalize_graph_data,
    register_graph_source,
    run_graph_fetch,
    run_graph_node_detail,
)


@pytest.fixture()
def isolated_registry(monkeypatch):
    """A fresh, EMPTY graph-source registry for tests that need to control
    exactly what's registered — restored automatically by `monkeypatch` at
    teardown."""
    monkeypatch.setattr(graph_module, "_GRAPH_SOURCES", {})
    return graph_module._GRAPH_SOURCES


# ---------------------------------------------------------------------------
# Dataclass shapes
# ---------------------------------------------------------------------------


class TestGraphNode:
    def test_required_fields_and_defaults(self):
        node = GraphNode(id="a", label="A", kind="k")
        assert node.status is None
        assert node.group is None
        assert node.badges == ()
        assert node.meta == {}

    def test_frozen(self):
        node = GraphNode(id="a", label="A", kind="k")
        with pytest.raises(Exception):
            node.id = "b"  # type: ignore[misc]

    def test_full_fields(self):
        node = GraphNode(
            id="a", label="A", kind="k", status="ok", group="g", badges=("x",), meta={"k": "v"}
        )
        assert node.badges == ("x",)
        assert node.meta == {"k": "v"}


class TestGraphEdge:
    def test_defaults(self):
        edge = GraphEdge(source="a", target="b")
        assert edge.kind is None
        assert edge.label is None

    def test_frozen(self):
        edge = GraphEdge(source="a", target="b")
        with pytest.raises(Exception):
            edge.source = "c"  # type: ignore[misc]


class TestGraphData:
    def test_defaults(self):
        data = GraphData(source="s")
        assert data.nodes == ()
        assert data.edges == ()
        assert data.layout_hint is None


class TestGraphDetail:
    def test_defaults(self):
        detail = GraphDetail(title="t")
        assert detail.tags == ()
        assert detail.sections == ()


class TestGraphContext:
    def test_defaults(self):
        ctx = GraphContext(tenant="default", role="read")
        assert dict(ctx.params) == {}

    def test_frozen(self):
        ctx = GraphContext(tenant="default", role="read")
        with pytest.raises(Exception):
            ctx.tenant = "other"  # type: ignore[misc]


class TestGraphSourceSpec:
    def test_defaults(self):
        spec = GraphSourceSpec(name="alpha", data=lambda ctx: GraphData(source="alpha"))
        assert spec.node_detail is None
        assert spec.title is None
        assert spec.min_role == "read"
        assert spec.params == ()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_list_get(self, isolated_registry):
        spec = GraphSourceSpec(name="alpha", data=lambda ctx: GraphData(source="alpha"))
        register_graph_source(spec)
        assert get_graph_source("alpha") is spec
        assert [s.name for s in list_graph_sources()] == ["alpha"]

    def test_unknown_name_returns_none(self, isolated_registry):
        assert get_graph_source("nope") is None

    def test_list_sorted_by_name(self, isolated_registry):
        register_graph_source(
            GraphSourceSpec(name="zeta", data=lambda ctx: GraphData(source="zeta"))
        )
        register_graph_source(
            GraphSourceSpec(name="alpha", data=lambda ctx: GraphData(source="alpha"))
        )
        assert [s.name for s in list_graph_sources()] == ["alpha", "zeta"]

    def test_duplicate_name_different_spec_raises(self, isolated_registry):
        spec1 = GraphSourceSpec(name="alpha", data=lambda ctx: GraphData(source="alpha"))
        spec2 = GraphSourceSpec(name="alpha", data=lambda ctx: GraphData(source="alpha"))
        register_graph_source(spec1)
        with pytest.raises(GraphSourceNameCollisionError):
            register_graph_source(spec2)

    def test_reregistering_same_object_is_idempotent(self, isolated_registry):
        spec = GraphSourceSpec(name="alpha", data=lambda ctx: GraphData(source="alpha"))
        register_graph_source(spec)
        register_graph_source(spec)  # must not raise
        assert get_graph_source("alpha") is spec


# ---------------------------------------------------------------------------
# normalize_graph_data
# ---------------------------------------------------------------------------


class TestNormalizeGraphData:
    def test_valid_passthrough(self):
        raw = GraphData(
            source="s",
            nodes=(GraphNode(id="a", label="A", kind="k"),),
            edges=(GraphEdge(source="a", target="a"),),
            layout_hint="grid",
        )
        result = normalize_graph_data(raw)
        assert result == raw

    def test_accepts_list_nodes_edges_coerced_to_tuple(self):
        raw = GraphData(source="s", nodes=[GraphNode(id="a", label="A", kind="k")], edges=[])  # type: ignore[arg-type]
        result = normalize_graph_data(raw)
        assert isinstance(result.nodes, tuple)

    def test_not_graphdata_raises(self):
        with pytest.raises(GraphDataError):
            normalize_graph_data({"nodes": []})

    def test_empty_source_raises(self):
        with pytest.raises(GraphDataError):
            normalize_graph_data(GraphData(source=""))

    def test_bad_node_type_raises(self):
        raw = GraphData(source="s", nodes=("not-a-node",))  # type: ignore[arg-type]
        with pytest.raises(GraphDataError):
            normalize_graph_data(raw)

    def test_bad_edge_type_raises(self):
        raw = GraphData(source="s", edges=("not-an-edge",))  # type: ignore[arg-type]
        with pytest.raises(GraphDataError):
            normalize_graph_data(raw)

    def test_invalid_layout_hint_raises(self):
        raw = GraphData(source="s", layout_hint="bogus")
        with pytest.raises(GraphDataError):
            normalize_graph_data(raw)

    def test_none_layout_hint_is_valid(self):
        result = normalize_graph_data(GraphData(source="s", layout_hint=None))
        assert result.layout_hint is None


# ---------------------------------------------------------------------------
# run_graph_fetch — never-raise discipline
# ---------------------------------------------------------------------------


class TestRunGraphFetch:
    def test_happy_path(self):
        spec = GraphSourceSpec(
            name="alpha",
            data=lambda ctx: GraphData(
                source="alpha", nodes=(GraphNode(id="a", label="A", kind="k"),)
            ),
        )
        ctx = GraphContext(tenant="default", role="read")
        result = run_graph_fetch(spec, ctx)
        assert result.source == "alpha"
        assert len(result.nodes) == 1

    def test_raising_source_never_raises_and_normalizes_to_error_node(self):
        def _boom(ctx: GraphContext) -> GraphData:
            raise RuntimeError("boom secret-value-should-not-leak")

        spec = GraphSourceSpec(name="broken", data=_boom)
        ctx = GraphContext(tenant="default", role="read")
        result = run_graph_fetch(spec, ctx)  # must not raise
        assert result.source == "broken"
        assert len(result.nodes) == 1
        assert result.nodes[0].kind == "error"
        assert result.nodes[0].status == "error"
        assert result.nodes[0].label == "RuntimeError"

    def test_malformed_return_never_raises(self):
        spec = GraphSourceSpec(name="malformed", data=lambda ctx: {"not": "graphdata"})
        ctx = GraphContext(tenant="default", role="read")
        result = run_graph_fetch(spec, ctx)
        assert result.nodes[0].status == "error"

    def test_source_field_always_overridden_to_spec_name(self):
        spec = GraphSourceSpec(
            name="alpha", data=lambda ctx: GraphData(source="spoofed-name", nodes=(), edges=())
        )
        ctx = GraphContext(tenant="default", role="read")
        result = run_graph_fetch(spec, ctx)
        assert result.source == "alpha"

    def test_non_json_serializable_meta_value_never_raises_downstream(self):
        """Regression: `GraphNode.meta` passed shape validation (a Mapping)
        but contained a non-JSON-serializable VALUE (e.g. a plugin author
        stashing a raw object) — this used to sail through
        `normalize_graph_data` untouched and only blow up later, as an
        UNCAUGHT 500, at `_graph_data_to_dict` -> FastAPI's JSON encoder
        (hivepilot/services/api_service.py), entirely OUTSIDE
        `run_graph_fetch`'s never-raise `try`/`except`. `_coerce_node` must
        now stringify any such value so the whole response is guaranteed
        JSON-serializable, never just internally well-shaped."""
        import json

        from hivepilot.services.api_service import _graph_data_to_dict

        class _Unserializable:
            def __repr__(self) -> str:
                return "<unserializable>"

        spec = GraphSourceSpec(
            name="alpha",
            data=lambda ctx: GraphData(
                source="alpha",
                nodes=(
                    GraphNode(
                        id="a",
                        label="A",
                        kind="k",
                        meta={"bad": _Unserializable(), 123: "non-str-key"},
                    ),
                ),
            ),
        )
        ctx = GraphContext(tenant="default", role="read")
        result = run_graph_fetch(spec, ctx)  # must not raise
        payload = _graph_data_to_dict(result)
        json.dumps(payload)  # must not raise
        assert result.nodes[0].meta["bad"] == "<unserializable>"
        assert result.nodes[0].meta["123"] == "non-str-key"


# ---------------------------------------------------------------------------
# run_graph_node_detail — never-raise discipline
# ---------------------------------------------------------------------------


class TestRunGraphNodeDetail:
    def test_no_node_detail_callable_returns_none(self):
        spec = GraphSourceSpec(name="alpha", data=lambda ctx: GraphData(source="alpha"))
        ctx = GraphContext(tenant="default", role="read")
        assert run_graph_node_detail(spec, ctx, "x") is None

    def test_node_detail_returning_none_returns_none(self):
        spec = GraphSourceSpec(
            name="alpha",
            data=lambda ctx: GraphData(source="alpha"),
            node_detail=lambda ctx, node_id: None,
        )
        ctx = GraphContext(tenant="default", role="read")
        assert run_graph_node_detail(spec, ctx, "x") is None

    def test_happy_path(self):
        spec = GraphSourceSpec(
            name="alpha",
            data=lambda ctx: GraphData(source="alpha"),
            node_detail=lambda ctx, node_id: GraphDetail(title=node_id, tags=("t",), sections=()),
        )
        ctx = GraphContext(tenant="default", role="read")
        detail = run_graph_node_detail(spec, ctx, "x")
        assert detail is not None
        assert detail.title == "x"

    def test_raising_node_detail_never_raises_and_normalizes_to_error_detail(self):
        def _boom(ctx: GraphContext, node_id: str) -> GraphDetail:
            raise RuntimeError("boom secret-value-should-not-leak")

        spec = GraphSourceSpec(
            name="alpha", data=lambda ctx: GraphData(source="alpha"), node_detail=_boom
        )
        ctx = GraphContext(tenant="default", role="read")
        detail = run_graph_node_detail(spec, ctx, "x")  # must not raise
        assert detail is not None
        assert detail.tags == ("error",)
        assert detail.sections[0]["content"] == "RuntimeError"

    def test_malformed_detail_return_never_raises(self):
        spec = GraphSourceSpec(
            name="alpha",
            data=lambda ctx: GraphData(source="alpha"),
            node_detail=lambda ctx, node_id: {"not": "graphdetail"},
        )
        ctx = GraphContext(tenant="default", role="read")
        detail = run_graph_node_detail(spec, ctx, "x")
        assert detail is not None
        assert detail.tags == ("error",)


# ---------------------------------------------------------------------------
# Acceptance criterion 7 — no web/Textual optional dependency
# ---------------------------------------------------------------------------


class TestNoOptionalDeps:
    def test_import_hivepilot_graph_succeeds_standalone(self):
        result = subprocess.run(
            [sys.executable, "-c", "import hivepilot.graph"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
