"""Built-in graph sources (Mirador Graph View PRD, Sprint 1). Importing this
package registers every built-in `GraphSourceSpec`, mirroring
`hivepilot/registry.py`'s `_BUILTIN_RUNNERS` module-level registration loop.
Idempotent — safe to import more than once in the same process."""

from __future__ import annotations

from hivepilot.graph import register_graph_source
from hivepilot.graph_sources.plugins_source import PLUGINS_GRAPH_SOURCE


def register_builtin_graph_sources() -> None:
    """Register every built-in graph source. Idempotent (see
    `register_graph_source`'s own docstring)."""
    register_graph_source(PLUGINS_GRAPH_SOURCE)


register_builtin_graph_sources()
