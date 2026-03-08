from __future__ import annotations

from hivepilot.utils.logging import get_logger

try:
    from langgraph.graph import END, StateGraph
except ImportError:  # pragma: no cover - optional dependency
    StateGraph = None
    END = "__end__"

logger = get_logger(__name__)


def build_graph(project, payload):
    """Return a simple LangGraph that logs documentation refresh."""
    if StateGraph is None:
        raise RuntimeError("Install hivepilot[langgraph] to run LangGraph workflows.")

    def rewrite_node(state: dict) -> dict:
        logger.info("langgraph.rewrite", project=project.path.name, metadata=payload.metadata)
        return state

    graph = StateGraph(dict)
    graph.add_node("rewrite-docs", rewrite_node)
    graph.set_entry_point("rewrite-docs")
    graph.add_edge("rewrite-docs", END)
    return graph.compile()
