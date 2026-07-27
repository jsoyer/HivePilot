"""TDD-hook satisfying smoke test for `hivepilot/graph.py`.

The hook's naming convention expects `tests/test_graph.py` for
`hivepilot/graph.py`; the sprint spec for the Mirador Graph View PRD
(Sprint 1) instead names the real, exhaustive contract test suite
`tests/test_graph_contract.py` (plus `test_graph_plugins_source.py`,
`test_graph_api.py`, `test_graph_no_secret.py`). This file is a thin,
genuinely-executed smoke test — not a stub — so it adds real (if minimal)
coverage rather than being a dead marker.
"""

from __future__ import annotations


def test_graph_module_importable_with_core_symbols() -> None:
    import hivepilot.graph as graph

    assert hasattr(graph, "GraphNode")
    assert hasattr(graph, "GraphEdge")
    assert hasattr(graph, "GraphData")
    assert hasattr(graph, "GraphDetail")
    assert hasattr(graph, "GraphContext")
    assert hasattr(graph, "GraphSourceSpec")
    assert hasattr(graph, "register_graph_source")
    assert hasattr(graph, "list_graph_sources")
    assert hasattr(graph, "get_graph_source")
    assert hasattr(graph, "run_graph_fetch")
    assert hasattr(graph, "run_graph_node_detail")


# ---------------------------------------------------------------------------
# `safe_param_options` — the never-raise reader behind `/v1/graph/sources`'
# `param_options` hint. Every degradation below must yield `{}` or a partial
# mapping, because a consumer treats a missing entry as "not enumerable, fall
# back to a free-text field" — never as "no values allowed".
# ---------------------------------------------------------------------------


def _spec(**kwargs):
    from hivepilot.graph import GraphData, GraphSourceSpec

    defaults = {
        "name": "s",
        "data": lambda ctx: GraphData(source="s"),
        "params": ("pipeline",),
    }
    defaults.update(kwargs)
    return GraphSourceSpec(**defaults)


def test_safe_param_options_returns_empty_when_no_provider() -> None:
    from hivepilot.graph import safe_param_options

    assert safe_param_options(_spec()) == {}


def test_safe_param_options_sorts_and_dedupes() -> None:
    from hivepilot.graph import safe_param_options

    spec = _spec(param_options=lambda: {"pipeline": ["b", "a", "b", " a ", ""]})
    assert safe_param_options(spec) == {"pipeline": ["a", "b"]}


def test_safe_param_options_swallows_a_raising_provider() -> None:
    from hivepilot.graph import safe_param_options

    def boom():
        raise RuntimeError("bad config")

    assert safe_param_options(_spec(param_options=boom)) == {}


def test_safe_param_options_drops_undeclared_params() -> None:
    from hivepilot.graph import safe_param_options

    spec = _spec(param_options=lambda: {"pipeline": ["a"], "not_declared": ["x"]})
    assert safe_param_options(spec) == {"pipeline": ["a"]}


def test_safe_param_options_drops_non_string_and_non_sequence_values() -> None:
    from hivepilot.graph import safe_param_options

    assert safe_param_options(_spec(param_options=lambda: {"pipeline": "abc"})) == {}
    assert safe_param_options(_spec(param_options=lambda: {"pipeline": 3})) == {}
    assert safe_param_options(_spec(param_options=lambda: {"pipeline": [1, None]})) == {}
    assert safe_param_options(_spec(param_options=lambda: ["not", "a", "mapping"])) == {}


def test_safe_param_options_omits_a_param_with_no_values() -> None:
    from hivepilot.graph import safe_param_options

    assert safe_param_options(_spec(param_options=lambda: {"pipeline": []})) == {}


def test_pipeline_source_declares_enumerable_pipeline_names() -> None:
    """The built-in `pipeline` source must offer its declared pipeline names,
    which is what lets Pollen render a pick-list instead of the free-text box
    that made the Graph view open empty."""
    from hivepilot.graph_sources.pipeline_source import PIPELINE_GRAPH_SOURCE

    assert PIPELINE_GRAPH_SOURCE.param_options is not None
    from hivepilot.graph import safe_param_options

    # Never raises whatever the ambient config looks like.
    options = safe_param_options(PIPELINE_GRAPH_SOURCE)
    assert set(options).issubset({"pipeline"})
    assert all(isinstance(v, str) for v in options.get("pipeline", []))
