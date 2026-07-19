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
