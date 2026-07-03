from __future__ import annotations

from importlib import import_module
from typing import Any, Callable

from hivepilot.models import ProjectConfig, TaskConfig
from hivepilot.runners.base import RunnerPayload


def run_engine(*, task: TaskConfig, project: ProjectConfig, payload: RunnerPayload) -> None:
    if task.engine == "native":
        raise RuntimeError("Native engine should not be dispatched through run_engine.")
    if task.engine == "langgraph":
        graph_callable = _load_callable(task.graph or task.options.get("graph"), "build_graph")
        graph = graph_callable(project, payload)
        if hasattr(graph, "invoke"):
            graph.invoke(payload.metadata)
        else:
            graph(payload.metadata)
    elif task.engine == "crewai":
        crew_callable = _load_callable(task.crew or task.options.get("crew"), "build_crew")
        crew = crew_callable(project, payload)
        if hasattr(crew, "kickoff"):
            crew.kickoff(inputs=payload.metadata)
        else:
            crew(payload.metadata)
    else:
        raise ValueError(f"Unknown engine: {task.engine}")


def _load_callable(target: str | None, attr: str) -> Callable[..., Any]:
    if not target:
        raise ValueError("Engine requires module:function reference.")
    module_name, func_name = target.split(":") if ":" in target else (target, attr)
    module = import_module(module_name)
    return getattr(module, func_name)
