from __future__ import annotations

import csv
import io
import threading
import uuid
from collections import defaultdict
from pathlib import Path
from time import time
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import generate_latest
from pydantic import BaseModel, field_validator

from hivepilot import roles
from hivepilot.config import settings
from hivepilot.orchestrator import Orchestrator
from hivepilot.services import (
    analytics_service,
    async_run_service,
    chatops_service,
    memory_service,
    notification_service,
    policy_service,
    state_service,
    token_service,
)
from hivepilot.services.metrics import registry, run_duration_seconds
from hivepilot.ui.plugin_persist import persist_plugins_disabled
from hivepilot.utils.validation import MAX_PROMPT_LEN, check_prompt_injection, sanitize_prompt

app = FastAPI(
    title="HivePilot API",
    version="0.2.0",
    root_path=settings.api_root_path,
)

_allowed_origins = settings.api_allowed_origins or ["http://localhost", "http://127.0.0.1"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials="*" not in _allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)

# Trust X-Forwarded-For / X-Forwarded-Proto from reverse proxies (nginx, caddy, traefik)
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # noqa: E402

app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")


# -- Distributed tracing (Phase 18) -----------------------------------------
@app.on_event("startup")
async def _init_tracing() -> None:
    """Wire up OpenTelemetry tracing (opt-in, no-op unless
    `HIVEPILOT_ENABLE_TRACING=1` + the `tracing` extra is installed) once
    the API server process actually starts serving — this is "a run
    begins" for the API entry point (mirrors the CLI's `run-pipeline`
    command and the scheduler daemon's `run()`)."""
    from hivepilot.observability.tracing import init_tracing

    init_tracing(settings)


# -- Body size limit (Phase 14b) -------------------------------------------
_MAX_BODY_BYTES = getattr(settings, "api_max_body_size", 1_048_576)  # 1 MB default


@app.middleware("http")
async def body_size_limit(request: Request, call_next):
    if request.method == "POST":
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > _MAX_BODY_BYTES:
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "Request body too large"}, status_code=413)
    return await call_next(request)


# -- X-Request-ID correlation middleware (Phase 14b) -------------------------
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# -- Rate limiter (Phase 14d: use X-Forwarded-For when behind proxy) ---------
_RATE_LIMIT = 20
_RATE_WINDOW = 60.0
_rate_lock = threading.Lock()
_rate_counts: dict[str, list[float]] = defaultdict(list)

_RATE_LIMITED_PATHS = {
    "/chatops/slack",
    "/chatops/discord",
    "/chatops/telegram",
    "/v1/chatops/slack",
    "/v1/chatops/discord",
    "/v1/chatops/telegram",
}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path in _RATE_LIMITED_PATHS:
        ip = _client_ip(request)
        now = time()
        with _rate_lock:
            window_start = now - _RATE_WINDOW
            _rate_counts[ip] = [t for t in _rate_counts[ip] if t > window_start]
            if len(_rate_counts[ip]) >= _RATE_LIMIT:
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
            _rate_counts[ip].append(now)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Lazy orchestrator singleton (Phase 14)
# ---------------------------------------------------------------------------
_orchestrator: Orchestrator | None = None
_orch_lock = threading.Lock()


def _get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        with _orch_lock:
            if _orchestrator is None:
                _orchestrator = Orchestrator()
    return _orchestrator


def require_role(required: str):
    async def dependency(request: Request, authorization: str = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
        token_value = authorization.split(" ", 1)[1]
        entry = token_service.resolve_token(token_value)
        if not entry:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        if token_service.role_rank(entry.role) < token_service.role_rank(required):
            state_service.record_audit(
                token_hash=entry.token[:16],
                role=entry.role,
                endpoint=request.url.path,
                method=request.method,
                result="forbidden",
                tenant=entry.tenant,
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        state_service.record_audit(
            token_hash=entry.token[:16],
            role=entry.role,
            endpoint=request.url.path,
            method=request.method,
            result="authorized",
            tenant=entry.tenant,
        )
        return entry

    return dependency


def _validate_extra_prompt(v: str | None) -> str | None:
    """Shared `extra_prompt` validation for run-triggering request bodies
    (`NewRunRequest` for async `POST /v1/runs`; originally shared with
    `RunRequest`, the request body of the now-removed synchronous
    `POST /run` -- see `RunRequest`'s own docstring, Phase 14b). A single
    helper -- not duplicated per model -- so any future sibling model
    applies byte-for-byte the same length check / sanitize / injection-
    detection behavior; extracted verbatim from `RunRequest`'s own prior
    validator with no behavior change.
    """
    if v is None:
        return None
    if len(v) > MAX_PROMPT_LEN:
        raise ValueError(
            f"extra_prompt exceeds maximum allowed length of {MAX_PROMPT_LEN} characters"
        )
    cleaned = sanitize_prompt(v)
    hits = check_prompt_injection(cleaned)
    if hits:
        from hivepilot.utils.logging import get_logger

        get_logger(__name__).warning("prompt_injection.detected", patterns=hits)
    return cleaned


class RunRequest(BaseModel):
    """Request body of the synchronous `POST /run` endpoint, **removed** in
    Phase 14b (see `docs/DASHBOARD.md`'s "Breaking change" note -- external
    callers must migrate to `POST /v1/runs` + `GET /v1/runs/{run_id}`).

    Kept (not deleted) solely because `test_async_runs_endpoint.py::
    test_extra_prompt_validation_shared_with_sync_run_request` uses it as a
    regression guard proving `NewRunRequest`'s `extra_prompt` handling never
    silently drifts from this original validator. `Orchestrator.run_task`
    (the in-process method this model's fields used to be forwarded to) is
    untouched and still used by the CLI/chatops in-process callers.
    """

    task: str
    projects: list[str]
    extra_prompt: str | None = None
    auto_git: bool = False

    @field_validator("extra_prompt", mode="before")
    @classmethod
    def validate_extra_prompt(cls, v: str | None) -> str | None:
        return _validate_extra_prompt(v)


class NewRunRequest(BaseModel):
    """Body for `POST /v1/runs` (Mirador actionable dashboard PRD, Sprint 3)
    -- the async, single-project successor to the removed sync `RunRequest`.
    Reuses `_validate_extra_prompt` (see above) so its `extra_prompt`
    handling is identical to `RunRequest`'s, never a weaker reimplementation."""

    task: str
    project: str
    extra_prompt: str | None = None
    auto_git: bool = False

    @field_validator("extra_prompt", mode="before")
    @classmethod
    def validate_extra_prompt(cls, v: str | None) -> str | None:
        return _validate_extra_prompt(v)


# ---------------------------------------------------------------------------
# /v1/ versioned router (Phase 14b)
# ---------------------------------------------------------------------------
v1 = APIRouter(prefix="/v1")


@v1.get("/health")
@app.get("/health")
def health():
    checks: dict[str, str] = {}

    try:
        state_service.list_recent_runs(limit=1)
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {exc}"

    try:
        orch = _get_orchestrator()
        runner_count = (
            len(orch.registry._definitions) if hasattr(orch.registry, "_definitions") else -1
        )
        checks["runners"] = f"ok ({runner_count} defined)" if runner_count >= 0 else "ok"
    except Exception:  # noqa: BLE001
        checks["runners"] = "error"

    for dep in ("langchain", "boto3", "docker", "telegram"):
        try:
            __import__(dep)
            checks[f"dep:{dep}"] = "available"
        except ImportError:
            checks[f"dep:{dep}"] = "not installed"

    overall = "ok" if checks["database"] == "ok" else "degraded"
    return {"status": overall, "checks": checks}


@v1.get("/healthz")
@app.get("/healthz")
def healthz():
    """Liveness probe — alias for /health returning a minimal ok payload."""
    return {"status": "ok"}


@v1.get("/readyz")
@app.get("/readyz")
async def readyz():
    """Readiness probe: checks DB and config reachability."""
    checks: dict[str, str] = {}

    # Check (a): state DB reachable
    try:
        state_service.init_db()
        checks["db"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["db"] = f"error: {exc}"

    # Check (b): core config loads
    try:
        from hivepilot.services.project_service import load_projects

        load_projects()
        checks["config"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["config"] = f"error: {exc}"

    failing = [k for k, v in checks.items() if v != "ok"]
    if failing:
        raise HTTPException(status_code=503, detail={"ready": False, "checks": checks})

    return {"ready": True, "checks": checks}


# ---------------------------------------------------------------------------
# Mirador actionable dashboard (PRD mirador-actionable-dashboard, Sprint 1)
# ---------------------------------------------------------------------------


@v1.get("/whoami")
@app.get("/whoami")
def whoami(caller: token_service.TokenEntry = Depends(require_role("read"))) -> dict[str, str]:
    """Let the caller introspect its own RBAC role/tenant.

    Gated at the lowest rank (`read`, the floor every valid token
    satisfies), so any authenticated caller can always resolve its own
    identity — this is what powers the Mirador web client's `useRole()`
    (`web/src/lib/role-context.tsx`), which fail-closed gates action
    controls app-wide (unknown/null role -> `can()` false for everything).

    Returns ONLY `{role, tenant}` — never the token hash, note, expiry, or
    any other `TokenEntry` field.
    """
    return {"role": caller.role, "tenant": caller.tenant}


@v1.get("/projects", dependencies=[Depends(require_role("read"))])
@app.get("/projects", dependencies=[Depends(require_role("read"))])
def list_projects():
    return _get_orchestrator().projects.projects


@v1.get("/tasks", dependencies=[Depends(require_role("read"))])
@app.get("/tasks", dependencies=[Depends(require_role("read"))])
def list_tasks():
    return list(_get_orchestrator().tasks.tasks.keys())


@v1.get("/runs")
@app.get("/runs")
def list_runs(caller: token_service.TokenEntry = Depends(require_role("run"))):
    """List runs, filtered to caller's tenant for non-admin roles."""
    if caller.role == "admin":
        return state_service.list_recent_runs()
    return state_service.list_recent_runs(tenant=caller.tenant)


# ---------------------------------------------------------------------------
# Mirador actionable dashboard PRD, Sprint 3 -- async run trigger.
#
# `POST /v1/runs` records a run row and returns its id immediately (202,
# <500ms); the pipeline itself executes on a background thread via
# `hivepilot.services.async_run_service.submit_run`. This is deliberately
# `/v1/runs` only (NOT dual-registered on `app` like every other endpoint in
# this file) -- a distinct HTTP method+path pairing from `GET /v1/runs`
# (`list_runs` above), so FastAPI dispatches by method with no route
# collision.
#
# **THE CRUX (exactly one run row per trigger, no dropped run-level gate):**
# `Orchestrator.run_task`/`_run_task_body` ALWAYS creates its own run row via
# `state_service.record_run_start` and drives it to terminal -- calling it
# here would create a SECOND row and leave the row THIS endpoint pre-creates
# stuck at whatever initial status it started with. Instead, this endpoint
# owns run-row creation itself (like `_run_task_body` owns it) and calls the
# same per-project execution primitive `_run_task_body` calls,
# `Orchestrator._execute_task`, which accepts a caller-supplied `run_id` and
# does NOT create a row.
#
# Because this endpoint owns row creation, it can pick the correct INITIAL
# status synchronously, before ever creating the row: `policy.
# require_approval` is a config-only check (no I/O), so `create_run` below
# evaluates it up front and creates the row with status "pending" instead of
# "running" when true -- mirroring `_run_task_body`'s
# `require_approval`-first branch (lines ~791-814) without ever needing to
# "downgrade" a running row afterward. The (potentially slow) CVE-gate scan
# and the run itself both happen in the background worker
# (`_run_async_task` below), which mirrors `_run_task_body`'s remaining
# if/elif/else branches (CVE gate at ~815-836, else-execute at ~837-853) in
# the same order, so no run-level gate is weaker than sync `POST /v1/run`.
# ---------------------------------------------------------------------------


class NewRunResponse(BaseModel):
    run_id: int
    status: str


def _run_async_task(
    *,
    orch: Orchestrator,
    run_id: int,
    project: Any,
    task_name: str,
    task: Any,
    extra_prompt: str | None,
    auto_git: bool,
    policy: Any,
) -> None:
    """The background work `POST /v1/runs` submits via `async_run_service.
    submit_run`. Mirrors `Orchestrator._run_task_body`'s per-project
    require_approval / CVE-gate / execute branches -- EXCEPT run-row
    creation, which the caller (`create_run` below) already owns. Drives
    `run_id` to a terminal status exactly once (or leaves it `pending` for
    an approval, mirroring `_run_task_body`'s own approval branch, which
    also never calls `complete_run`).

    Never surfaces raw exception text / `capture()` output to
    `state_service.complete_run`'s `detail` -- only a short, safe summary
    (exception TYPE name, never its message).
    """
    from hivepilot.orchestrator import RunCancelled, StepApprovalPending
    from hivepilot.services.config_provenance import redact_text
    from hivepilot.services.quota import QuotaDeferredError

    try:
        if policy.require_approval:
            approval_meta = {
                "task": task_name,
                "project": project.path.name,
                "extra_prompt": extra_prompt,
                "auto_git": auto_git,
            }
            state_service.record_approval_request(
                run_id, project.path.name, task_name, approval_meta
            )
            notification_service.send_approval_keyboard(
                run_id=run_id, project=project.path.name, task=task_name
            )
            return

        severity = policy.block_on_severity
        if severity:
            cve_block_detail = orch._cve_gate_block_detail(project, policy.scan_tool, severity)
            if cve_block_detail is not None:
                state_service.complete_run(run_id, "failed", cve_block_detail)
                notification_service.send_notification(
                    f"⛔ {project.path.name}: {task_name} blocked by CVE gate"
                )
                return

        try:
            from hivepilot.services.notion_service import on_run_start

            on_run_start(run_id=run_id, project=project.path.name, task=task_name)
        except Exception:  # noqa: BLE001
            pass
        notification_service.send_notification(f"Starting {task_name} on {project.path.name}")

        detail = orch._execute_task(
            project=project,
            task_name=task_name,
            task=task,
            extra_prompt=extra_prompt,
            auto_git=auto_git,
            run_id=run_id,
            policy=policy,
            simulate=False,
            dry_run=False,
        )
        detail = redact_text(detail) if detail else detail
        state_service.complete_run(run_id, "success", "run completed")
        notification_service.send_notification(f"✅ {project.path.name}: {task_name} completed")
    except StepApprovalPending:
        # A mid-task step-approval gate already recorded its own approval
        # request and left the run paused -- do NOT overwrite that status
        # (mirrors `_run_task_body`'s own StepApprovalPending handling).
        pass
    except RunCancelled:
        # The step loop already marked the run CANCELLED (+ finished_at) via
        # `state_service.complete_run` before raising -- mirrors
        # StepApprovalPending's "already recorded its own terminal state,
        # don't overwrite it" handling immediately above. Do NOT call
        # complete_run again -- the run must resolve to a terminal status
        # exactly once.
        pass
    except QuotaDeferredError:
        state_service.complete_run(run_id, "deferred")
    except Exception as exc:  # noqa: BLE001 -- never surface raw exception text
        state_service.complete_run(run_id, "failed", f"run failed: {type(exc).__name__}")


@v1.post("/runs", status_code=status.HTTP_202_ACCEPTED)
def create_run(
    body: NewRunRequest,
    caller: token_service.TokenEntry = Depends(require_role("run")),
) -> NewRunResponse:
    """Trigger a single-project run asynchronously. Returns immediately with
    `{run_id, status}` -- the pipeline executes on a background thread (see
    `_run_async_task` above). `caller.tenant` is recorded on the run row,
    exactly like `list_runs`/`pending_approvals` scope by it.
    """
    orch = _get_orchestrator()

    if body.task not in orch.tasks.tasks:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown task")
    task = orch.tasks.tasks[body.task]

    try:
        project = orch._project(body.project)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown project"
        ) from None

    try:
        policy = policy_service.enforce_policy(project.path.name, auto_git=body.auto_git)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    initial_status = "pending" if policy.require_approval else "running"
    run_id = state_service.record_run_start(
        project.path.name, body.task, status=initial_status, tenant=caller.tenant
    )

    def _work() -> None:
        _run_async_task(
            orch=orch,
            run_id=run_id,
            project=project,
            task_name=body.task,
            task=task,
            extra_prompt=body.extra_prompt,
            auto_git=body.auto_git,
            policy=policy,
        )

    async_run_service.submit_run(run_id, _work)
    return NewRunResponse(run_id=run_id, status=initial_status)


# ---------------------------------------------------------------------------
# Mirador actionable dashboard PRD, Sprint 4 -- stop/cancel an in-flight
# async run. `/v1`-only (like `POST /v1/runs` above), not dual-registered on
# `app` -- a distinct HTTP method+path pairing from every other route in
# this file, so FastAPI dispatches by method+path with no route collision.
#
# **FAIL-CLOSED IS THE WHOLE POINT (see INVARIANTS.md "Write Endpoints
# Fail-Closed" / "Async Run Handle"):** `async_run_service.request_cancel`
# is the single source of truth for "is this run actually cancellable right
# now" -- it returns `False` for an unknown run_id, a run that was never
# async, OR a run that's already reached a terminal status (popped from the
# in-flight registry by `submit_run`'s own `finally`). Every one of those
# maps to `409`, NEVER a false-success `202`. Tenant-checked EXACTLY like
# `POST /v1/approvals/{run_id}` (`handle_approval` above): 404 if the run
# row doesn't exist, 403 for a non-admin caller whose tenant doesn't match
# the run's tenant, admin bypasses the tenant check entirely.
# ---------------------------------------------------------------------------


class CancelRunResponse(BaseModel):
    run_id: int
    status: str


@v1.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_run(
    run_id: int,
    caller: token_service.TokenEntry = Depends(require_role("run")),
) -> CancelRunResponse:
    """Request cooperative cancellation of an in-flight async run. The run
    resolves to `RunStatus.CANCELLED` at its NEXT step boundary (see
    `Orchestrator._execute_task_body`'s step loop) -- this endpoint itself
    never blocks on that, it only flips the cooperative flag and returns.
    """
    row = state_service.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if caller.role != "admin":
        row_tenant = row.get("tenant", "default")
        if row_tenant != caller.tenant:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cross-tenant cancel not allowed",
            )
    if not async_run_service.request_cancel(run_id):
        # Unknown to the registry (never async, or already terminal) --
        # fail-closed: never report false success.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not cancellable")
    return CancelRunResponse(run_id=run_id, status="cancelling")


# ---------------------------------------------------------------------------
# Phase 14b -- result retrieval by id, the async family's missing piece.
# `POST /v1/runs` (above) returns a `run_id` immediately; this is how a
# caller polls it for status + step results. `/v1`-only, like `POST /v1/runs`
# and `POST /v1/runs/{run_id}/cancel` above -- not dual-registered on `app`.
#
# Gated at `run`, matching every other endpoint in this family --
# `GET /v1/runs` (list), `POST /v1/runs` (create), `POST /v1/runs/{run_id}/
# cancel` -- all require `run`. Run ids are a sequential autoincrement PK,
# so a lower `read` floor here would let a bare `read` token enumerate
# `id=1,2,3,...` and harvest every run's full step detail (which includes
# provider/model/token/cost fields the list endpoint doesn't expose) within
# its tenant -- "must already know the id" is not a real barrier against a
# sequential id space.
#
# Tenant-checked like `POST /v1/runs/{run_id}/cancel` (404 if the row
# doesn't exist, admin bypasses the tenant check) EXCEPT a tenant mismatch
# also reports 404 here, not 403 -- a GET must not let an unauthorized
# caller distinguish "wrong tenant" from "doesn't exist" (existence leak).
# ---------------------------------------------------------------------------


class RunStepDetail(BaseModel):
    step: str
    status: str
    detail: str | None = None
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    timestamp: str | None = None


class RunDetailResponse(BaseModel):
    run_id: int
    project: str
    task: str
    status: str
    detail: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    tenant: str = "default"
    steps: list[RunStepDetail]


@v1.get("/runs/{run_id}")
def get_run(
    run_id: int,
    caller: token_service.TokenEntry = Depends(require_role("run")),
) -> RunDetailResponse:
    """Fetch a single run's status + step results by id -- how a caller
    polls the `run_id` returned by `POST /v1/runs` to completion. Gated at
    `run`, matching `GET /v1/runs`/`POST /v1/runs`/`POST /v1/runs/{run_id}/
    cancel` (see module comment above for why a lower `read` floor isn't
    safe here).

    `detail`/step `detail` are returned exactly as persisted: `record_step`/
    `complete_run` already redact every registered secret VALUE before
    writing to the `steps`/`runs` tables (see `state_service`'s own
    docstrings), so there is no additional un-redacted surface to guard
    against here -- this endpoint never re-derives or re-fetches raw output.
    """
    row = state_service.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    row_tenant = row.get("tenant", "default")
    if caller.role != "admin" and row_tenant != caller.tenant:
        # Same status as "doesn't exist" -- never leak that a run exists in
        # another tenant.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    steps = state_service.get_steps_for_run(run_id)
    return RunDetailResponse(
        run_id=row["id"],
        project=row.get("project"),
        task=row.get("task"),
        status=row.get("status"),
        detail=row.get("detail"),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        tenant=row_tenant,
        steps=[
            RunStepDetail(
                step=step.get("step"),
                status=step.get("status"),
                detail=step.get("detail"),
                provider=step.get("provider"),
                model=step.get("model"),
                input_tokens=step.get("input_tokens"),
                output_tokens=step.get("output_tokens"),
                cost_usd=step.get("cost_usd"),
                timestamp=step.get("timestamp"),
            )
            for step in steps
        ],
    )


@v1.get("/approvals")
@app.get("/approvals")
def pending_approvals(caller: token_service.TokenEntry = Depends(require_role("run"))):
    """List pending approvals, filtered to caller's tenant for non-admin roles."""
    if caller.role == "admin":
        return state_service.get_pending_approvals()
    return state_service.get_pending_approvals(tenant=caller.tenant)


class ApprovalAction(BaseModel):
    approver: str = "api"
    approve: bool = True
    reason: str | None = None


@v1.post("/approvals/{run_id}")
@app.post("/approvals/{run_id}")
def handle_approval(
    run_id: int,
    action: ApprovalAction,
    caller: token_service.TokenEntry = Depends(require_role("approve")),
):
    """Approve/deny a run. Non-admin callers may only act on their own tenant's runs."""
    if caller.role != "admin":
        row = state_service.get_approval(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Approval not found")
        row_tenant = row.get("tenant", "default")
        if row_tenant != caller.tenant:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cross-tenant approval not allowed",
            )
    with run_duration_seconds.time():
        result = _get_orchestrator().run_approved(
            run_id=run_id,
            approve=action.approve,
            approver=action.approver,
            reason=action.reason,
        )
    return {"result": result.__dict__}


# ---------------------------------------------------------------------------
# Analytics (Phase 24a) — read-only aggregates over the run store.
# Every endpoint: Depends(require_role("read")), tenant-filtered from the
# caller's token (admin: unfiltered, mirrors GET /runs / GET /approvals).
# ---------------------------------------------------------------------------


def _analytics_tenant(caller: token_service.TokenEntry) -> str | None:
    return None if caller.role == "admin" else caller.tenant


# CSV/formula-injection defense-in-depth: Excel, Google Sheets, and
# LibreOffice all execute a cell as a formula if it starts with one of these
# characters when the CSV is opened. project/task names aren't attacker-
# reachable today (validated against config before a run can exist), but
# this is user-facing exported data, so guard it anyway.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def _csv_safe(value: Any) -> Any:
    """Prefix string cells that start with a formula-trigger character with
    a single quote — the standard CSV-injection mitigation. Spreadsheet
    apps then render the leading quote as plain text instead of evaluating
    a formula; csv.reader consumers see the literal `'`-prefixed string.
    Non-string (numeric) cells pass through untouched.
    """
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def _csv_response(rows: list[dict[str, Any]], fieldnames: list[str]) -> Response:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_safe(value) for key, value in row.items()})
    return Response(content=buf.getvalue(), media_type="text/csv")


def _pdf_safe(value: Any) -> str:
    """Render an analytics cell as plain text for the PDF table. PDFs don't
    execute cell content as formulas the way spreadsheets do, so — unlike
    `_csv_safe` — no formula-prefix escaping is needed here; this only
    normalizes `None` the same way `csv.DictWriter` would (empty string).

    fpdf2's built-in core fonts (Helvetica, etc.) only support latin-1 —
    project/task names and provider/model names (the latter sourced from
    LLM APIs) are not guaranteed to be latin-1. Encoding with
    errors="replace" swaps any non-representable character for `?` instead
    of letting `table()` raise `FPDFUnicodeEncodingException`/
    `UnicodeEncodeError`, which would otherwise surface as an uncaught 500.
    """
    if value is None:
        return ""
    return str(value).encode("latin-1", "replace").decode("latin-1")


# Common install locations for a Unicode-capable TTF, checked (in order,
# after `settings.pdf_font_path`) when no explicit font path is configured.
# Debian/Ubuntu: `apt install fonts-dejavu`; Alpine: `apk add ttf-dejavu`.
_COMMON_UNICODE_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
)


def _resolve_unicode_font_path() -> str | None:
    """Return a usable Unicode TTF path, or `None` if none can be found.

    Checked in order: (1) `settings.pdf_font_path`
    (`HIVEPILOT_PDF_FONT_PATH`) if set and the file exists; (2) a small list
    of common system font install paths. Never raises -- an unreadable
    configured path or a filesystem error just falls through to `None`, so
    the caller can gracefully degrade to the latin-1-only core font instead
    of ever 500ing on a font-lookup failure.
    """
    try:
        if settings.pdf_font_path and Path(settings.pdf_font_path).is_file():
            return settings.pdf_font_path
        for candidate in _COMMON_UNICODE_FONT_PATHS:
            if Path(candidate).is_file():
                return candidate
    except OSError:
        return None
    return None


def _render_pdf_bytes(
    rows: list[dict[str, Any]],
    title: str,
    columns: list[str],
    *,
    unicode_font_path: str | None,
) -> bytes:
    """Build the actual PDF bytes for `_pdf_response`, either via a Unicode
    TTF (`unicode_font_path` set) or the latin-1-only Helvetica core font
    (`unicode_font_path` is `None`). Split out so `_pdf_response` can retry
    with the latin-1 path if the Unicode path raises for ANY reason at
    render time -- see `_pdf_response`'s docstring for why render-time
    failures (not just font-*load* failures) must also degrade gracefully.
    """
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    pdf = FPDF(orientation="L")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    table_kwargs: dict[str, Any] = {}
    if unicode_font_path:
        pdf.add_font("HivePilotUni", fname=unicode_font_path)
        body_family = "HivePilotUni"
        cell_text = lambda v: str(v) if v is not None else ""  # noqa: E731
        # Only the regular (non-bold) style of the Unicode font is
        # registered above -- fpdf2's table() defaults to a bold heading
        # row, which would raise if asked to use a family with no bold
        # variant loaded. Disable the bold emphasis on headings for the
        # Unicode path only; the Helvetica/latin-1 fallback keeps its
        # original (bold-heading) look.
        table_kwargs["headings_style"] = FontFace(emphasis=None)
    else:
        body_family = "Helvetica"
        cell_text = _pdf_safe

    pdf.set_font(body_family, size=14)
    pdf.cell(0, 10, cell_text(title), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(body_family, size=9)
    with pdf.table(**table_kwargs) as table:
        header_row = table.row()
        for column in columns:
            header_row.cell(cell_text(column))
        for row in rows:
            data_row = table.row()
            for column in columns:
                data_row.cell(cell_text(row.get(column)))
    return bytes(pdf.output())


def _pdf_response(rows: list[dict[str, Any]], title: str, columns: list[str]) -> Response:
    """Render `rows`/`columns` (the same shape `_csv_response` consumes) as a
    simple tabular PDF. fpdf2 is an OPTIONAL extra (`pip install
    hivepilot[pdf]`) — lazy-imported here so the core API never depends on
    it. If it's missing, fail gracefully with a clear message instead of a
    500/traceback.

    Unicode rendering: fpdf2's built-in core fonts (Helvetica, etc.) only
    support latin-1, so non-latin project/task/provider names degrade to
    `?` (see `_pdf_safe`). When a Unicode TTF is available (see
    `_resolve_unicode_font_path`), it's registered and used instead, and
    cell text is passed through WITHOUT the latin-1 replace so non-latin
    characters render correctly.

    Never 500s on EITHER font *load* or *render*: glyph coverage varies per
    TTF (e.g. DejaVu Sans has no emoji/most CJK), and `Row.cell()` only
    queues text -- the actual glyph lookup happens later, inside the `with
    pdf.table()` block, when `table.render()` runs. So `_render_pdf_bytes`
    is wrapped in its own try/except here (not just the `add_font` call):
    ANY failure building the Unicode-font PDF -- font-load OR a render-time
    error for an out-of-coverage codepoint -- discards that attempt and
    rebuilds the WHOLE PDF via the Helvetica/latin-1 path instead, which
    can only ever render already-latin-1-safe text (see `_pdf_safe`) and so
    cannot itself raise the same way.
    """
    try:
        from fpdf import FPDF  # noqa: F401 -- import-availability probe only
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="PDF export requires the 'pdf' extra: pip install hivepilot[pdf]",
        ) from exc

    font_path = _resolve_unicode_font_path()
    pdf_bytes: bytes | None = None
    if font_path:
        try:
            pdf_bytes = _render_pdf_bytes(rows, title, columns, unicode_font_path=font_path)
        except Exception:  # noqa: BLE001 -- any Unicode font-load/render failure -> latin-1 fallback
            pdf_bytes = None
    if pdf_bytes is None:
        pdf_bytes = _render_pdf_bytes(rows, title, columns, unicode_font_path=None)

    filename = title.lower().replace(" ", "_").replace("/", "_") + ".pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_SUMMARY_CSV_FIELDS = ["scope", "key", "total", "succeeded", "failed", "skipped", "other"]
_TRENDS_CSV_FIELDS = ["bucket", "total", "succeeded", "failed", "skipped", "other"]
_DURATIONS_CSV_FIELDS = ["scope", "key", "count", "min", "max", "avg", "p50", "p95", "p99"]
_HOTSPOTS_CSV_FIELDS = ["step", "status", "count"]
_APPROVAL_LATENCY_CSV_FIELDS = ["count", "min", "max", "avg", "p50", "p95", "p99"]
_PROVIDERS_CSV_FIELDS = ["scope", "key", "total", "succeeded", "failed", "skipped", "other"]
_COST_CSV_FIELDS = [
    "scope",
    "key",
    "total_steps",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "unpriced_steps",
]


@v1.get("/analytics/summary")
@app.get("/analytics/summary")
def analytics_summary(
    days: int = 30,
    project: str | None = None,
    task: str | None = None,
    format: str | None = None,
    caller: token_service.TokenEntry = Depends(require_role("read")),
):
    data = analytics_service.run_summary(
        tenant=_analytics_tenant(caller), days=days, project=project, task=task
    )
    if format in ("csv", "pdf"):
        rows: list[dict[str, Any]] = [
            {"scope": "overall", "key": "", "total": data["total"], **data["outcomes"]}
        ]
        for key, val in data["by_project"].items():
            rows.append({"scope": "project", "key": key, "total": val["total"], **val["outcomes"]})
        for key, val in data["by_task"].items():
            rows.append({"scope": "task", "key": key, "total": val["total"], **val["outcomes"]})
        if format == "csv":
            return _csv_response(rows, _SUMMARY_CSV_FIELDS)
        return _pdf_response(rows, "Analytics Summary", _SUMMARY_CSV_FIELDS)
    return data


@v1.get("/analytics/trends")
@app.get("/analytics/trends")
def analytics_trends(
    days: int = 30,
    project: str | None = None,
    task: str | None = None,
    bucket: str = "day",
    format: str | None = None,
    caller: token_service.TokenEntry = Depends(require_role("read")),
):
    if bucket not in ("day", "week"):
        raise HTTPException(status_code=400, detail="bucket must be 'day' or 'week'")
    data = analytics_service.run_trends(
        tenant=_analytics_tenant(caller), days=days, project=project, task=task, bucket=bucket
    )
    if format in ("csv", "pdf"):
        rows = [
            {"bucket": row["bucket"], "total": row["total"], **row["outcomes"]}
            for row in data["series"]
        ]
        if format == "csv":
            return _csv_response(rows, _TRENDS_CSV_FIELDS)
        return _pdf_response(rows, "Analytics Trends", _TRENDS_CSV_FIELDS)
    return data


@v1.get("/analytics/durations")
@app.get("/analytics/durations")
def analytics_durations(
    days: int = 30,
    project: str | None = None,
    task: str | None = None,
    format: str | None = None,
    caller: token_service.TokenEntry = Depends(require_role("read")),
):
    data = analytics_service.run_durations(
        tenant=_analytics_tenant(caller), days=days, project=project, task=task
    )
    if format in ("csv", "pdf"):
        rows = [{"scope": "overall", "key": "", **data["overall"]}]
        for key, stats in data["by_project"].items():
            rows.append({"scope": "project", "key": key, **stats})
        for key, stats in data["by_task"].items():
            rows.append({"scope": "task", "key": key, **stats})
        if format == "csv":
            return _csv_response(rows, _DURATIONS_CSV_FIELDS)
        return _pdf_response(rows, "Analytics Durations", _DURATIONS_CSV_FIELDS)
    return data


@v1.get("/analytics/steps/failures")
@app.get("/analytics/steps/failures")
def analytics_step_failures(
    days: int = 30,
    project: str | None = None,
    task: str | None = None,
    limit: int = 20,
    format: str | None = None,
    caller: token_service.TokenEntry = Depends(require_role("read")),
):
    hotspots = analytics_service.step_failure_hotspots(
        tenant=_analytics_tenant(caller), days=days, project=project, task=task, limit=limit
    )
    if format == "csv":
        return _csv_response(hotspots, _HOTSPOTS_CSV_FIELDS)
    if format == "pdf":
        return _pdf_response(hotspots, "Step Failure Hotspots", _HOTSPOTS_CSV_FIELDS)
    return {"hotspots": hotspots}


@v1.get("/analytics/approvals/latency")
@app.get("/analytics/approvals/latency")
def analytics_approval_latency(
    days: int = 30,
    project: str | None = None,
    task: str | None = None,
    format: str | None = None,
    caller: token_service.TokenEntry = Depends(require_role("read")),
):
    data = analytics_service.approval_latency(
        tenant=_analytics_tenant(caller), days=days, project=project, task=task
    )
    if format == "csv":
        return _csv_response([data], _APPROVAL_LATENCY_CSV_FIELDS)
    if format == "pdf":
        return _pdf_response([data], "Approval Latency", _APPROVAL_LATENCY_CSV_FIELDS)
    return data


@v1.get("/analytics/providers")
@app.get("/analytics/providers")
def analytics_providers(
    days: int = 30,
    project: str | None = None,
    task: str | None = None,
    format: str | None = None,
    caller: token_service.TokenEntry = Depends(require_role("read")),
):
    """Phase 24b.1 — provider/model breakdown analytics: `steps` grouped by
    provider (runner kind / resolved API provider) and by model, with
    counts + outcome split. Token/cost analytics are a later sub-sprint
    (24b.2) — this endpoint only reflects what's persisted per step today.
    """
    by_provider = analytics_service.steps_by_provider(
        tenant=_analytics_tenant(caller), days=days, project=project, task=task
    )
    by_model = analytics_service.steps_by_model(
        tenant=_analytics_tenant(caller), days=days, project=project, task=task
    )
    if format in ("csv", "pdf"):
        rows: list[dict[str, Any]] = [
            {"scope": "provider", "key": row["provider"], "total": row["total"], **row["outcomes"]}
            for row in by_provider
        ] + [
            {"scope": "model", "key": row["model"], "total": row["total"], **row["outcomes"]}
            for row in by_model
        ]
        if format == "csv":
            return _csv_response(rows, _PROVIDERS_CSV_FIELDS)
        return _pdf_response(rows, "Analytics Providers", _PROVIDERS_CSV_FIELDS)
    return {"by_provider": by_provider, "by_model": by_model}


@v1.get("/analytics/cost")
@app.get("/analytics/cost")
def analytics_cost(
    days: int = 30,
    project: str | None = None,
    task: str | None = None,
    format: str | None = None,
    caller: token_service.TokenEntry = Depends(require_role("read")),
):
    """Phase 24b.2b — cost/provider analytics: token + cost totals, overall
    and grouped by `provider`/`model`. Effective cost per step is the
    self-reported `cost_usd` when present, else an estimate from the price
    map (`hivepilot.services.pricing`), else the step contributes 0 to the
    cost total and is counted in `unpriced_steps` — never silently presented
    as a complete total. Closes Phase 24 (analytics API).
    """
    data = analytics_service.cost_summary(
        tenant=_analytics_tenant(caller), days=days, project=project, task=task
    )
    if format in ("csv", "pdf"):
        rows: list[dict[str, Any]] = [{"scope": "overall", "key": "", **data["overall"]}]
        rows += [
            {
                "scope": "provider",
                "key": row["provider"],
                **{k: v for k, v in row.items() if k != "provider"},
            }
            for row in data["by_provider"]
        ]
        rows += [
            {
                "scope": "model",
                "key": row["model"],
                **{k: v for k, v in row.items() if k != "model"},
            }
            for row in data["by_model"]
        ]
        if format == "csv":
            return _csv_response(rows, _COST_CSV_FIELDS)
        return _pdf_response(rows, "Analytics Cost", _COST_CSV_FIELDS)
    return data


# ---------------------------------------------------------------------------
# Memory-quality instrumentation subsystem — backs Mirador's "Réalité" view.
# Sibling to the analytics endpoints above, same shape: every GET endpoint
# Depends(require_role("read")), tenant-filtered from the caller's token via
# `_memory_tenant` (mirrors `_analytics_tenant`: admin -> unscoped/`None`,
# every other caller -> their own tenant, NEVER leaking cross-tenant rows —
# see `memory_service.py`'s own tenant-scoping docstring). The one write
# endpoint, `POST /v1/memory/evaluations`, requires the higher `"run"` rank
# (mirrors `POST /v1/runs`'s gate — this is a mutation, not a read) and
# ALWAYS records for the caller's own token tenant (`caller.tenant`, never
# `_memory_tenant`'s admin-unscoped `None`) — a caller can never write into
# another tenant's evaluation log, regardless of role.
#
# Everything here is OPT-IN and additive: when nothing has been instrumented
# (no plugin calls `memory_service.record_*` — see `plugins/mem0.py`'s
# `recall`/`store` wiring), the underlying tables stay empty and every
# endpoint below returns zeros/`[]` — NEVER fabricated data.
# ---------------------------------------------------------------------------


def _memory_tenant(caller: token_service.TokenEntry) -> str | None:
    return None if caller.role == "admin" else caller.tenant


@v1.get("/memory/reality")
@app.get("/memory/reality")
def memory_reality(
    days: int = 30,
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    return memory_service.reality_summary(tenant=_memory_tenant(caller), days=days)


@v1.get("/memory/gaps")
@app.get("/memory/gaps")
def memory_gaps(
    days: int = 30,
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    return {"gaps": memory_service.gaps_by_namespace(tenant=_memory_tenant(caller), days=days)}


@v1.get("/memory/evaluations")
@app.get("/memory/evaluations")
def list_memory_evaluations(
    limit: int = 50,
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    return {
        "evaluations": memory_service.recent_evaluations(tenant=_memory_tenant(caller), limit=limit)
    }


@v1.get("/memory/journal")
@app.get("/memory/journal")
def memory_journal(
    limit: int = 50,
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    return {"journal": memory_service.activity_journal(tenant=_memory_tenant(caller), limit=limit)}


class MemoryEvaluationRequest(BaseModel):
    namespace: str
    useful: bool
    ref_key: str | None = None
    note: str | None = None

    @field_validator("namespace")
    @classmethod
    def _namespace_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("namespace must not be empty")
        return v


@v1.post("/memory/evaluations")
@app.post("/memory/evaluations")
def record_memory_evaluation(
    body: MemoryEvaluationRequest,
    caller: token_service.TokenEntry = Depends(require_role("run")),
) -> dict[str, Any]:
    """Record a human evaluation ("was this memory useful?"). ALWAYS
    recorded for the caller's own token tenant (`caller.tenant`) — there is
    no `tenant` field on the request body, so a caller can never write into
    another tenant's evaluation log. `actor` is the best available real
    identity signal off the token (`caller.note`, the token's free-text
    label — see `hivepilot cli.py`'s `token add --note`), falling back to
    the token's `role` when no note was set; never fabricated.
    """
    memory_service.record_evaluation(
        namespace=body.namespace,
        useful=body.useful,
        ref_key=body.ref_key,
        note=body.note,
        actor=caller.note or caller.role,
        tenant=caller.tenant,
    )
    return {"recorded": True}


# ---------------------------------------------------------------------------
# Mirador web UI surface (Sprint 1) — plugin health + mem0 memory search.
# Both are read-only. Sibling to the analytics endpoints above, but NEITHER
# is tenant-scoped: plugin health is process-global state (no per-tenant
# concept applies), and mem0 memories have no tenant->project mapping to
# filter by (see `list_memories`'s docstring for the full scope analysis).
# ---------------------------------------------------------------------------


@v1.get("/plugins/health", dependencies=[Depends(require_role("read"))])
@app.get("/plugins/health", dependencies=[Depends(require_role("read"))])
def plugins_health_endpoint() -> dict[str, Any]:
    """Plugin health, mirroring the `plugins health` CLI's
    `PluginManager.check_all()` call (see `hivepilot/cli.py`
    `_print_health_table`). Health is process-global plugin state (NOT
    tenant-partitioned, unlike the analytics endpoints above) — every `read`
    token sees the same result, exactly like `GET /v1/tasks`/`GET
    /v1/projects`. `check_all()` never raises (`hivepilot/plugins.py`
    `PluginManager.run_health_check` catches per-check exceptions itself and
    normalizes them to `HealthStatus("error", ...)`), so this endpoint can't
    500 on a bad check. `HealthStatus.detail` is either the plugin author's
    own hand-written status string, which is documented (Phase 19
    discipline, `hivepilot/plugins.py`) to never contain a secret/token
    value — only presence/mode booleans — or, when a check raises
    unexpectedly, only the exception's type name (never the exception
    message, which is logged server-side instead). No additional redaction
    is needed here.
    """
    results = _get_orchestrator().plugins.check_all()
    return {
        "plugins": [
            {"name": name, "status": health.status, "detail": health.detail}
            for name, health in sorted(results.items())
        ],
        "disabled": sorted(settings.plugins_disabled),
    }


# ---------------------------------------------------------------------------
# Mirador actionable dashboard PRD, Sprint 5 -- POST /v1/plugins/{name}/toggle
# (admin-only). Enable/disable a plugin from the web Health tab by upserting
# `HIVEPILOT_PLUGINS_DISABLED` in the `.env` file `Settings` reads from (see
# `hivepilot.ui.plugin_persist.persist_plugins_disabled`, reused as-is --
# this endpoint only inlines the flip logic `PluginManagerApp.toggle_selected`
# already established for the TUI's `space` binding, it never imports the
# Textual app class itself).
#
# **Allowlist = UNION of `check_all()` (currently-registered/enabled
# plugins) and `settings.plugins_disabled` (currently-disabled plugins).**
# `check_all()` alone only lists ENABLED plugins -- a disabled plugin is
# never registered in the first place, so it never appears there. Using
# `check_all()` alone would make an already-disabled plugin permanently
# un-re-enableable via this endpoint (a fail-closed 404 on the very request
# meant to undo it). The union is therefore REQUIRED, not a convenience.
#
# **Fail-closed on an unknown name:** a name outside the union raises 404
# BEFORE `persist_plugins_disabled` is ever called -- an invariant this
# module's own tests assert on directly (a spied `persist_plugins_disabled`
# must see `call_count == 0` for an unknown name). No `.env` write ever
# happens for an unvalidated plugin name.
#
# **Concurrency:** `_plugin_toggle_lock` serializes the read-flip-persist
# sequence below -- this is a core state-changing path (like
# `_rate_lock`/`_orch_lock` above), so two concurrent toggles must not race
# and silently lose one caller's write (last-writer-wins on the in-memory
# read is fine; losing a write entirely is not).
#
# **No live reload.** `PluginManager` only scans/registers plugins once, at
# `Orchestrator()` construction (see `hivepilot/ui/plugin_manager.py`'s
# module docstring) -- this endpoint's effect is visible only after the API
# process is restarted. The response's `restart_required: true` field and
# the web UI's own copy make this explicit; there is no code path here that
# could accidentally suggest otherwise.
# ---------------------------------------------------------------------------

_plugin_toggle_lock = threading.Lock()


class PluginToggleResponse(BaseModel):
    name: str
    disabled: bool
    restart_required: bool


@v1.post("/plugins/{name}/toggle")
@app.post("/plugins/{name}/toggle")
def toggle_plugin_endpoint(
    name: str,
    caller: token_service.TokenEntry = Depends(require_role("admin")),
) -> PluginToggleResponse:
    """Enable/disable a plugin (effective on next restart only). See the
    module-level comment block just above for the allowlist-union,
    fail-closed, and concurrency rationale.
    """
    known = set(_get_orchestrator().plugins.check_all().keys()) | set(settings.plugins_disabled)
    if name not in known:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown plugin")

    with _plugin_toggle_lock:
        current = set(settings.plugins_disabled)
        if name in current:
            current.discard(name)
        else:
            current.add(name)
        updated = sorted(current)
        # Persist to .env FIRST; only mutate in-memory settings once the write
        # succeeds. Otherwise a failing persist (permission/disk) would leave
        # settings.plugins_disabled diverged from .env, and a later toggle would
        # compute `current` from the corrupted in-memory value (code-review S5).
        persist_plugins_disabled(updated)
        settings.plugins_disabled = updated
        disabled = name in current

    return PluginToggleResponse(name=name, disabled=disabled, restart_required=True)


# ---------------------------------------------------------------------------
# Config hot-reload (Phase 14c, #249)
# ---------------------------------------------------------------------------
# Makes `config sync`'d roles.yaml / projects.yaml / tasks.yaml / pipelines.yaml
# changes take effect in a running `api serve` process WITHOUT a restart.
# Fail-closed: `roles.refresh_roles()` and `Orchestrator.refresh()` each
# stage-then-commit internally (see their own docstrings) -- a broken config
# file on disk never corrupts the live process, it just keeps serving the
# previous good config and reports `False` for that half of the reload.
#
# `_orch_lock` -- the SAME lock `_get_orchestrator()` uses to guard the
# lazy-singleton double-checked-lock construction -- is reused here so a
# reload can never race that construction (e.g. the very first request that
# triggers `Orchestrator()` construction, concurrent with an admin calling
# reload). Holding it around `orch.refresh()` also prevents two concurrent
# `POST /v1/admin/reload` calls from interleaving their staging passes on
# the same Orchestrator instance. It does NOT block already-in-flight
# requests reading `orch.tasks`/`orch.projects`/`orch.pipelines` -- those
# attribute reads are unguarded (as they always have been) and, per
# `Orchestrator.refresh()`'s own atomicity guarantee, see either the fully
# old or fully new object, never a torn read.
class ReloadResponse(BaseModel):
    roles_reloaded: bool
    config_reloaded: bool


@v1.post("/admin/reload")
@app.post("/admin/reload")
def admin_reload_endpoint(
    caller: token_service.TokenEntry = Depends(require_role("admin")),
) -> ReloadResponse:
    """Hot-reload roles.yaml + projects/tasks/pipelines into this running API
    process. See the module-level comment block just above for the
    fail-closed and concurrency rationale. Also reachable via `hivepilot
    reload` (CLI) and, for the scheduler daemon, `SIGHUP`.
    """
    with _orch_lock:
        roles_reloaded = roles.refresh_roles()
        config_reloaded = _get_orchestrator().refresh()
    return ReloadResponse(roles_reloaded=roles_reloaded, config_reloaded=config_reloaded)


def _get_mem0_client() -> Any | None:
    """Build a mem0 client from Settings — mirrors `plugins/mem0.py`'s
    `_get_client()` exactly (hosted `MemoryClient` when
    `settings.mem0_api_key` is set, else self-host `Memory()` /
    `Memory.from_config()`). Duplicated here rather than importing
    `plugins/mem0.py` directly: `plugins/` is a user-editable, optional
    directory (an operator may delete or replace any file in it, and it's
    loaded via `importlib.util.spec_from_file_location`, not a stable
    package import), so the core API must not depend on that specific file
    being present. Never raises: any construction failure (library absent,
    bad config, network error on hosted init) degrades to `None` — the same
    graceful-degradation contract the plugin itself has.
    """
    if not settings.mem0_enabled:
        return None
    try:
        from mem0 import Memory, MemoryClient
    except ImportError:  # mem0ai is optional — never a hivepilot dependency
        return None
    try:
        if settings.mem0_api_key:
            return MemoryClient(api_key=settings.mem0_api_key)
        config = settings.mem0_config
        return Memory.from_config(config) if config else Memory()
    except Exception as exc:  # noqa: BLE001 — must never crash the endpoint
        from hivepilot.utils.logging import get_logger

        get_logger(__name__).warning("api.memories.client_init_failed", error=str(exc))
        return None


def _extract_memory_items(results: Any) -> list[dict[str, Any]]:
    """Best-effort normalization of a mem0 `search()` result into plain dicts.

    Tolerant of mem0's known response shapes (a bare list of dicts/strings,
    or `{"results": [...]}` / `{"memories": [...]}` — mirrors
    `plugins/mem0.py`'s `_extract_memory_texts`) but keeps the full item
    (`id`/`metadata`/`score`) rather than just the text, since the Mirador
    Mem0 view needs the structured PROVENANCE metadata (`project`/`task`/
    `role`/`category`/`ts` — see `plugins/mem0.py`'s `_provenance_metadata`)
    to render/filter, not just the memory string. Degrades to an empty list
    for any unrecognized shape rather than raising.
    """
    if results is None:
        return []
    items: Any = results
    if isinstance(results, dict):
        items = results.get("results", results.get("memories", []))
    if not isinstance(items, list):
        return []
    extracted: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            if item:
                extracted.append({"memory": item})
            continue
        if not isinstance(item, dict):
            continue
        text = item.get("memory") or item.get("text") or item.get("content")
        if not isinstance(text, str) or not text:
            continue
        entry: dict[str, Any] = {"memory": text}
        if "id" in item:
            entry["id"] = item["id"]
        if isinstance(item.get("metadata"), dict):
            entry["metadata"] = item["metadata"]
        if "score" in item:
            entry["score"] = item["score"]
        extracted.append(entry)
    return extracted


@v1.get("/memories", dependencies=[Depends(require_role("admin"))])
@app.get("/memories", dependencies=[Depends(require_role("admin"))])
def list_memories(query: str, limit: int = 20) -> dict[str, Any]:
    """Mirador Mem0 view — semantic search proxy over mem0.

    **Scope/tenant safety (investigated, Sprint 1 — the key risk this
    endpoint carries).** mem0 memories carry `project`/`task`/`role`
    PROVENANCE metadata (`plugins/mem0.py` `_provenance_metadata`, added in
    PR #143) but the mem0 store itself is NOT partitioned by HivePilot
    `tenant`: nothing in this repo maps a `tenant` to the set of `project`s
    it may see — `hivepilot.models.ProjectConfig` has no `tenant` field at
    all, and `tenant` only exists on `TokenEntry` / DB rows written by
    `state_service` (used to scope *runs*, not project ownership). Filtering
    returned memories to "the caller's tenant's projects" is therefore NOT
    cleanly derivable without inventing a tenant->project mapping that
    doesn't exist anywhere else in the codebase — doing that here, ad hoc,
    would be worse than not shipping the feature (a fabricated, unverified
    trust boundary). So: this endpoint is gated behind
    `require_role("admin")` instead of `"read"` — the same role that already
    sees unfiltered data on every analytics endpoint (`_analytics_tenant`
    returns `None` for admin) and unfiltered `GET /runs` / `GET /approvals`.
    No non-admin token, regardless of its tenant, can call this endpoint at
    all — the most restrictive safe option available given the data model,
    and consistent with this file's existing tenant-scoping precedent.

    **Graceful degradation:** `mem0_enabled` off (the default), `mem0ai` not
    installed, or the client can't be built -> `200` with
    `{"configured": false, "memories": [], "detail": ...}`, never a 500 and
    never a stack trace. A `client.search()` failure degrades the same way.
    """
    limit = max(1, min(limit, 100))
    client = _get_mem0_client()
    if client is None:
        return {
            "configured": False,
            "memories": [],
            "detail": "mem0 not configured (mem0_enabled is off, mem0ai isn't "
            "installed, or the mem0 client could not be built)",
        }

    try:
        results = client.search(query, limit=limit)
    except Exception as exc:  # noqa: BLE001 — a mem0 client failure must never 500
        from hivepilot.utils.logging import get_logger

        get_logger(__name__).warning("api.memories.search_failed", error=str(exc))
        return {
            "configured": False,
            "memories": [],
            "detail": "mem0 search failed",
        }

    memories = _extract_memory_items(results)[:limit]
    return {"configured": True, "memories": memories}


# ---------------------------------------------------------------------------
# Mirador web UI surface (Sprint 3) — plugin panels. Read-only, sibling to
# the plugin-health/mem0 endpoints above.
# ---------------------------------------------------------------------------


@v1.get("/panels", dependencies=[Depends(require_role("read"))])
@app.get("/panels", dependencies=[Depends(require_role("read"))])
def list_panels_endpoint() -> dict[str, Any]:
    """Every registered Mirador panel (name/title/min_role), mirroring the
    TUI's own panel listing (Sprint 2, `hivepilot/ui/dashboard.py`). Panel
    name/title/`min_role` are plugin CONFIGURATION, not secret — every
    `read` token sees the full panel list regardless of its own role. A
    panel's `min_role` only gates *fetching that panel's data*
    (`get_panel_endpoint` below), not whether it appears in this list.
    Never raises: `PluginManager.list_panels()` only reads its own
    in-memory dict.
    """
    panels = _get_orchestrator().plugins.list_panels()
    return {
        "panels": [
            {"name": p["name"], "title": p["title"], "min_role": p.get("min_role", "read")}
            for p in panels
        ]
    }


@v1.get("/panels/{name}")
@app.get("/panels/{name}")
def get_panel_endpoint(
    name: str, caller: token_service.TokenEntry = Depends(require_role("read"))
) -> dict[str, Any]:
    """A single panel's data. Unlike every other endpoint in this file, the
    required role is DATA-DEPENDENT: the panel itself declares its own
    `min_role` (default "read" — see `hivepilot/plugins.py` `PanelSpec`), so
    it cannot be expressed as a static `Depends(require_role(...))`.
    Instead, `Depends(require_role("read"))` above only enforces the floor
    (any authenticated token; 401 otherwise) — the panel's OWN `min_role` is
    enforced HERE, after the panel is resolved, using the same
    `token_service.role_rank` comparison `require_role` itself uses
    internally. A `read` token therefore gets 403 on a panel declaring
    `min_role: "admin"`, while an `admin` token gets 200 for the same panel.

    Unknown panel name -> 404. A raising/malformed `fetch()` -> 200 with a
    normalized error panel (exception TYPE name only, never the exception
    message — see `PluginManager.run_panel_fetch`), never a 500. No secret
    can appear in any response (panel names/titles are config; error detail
    is a type name only).

    **No framework-level tenant scoping.** Unlike `/v1/analytics/*` and
    `/v1/runs`, panel data has no `tenant` concept at this layer: a panel's
    `fetch()` returns whatever the plugin computes, entirely unfiltered.
    `min_role` is the ONLY access control this endpoint applies — a panel
    author is responsible for not exposing cross-tenant or otherwise
    sensitive data via a low-`min_role` panel.

    **Fail-closed on an invalid `min_role`.** `hivepilot/plugins.py`
    rejects a panel at registration time if its `min_role` is not a
    recognized role (`PanelInvalidMinRoleError`), but this endpoint ALSO
    treats a non-string/unrecognized `min_role` as the highest possible
    bar and denies every caller — defense in depth against
    `token_service.role_rank` returning `-1` for an unknown role, which
    would otherwise make the comparison below fail OPEN.
    """
    plugins = _get_orchestrator().plugins
    spec = plugins.get_panel(name)
    if spec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")

    min_role = spec.get("min_role", "read")
    min_role_rank = token_service.role_rank(min_role) if isinstance(min_role, str) else -1
    if min_role_rank < 0:
        # Defensive, belt-and-suspenders guard: `plugins.py`'s
        # `PanelInvalidMinRoleError` already refuses to REGISTER a panel
        # with an unrecognized/non-string `min_role`, so a real panel
        # should never reach this branch. But `token_service.role_rank`
        # returns -1 for ANY unrecognized role, and `role_rank(caller.role)
        # < role_rank(min_role)` would then be `0 < -1` — ALWAYS false —
        # which fails OPEN and serves the panel to any `read` token. Treat
        # an unknown/invalid `min_role` as the highest possible bar instead,
        # so this endpoint denies every caller rather than ever fail open.
        min_role_rank = max(token_service.ROLE_RANKS.values()) + 1
    if token_service.role_rank(caller.role) < min_role_rank:
        state_service.record_audit(
            token_hash=caller.token[:16],
            role=caller.role,
            endpoint=f"/v1/panels/{name}",
            method="GET",
            result="forbidden",
            tenant=caller.tenant,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role for this panel"
        )

    return dict(plugins.run_panel_fetch(name))


# ---------------------------------------------------------------------------
# Mirador Graph View (PRD mirador-graph-view, Sprint 1) — graph-native
# backend sources (plugins/roles/runners as nodes+edges today; future
# sprints add more). Read-only, sibling to the panel endpoints above. Dual-
# registered on both the /v1/* router and the bare app, exactly like
# /panels. `hivepilot.graph_sources` is imported here for its SIDE EFFECT
# (registering every built-in `GraphSourceSpec` — see
# `hivepilot/graph_sources/__init__.py`), mirroring how `hivepilot.webui`
# is imported below purely to be referenced by attribute.
# ---------------------------------------------------------------------------
from hivepilot import graph as graph_module  # noqa: E402
from hivepilot import graph_sources as _graph_sources  # noqa: E402,F401


def _graph_node_to_dict(node: graph_module.GraphNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "label": node.label,
        "kind": node.kind,
        "status": node.status,
        "group": node.group,
        "badges": list(node.badges),
        "meta": dict(node.meta),
    }


def _graph_edge_to_dict(edge: graph_module.GraphEdge) -> dict[str, Any]:
    return {"source": edge.source, "target": edge.target, "kind": edge.kind, "label": edge.label}


def _graph_data_to_dict(data: graph_module.GraphData) -> dict[str, Any]:
    return {
        "source": data.source,
        "nodes": [_graph_node_to_dict(n) for n in data.nodes],
        "edges": [_graph_edge_to_dict(e) for e in data.edges],
        "layout_hint": data.layout_hint,
    }


def _graph_detail_to_dict(detail: graph_module.GraphDetail) -> dict[str, Any]:
    return {"title": detail.title, "tags": list(detail.tags), "sections": list(detail.sections)}


def _resolve_graph_min_role_rank(min_role: str) -> int:
    """Fail-closed resolution of a graph source's declared `min_role`,
    mirroring `get_panel_endpoint`'s own defensive guard: a non-string or
    unrecognized `min_role` is treated as the HIGHEST possible bar (denies
    every caller) rather than letting `token_service.role_rank`'s `-1`
    sentinel invert the `<` comparison and fail open."""
    rank = token_service.role_rank(min_role) if isinstance(min_role, str) else -1
    if rank < 0:
        return max(token_service.ROLE_RANKS.values()) + 1
    return rank


def _enforce_graph_min_role(
    spec: graph_module.GraphSourceSpec, caller: token_service.TokenEntry, endpoint: str
) -> None:
    """Enforce *spec*'s own `min_role`, AFTER the source has already been
    resolved — the required role is DATA-DEPENDENT (mirrors
    `get_panel_endpoint`), so it cannot be expressed as a static
    `Depends(require_role(...))`. Raises 403 (with an audit record) on
    denial; returns normally when the caller satisfies the bar."""
    min_role_rank = _resolve_graph_min_role_rank(spec.min_role)
    if token_service.role_rank(caller.role) < min_role_rank:
        state_service.record_audit(
            token_hash=caller.token[:16],
            role=caller.role,
            endpoint=endpoint,
            method="GET",
            result="forbidden",
            tenant=caller.tenant,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role for this graph source"
        )


@v1.get("/graph/sources", dependencies=[Depends(require_role("read"))])
@app.get("/graph/sources", dependencies=[Depends(require_role("read"))])
def list_graph_sources_endpoint() -> dict[str, Any]:
    """Every registered graph source (name/title/min_role/params) — mirrors
    `list_panels_endpoint` above. Source metadata is configuration, not
    secret; every `read` token sees the full list regardless of its own
    role (a source's own `min_role` only gates *fetching its data*, via
    `get_graph_endpoint`/`get_graph_node_detail_endpoint` below)."""
    sources = graph_module.list_graph_sources()
    return {
        "sources": [
            {
                "name": s.name,
                "title": s.title or s.name,
                "min_role": s.min_role,
                "params": list(s.params),
            }
            for s in sources
        ]
    }


@v1.get("/graph/{source}")
@app.get("/graph/{source}")
def get_graph_endpoint(
    source: str,
    request: Request,
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    """A single graph source's data. Unlike `/v1/graph/sources`, the
    required role is DATA-DEPENDENT (mirrors `get_panel_endpoint`): the
    floor `Depends(require_role("read"))` only enforces "any authenticated
    token"; the source's own `min_role` is enforced HERE, after resolution.

    Unknown source -> 404. `GraphContext` is built from the CALLER's own
    resolved tenant+role (never client-supplied) plus the raw query
    params. Run via `run_graph_fetch`, which never raises — a raising/
    malformed source degrades to a normalized single error node, never a
    500.
    """
    spec = graph_module.get_graph_source(source)
    if spec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Graph source not found")

    _enforce_graph_min_role(spec, caller, f"/v1/graph/{source}")

    ctx = graph_module.GraphContext(
        tenant=caller.tenant, role=caller.role, params=dict(request.query_params)
    )
    data = graph_module.run_graph_fetch(spec, ctx)
    return _graph_data_to_dict(data)


@v1.get("/graph/{source}/node/{node_id}")
@app.get("/graph/{source}/node/{node_id}")
def get_graph_node_detail_endpoint(
    source: str,
    node_id: str,
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    """A single node's detail. Unknown source -> 404; the source's
    `min_role` is enforced after resolution (same as `get_graph_endpoint`).
    A source with no `node_detail` callable, or one that returns `None` for
    this node id, -> 404. `run_graph_node_detail` never raises — an
    exception inside a source's own `node_detail` degrades to a normalized
    error `GraphDetail` (200), never a 500.
    """
    spec = graph_module.get_graph_source(source)
    if spec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Graph source not found")

    _enforce_graph_min_role(spec, caller, f"/v1/graph/{source}/node/{node_id}")

    ctx = graph_module.GraphContext(tenant=caller.tenant, role=caller.role, params={})
    detail = graph_module.run_graph_node_detail(spec, ctx, node_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    return _graph_detail_to_dict(detail)


@v1.post("/chatops/slack", dependencies=[Depends(require_role("run"))])
@app.post("/chatops/slack", dependencies=[Depends(require_role("run"))])
def slack_handler(payload: dict[str, Any]):
    response = chatops_service.handle_slack(payload)
    return {"response": response}


@v1.post("/chatops/discord", dependencies=[Depends(require_role("run"))])
@app.post("/chatops/discord", dependencies=[Depends(require_role("run"))])
def discord_handler(payload: dict[str, Any]):
    response = chatops_service.handle_discord(payload)
    return {"response": response}


@v1.post("/chatops/telegram", dependencies=[Depends(require_role("run"))])
@app.post("/chatops/telegram", dependencies=[Depends(require_role("run"))])
def telegram_handler(payload: dict[str, Any]):
    response = chatops_service.handle_telegram(payload)
    return {"response": response}


@app.post("/webhook/telegram/{url_path}")
@v1.post("/webhook/telegram/{url_path}")
async def telegram_webhook(url_path: str, request: Request):
    """
    Receive Telegram updates in webhook mode.
    The url_path acts as a secret — Telegram only knows it if you registered it via
    `hivepilot telegram set-webhook`.  An optional X-Telegram-Bot-Api-Secret-Token
    header provides a second layer of verification.
    """
    from hivepilot.services import telegram_bot as tgbot

    expected_secret = settings.telegram_webhook_secret
    if expected_secret:
        incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if incoming_secret != expected_secret:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid secret")

    data = await request.json()
    await tgbot.process_update(data)
    return {"ok": True}


@app.post("/webhook/slack")
@v1.post("/webhook/slack")
async def slack_webhook(request: Request):
    from hivepilot.services.slack_bot import handle_webhook_request

    return await handle_webhook_request(request)


@app.post("/webhook/linear")
@v1.post("/webhook/linear")
async def linear_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("Linear-Delivery", "")
    from hivepilot.services.linear_service import handle_webhook, verify_webhook

    if not verify_webhook(body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")
    import json as _json

    payload = _json.loads(body)
    result = handle_webhook(payload)
    return {"status": "ok", "detail": result}


@app.post("/webhook/discord")
@v1.post("/webhook/discord")
async def discord_webhook(request: Request):
    """
    Receive Discord interactions in HTTP interactions mode.
    Discord requires Ed25519 signature verification on every request.
    """
    body = await request.body()
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    try:
        from hivepilot.services.discord_bot import verify_signature

        if not verify_signature(body, signature, timestamp):
            raise HTTPException(status_code=401, detail="Invalid signature")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    from hivepilot.services.discord_bot import handle_interaction

    result = handle_interaction(body, signature, timestamp)
    return result


@app.on_event("shutdown")
async def _shutdown_telegram():
    from hivepilot.services import telegram_bot as tgbot

    await tgbot.shutdown()


@app.on_event("shutdown")
async def _shutdown_slack():
    from hivepilot.services import slack_bot

    slack_bot.shutdown()


@app.get("/metrics")
@v1.get("/metrics")
def metrics():
    return Response(generate_latest(registry), media_type="text/plain")


# ---------------------------------------------------------------------------
# Generic named webhook trigger (Phase 25) — POST /webhook/trigger/{name}
# Fires a named schedule entry on demand. Returns immediately; run is async.
# ---------------------------------------------------------------------------
class TriggerResponse(BaseModel):
    schedule_name: str
    status: str
    detail: str


@app.post("/webhook/trigger/{schedule_name}", dependencies=[Depends(require_role("run"))])
@v1.post("/webhook/trigger/{schedule_name}", dependencies=[Depends(require_role("run"))])
def trigger_schedule(schedule_name: str, request: Request):
    """
    Fire a named schedule entry immediately, regardless of its cron expression.
    Useful for triggering automation from external tools (Zapier, n8n, mobile shortcuts).
    The run executes asynchronously — use GET /approvals or Telegram to track it.
    """
    import threading

    from hivepilot.services import schedule_service

    schedules = schedule_service.load_schedules(
        settings.resolve_config_path(settings.schedules_file)
    )
    entry = schedules.get(schedule_name)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Schedule '{schedule_name}' not found",
        )

    def _fire():
        try:
            schedule_service.run_entry(entry, _get_orchestrator())
        except Exception as exc:  # noqa: BLE001
            from hivepilot.utils.logging import get_logger

            get_logger(__name__).error(
                "webhook.trigger.failed", schedule=schedule_name, error=str(exc)
            )

    threading.Thread(target=_fire, daemon=True).start()
    return TriggerResponse(
        schedule_name=schedule_name,
        status="triggered",
        detail=f"Schedule '{schedule_name}' fired asynchronously",
    )


# ---------------------------------------------------------------------------
# Mirador web UI (Sprint 2) — serves the pre-built static bundle committed
# under hivepilot/webui/static/ (see hivepilot/webui/__init__.py). Gated by
# settings.enable_webui (env HIVEPILOT_ENABLE_WEBUI) AND a real build being
# present, both read fresh on every request so a disabled/absent UI is a
# clean 404 — no auth required to load the shell itself (the shell's own
# token gate, not this server, is what protects the data underneath; every
# /v1/* call it makes is auth-enforced as normal).
# ---------------------------------------------------------------------------
from fastapi.responses import FileResponse, RedirectResponse  # noqa: E402

from hivepilot import webui  # noqa: E402

# NOTE: import the module (`webui`), not its names — `webui.STATIC_DIR` /
# `webui.INDEX_HTML` are read fresh via attribute access below (and are what
# tests monkeypatch); `from hivepilot.webui import INDEX_HTML` would instead
# bind a stale copy at import time that a monkeypatched `webui.INDEX_HTML`
# could never reach.


def _webui_enabled() -> bool:
    return bool(settings.enable_webui) and webui.static_available()


@app.get("/ui", include_in_schema=False)
def redirect_webui_to_trailing_slash() -> RedirectResponse:
    # The SPA's built assets use relative paths (`assets/index-*.js`), which
    # the browser resolves against the *current directory* of the requested
    # URL. At `/ui` (no trailing slash) that directory is `/`, so the asset
    # requests land on non-existent root routes -> 404 -> blank page. At
    # `/ui/` it's `/ui/`, matched by the `/ui/{sub_path:path}` route below.
    # Redirecting here keeps `/ui` working as the documented entrypoint
    # while only ever serving the bundle from under `/ui/`.
    if not _webui_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return RedirectResponse(url="/ui/", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@app.get("/ui/", include_in_schema=False)
@app.get("/ui/{sub_path:path}", include_in_schema=False)
def serve_webui(sub_path: str = "") -> FileResponse:
    if not _webui_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    # A traversal attempt or unknown sub-path (resolve_static_path() returns
    # None) intentionally degrades to serving INDEX_HTML — the SPA fallback
    # for client-side routing. This is not an oversight: resolve_static_path()
    # has already guaranteed the request can never escape STATIC_DIR before
    # we get here, so falling back to the index is always safe.
    file_path = webui.resolve_static_path(sub_path) or webui.INDEX_HTML
    return FileResponse(str(file_path))


# The committed build (hivepilot/webui/static/index.html) was produced by
# Vite with `base: '/'`, so it references its own assets with root-absolute
# paths (`/assets/index-*.js`, `/assets/index-*.css`, `/favicon.svg`) rather
# than paths relative to `/ui/`. The browser therefore always requests these
# at the root, regardless of `/ui` vs `/ui/` — so they must be served at the
# root too, gated by the same `_webui_enabled()` check and the same
# `webui.resolve_static_path()` traversal guard as `serve_webui` above.
@app.get("/assets/{sub_path:path}", include_in_schema=False)
def serve_webui_assets(sub_path: str) -> FileResponse:
    if not _webui_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    file_path = webui.resolve_static_path(f"assets/{sub_path}")
    if file_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return FileResponse(str(file_path))


@app.get("/favicon.svg", include_in_schema=False)
def serve_webui_favicon() -> FileResponse:
    if not _webui_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    file_path = webui.resolve_static_path("favicon.svg")
    if file_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return FileResponse(str(file_path))


app.include_router(v1)
