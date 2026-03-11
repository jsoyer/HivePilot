from __future__ import annotations

import threading
import uuid
from collections import defaultdict
from time import time
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from pydantic import BaseModel, field_validator

from hivepilot.config import settings
from hivepilot.orchestrator import Orchestrator
from hivepilot.services import chatops_service, state_service, token_service
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

_RATE_LIMITED_PATHS = {"/run", "/v1/run", "/chatops/slack", "/chatops/discord", "/chatops/telegram",
                       "/v1/chatops/slack", "/v1/chatops/discord", "/v1/chatops/telegram"}


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


registry = CollectorRegistry()
run_counter = Counter("hivepilot_runs_total", "Total runs", ["status"], registry=registry)
run_duration = Histogram("hivepilot_run_duration_seconds", "Run duration", registry=registry)


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
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        state_service.record_audit(
            token_hash=entry.token[:16],
            role=entry.role,
            endpoint=request.url.path,
            method=request.method,
            result="authorized",
        )
        return entry

    return dependency


class RunRequest(BaseModel):
    task: str
    projects: list[str]
    extra_prompt: str | None = None
    auto_git: bool = False

    @field_validator("extra_prompt", mode="before")
    @classmethod
    def validate_extra_prompt(cls, v: str | None) -> str | None:
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
        runner_count = len(orch.registry._definitions) if hasattr(orch.registry, "_definitions") else -1
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


@v1.post("/run", dependencies=[Depends(require_role("run"))])
@app.post("/run", dependencies=[Depends(require_role("run"))])
def run_task(request: RunRequest):
    with run_duration.time():
        results = _get_orchestrator().run_task(
            project_names=request.projects,
            task_name=request.task,
            extra_prompt=request.extra_prompt,
            auto_git=request.auto_git,
        )
    for result in results:
        run_counter.labels(status="success" if result.success else "failure").inc()
    return {"results": [result.__dict__ for result in results]}


@v1.get("/projects", dependencies=[Depends(require_role("read"))])
@app.get("/projects", dependencies=[Depends(require_role("read"))])
def list_projects():
    return _get_orchestrator().projects.projects


@v1.get("/tasks", dependencies=[Depends(require_role("read"))])
@app.get("/tasks", dependencies=[Depends(require_role("read"))])
def list_tasks():
    return list(_get_orchestrator().tasks.tasks.keys())


@v1.get("/approvals", dependencies=[Depends(require_role("run"))])
@app.get("/approvals", dependencies=[Depends(require_role("run"))])
def pending_approvals():
    return state_service.get_pending_approvals()


class ApprovalAction(BaseModel):
    approver: str = "api"
    approve: bool = True
    reason: str | None = None


@v1.post("/approvals/{run_id}", dependencies=[Depends(require_role("approve"))])
@app.post("/approvals/{run_id}", dependencies=[Depends(require_role("approve"))])
def handle_approval(run_id: int, action: ApprovalAction):
    with run_duration.time():
        result = _get_orchestrator().run_approved(
            run_id=run_id,
            approve=action.approve,
            approver=action.approver,
            reason=action.reason,
        )
    run_counter.labels(status="success" if result.success else "failure").inc()
    return {"result": result.__dict__}


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

    schedules = schedule_service.load_schedules(settings.resolve_path(settings.schedules_file))
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
            get_logger(__name__).error("webhook.trigger.failed", schedule=schedule_name, error=str(exc))

    threading.Thread(target=_fire, daemon=True).start()
    return TriggerResponse(
        schedule_name=schedule_name,
        status="triggered",
        detail=f"Schedule '{schedule_name}' fired asynchronously",
    )


app.include_router(v1)
