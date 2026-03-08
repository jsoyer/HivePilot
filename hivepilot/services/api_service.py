from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from hivepilot.orchestrator import Orchestrator
from hivepilot.services import state_service, token_service, chatops_service
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

app = FastAPI(title="HivePilot API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = Orchestrator()

registry = CollectorRegistry()
run_counter = Counter("hivepilot_runs_total", "Total runs", ["status"], registry=registry)
run_duration = Histogram("hivepilot_run_duration_seconds", "Run duration", registry=registry)


def require_role(required: str):
    async def dependency(authorization: str = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
        token_value = authorization.split(" ", 1)[1]
        entry = token_service.resolve_token(token_value)
        if not entry:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        if token_service.role_rank(entry.role) < token_service.role_rank(required):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return entry

    return dependency


class RunRequest(BaseModel):
    task: str
    projects: list[str]
    extra_prompt: str | None = None
    auto_git: bool = False


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run", dependencies=[Depends(require_role("run"))])
def run_task(request: RunRequest):
    with run_duration.time():
        results = orchestrator.run_task(
            project_names=request.projects,
            task_name=request.task,
            extra_prompt=request.extra_prompt,
            auto_git=request.auto_git,
        )
    for result in results:
        run_counter.labels(status="success" if result.success else "failure").inc()
    return {"results": [result.__dict__ for result in results]}


@app.get("/projects", dependencies=[Depends(require_role("read"))])
def list_projects():
    return orchestrator.projects.projects


@app.get("/tasks", dependencies=[Depends(require_role("read"))])
def list_tasks():
    return list(orchestrator.tasks.tasks.keys())


@app.get("/approvals", dependencies=[Depends(require_role("run"))])
def pending_approvals():
    return state_service.get_pending_approvals()


class ApprovalAction(BaseModel):
    approver: str = "api"
    approve: bool = True
    reason: str | None = None


@app.post("/approvals/{run_id}", dependencies=[Depends(require_role("approve"))])
def handle_approval(run_id: int, action: ApprovalAction):
    with run_duration.time():
        result = orchestrator.run_approved(
            run_id=run_id,
            approve=action.approve,
            approver=action.approver,
            reason=action.reason,
        )
    run_counter.labels(status="success" if result.success else "failure").inc()
    return {"result": result.__dict__}


@app.post("/chatops/slack")
def slack_handler(payload: Dict[str, Any]):
    response = chatops_service.handle_slack(payload)
    return {"response": response}


@app.post("/chatops/discord")
def discord_handler(payload: Dict[str, Any]):
    response = chatops_service.handle_discord(payload)
    return {"response": response}


@app.post("/chatops/telegram")
def telegram_handler(payload: Dict[str, Any]):
    response = chatops_service.handle_telegram(payload)
    return {"response": response}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(registry), media_type="text/plain")
