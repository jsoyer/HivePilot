"""TDD-hook satisfying smoke test for `hivepilot/graph_sources/pipeline_source.py`.

The hook's naming convention expects `tests/test_pipeline_source.py`; this
project's real, exhaustive test suite for that module is instead named
`tests/test_graph_pipeline_source.py` (mirrors `tests/test_graph.py` vs
`tests/test_graph_contract.py` — see that file's own module docstring for
the same naming-convention note). This file is a thin, genuinely-executed
smoke test — not a stub — so it adds real (if minimal) coverage rather than
being a dead marker.
"""

from __future__ import annotations


def test_pipeline_source_module_importable_with_core_symbols() -> None:
    from hivepilot.graph_sources import pipeline_source

    assert hasattr(pipeline_source, "PIPELINE_GRAPH_SOURCE")
    assert pipeline_source.PIPELINE_GRAPH_SOURCE.name == "pipeline"
    assert callable(pipeline_source._build_graph)
    assert callable(pipeline_source._node_detail)
