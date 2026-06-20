"""HivePilot worker — runs agent steps dispatched by a remote hub (W1).

A worker is a HivePilot instance on another machine. The hub POSTs a step to
``/run-step``; the worker builds a runner locally and returns its stdout. The hub
owns all state (runs, approvals, checkpoints); the worker is a stateless executor.
"""

from __future__ import annotations

from typing import Any

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.registry import RunnerRegistry
from hivepilot.runners.base import RunnerPayload
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


def execute_step(body: dict[str, Any]) -> str:
    """Build a runner from the dispatched body and run it locally, returning stdout."""
    rdef = RunnerDefinition(
        kind=body["kind"],
        model=body.get("model"),
        command=body.get("command"),
        # host is intentionally None here — the worker runs the agent locally.
    )
    project = ProjectConfig(path=body["project_path"])
    step = TaskStep(
        name=body["step_name"],
        runner=body["kind"],
        prompt_file=body.get("prompt_file"),
    )
    payload = RunnerPayload(
        project_name=body.get("project_name") or project.path.name,
        project=project,
        task_name=body.get("task_name", "remote"),
        step=step,
        metadata=body.get("metadata") or {},
        secrets={},
    )
    logger.info("worker.run_step", kind=rdef.kind, step=step.name, project=payload.project_name)
    return RunnerRegistry({}).capture_definition(rdef, payload)


def create_app():
    """Build the worker FastAPI app (bearer-token auth when worker_token is set)."""
    from fastapi import FastAPI, Header, HTTPException

    app = FastAPI(title="HivePilot Worker")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/run-step")
    async def run_step(
        body: dict[str, Any], authorization: str | None = Header(None)
    ) -> dict[str, str]:
        token = settings.worker_token
        if token and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="unauthorized")
        return {"output": execute_step(body)}

    return app
