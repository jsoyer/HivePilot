from __future__ import annotations

import csv
import io
import json
import logging
import threading
import uuid
from collections import defaultdict
from pathlib import Path
from time import monotonic, sleep, time
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import generate_latest
from pydantic import BaseModel, Field, field_validator

from hivepilot import roles
from hivepilot.config import settings
from hivepilot.orchestrator import Orchestrator
from hivepilot.services import (
    analytics_service,
    async_run_service,
    autopilot_policy,
    autopilot_queue,
    chatops_service,
    efficiency_service,
    memory_service,
    notification_service,
    plugin_activity,
    policy_service,
    state_service,
    telemetry_service,
    token_service,
)
from hivepilot.services.metrics import registry, run_duration_seconds
from hivepilot.ui.plugin_persist import persist_plugins_disabled
from hivepilot.utils.validation import MAX_PROMPT_LEN, check_prompt_injection, sanitize_prompt

logger = logging.getLogger(__name__)

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


# -- Startup path logging (bug-debt fix) -------------------------------------
@app.on_event("startup")
async def _log_startup_paths() -> None:
    """Log the RESOLVED, ABSOLUTE paths this API server process is actually
    using (state DB / topics registry / config dir / prompts dir / vault) —
    see `hivepilot.utils.startup_paths` for the full rationale (the
    cwd-relative split between a service `cwd=/` and a CLI `cwd=$HOME` run
    has cost real debugging hours)."""
    from hivepilot.utils.startup_paths import log_resolved_startup_paths

    log_resolved_startup_paths(settings)


# -- Agent Studio (HP-25): adopt the store roster on boot ---------------------
@app.on_event("startup")
async def _adopt_store_roster() -> None:
    """Make a restart pick up any store-backed role edits. `refresh_roles()` is
    store-first (HP-25 slice 2): it adopts the DB roster when the store has been
    seeded and otherwise reloads `roles.yaml` unchanged — so an untouched
    deployment stays byte-identical, while one that used `POST/PUT/DELETE
    /v1/roles` (which seed the store) comes back up on the edited roster.
    Never fatal: a bad store must not stop the API from serving."""
    from hivepilot.utils.logging import get_logger

    try:
        with _orch_lock:
            roles.refresh_roles()
    except Exception as exc:  # noqa: BLE001 — startup must not crash on this
        get_logger(__name__).warning("store_roster_adopt_failed", error=str(exc))


# -- Agent voice (HP-49): make roles actually reply in Espaces + as subagents --
@app.on_event("startup")
async def _register_agent_voice() -> None:
    """Wire the runner-backed agent voice into the Espaces dépose/relève loop
    (HP-46) and the delegation subagent primitive (HP-48). Fail-safe — a wiring
    error must never stop the API from serving."""
    from hivepilot.utils.logging import get_logger

    try:
        from hivepilot.services import agent_voice

        agent_voice.register()
    except Exception as exc:  # noqa: BLE001
        get_logger(__name__).warning("agent_voice.register_failed", error=str(exc))


# -- Partition claim reconciliation (propose -> ratify -> dispatch PRD, §8) --
@app.on_event("startup")
async def _reconcile_partition_claims() -> None:
    """Rewind partition claims a crashed dispatcher left behind.

    A crash between `claim_task` and the run-row creation leaves a
    `status='claimed' AND run_id IS NULL` row -- visible, and by construction
    never a double dispatch. This sweeps exactly those, exactly once (the
    release is a conditional UPDATE), and only past the staleness threshold
    so a LIVE dispatcher mid-claim is never rewound out from under itself.

    Never fatal: an API server must still start when the journal cannot be
    read.
    """
    from hivepilot.utils.logging import get_logger

    try:
        from hivepilot.services import partition_service

        released = partition_service.reconcile_stale_claims()
        if released:
            get_logger(__name__).info("api.partition_claims_reconciled", released=len(released))
    except Exception as exc:  # noqa: BLE001 - startup must never die on a maintenance sweep
        get_logger(__name__).warning(
            "api.partition_reconcile_failed", error=f"{exc.__class__.__name__}: {exc}"
        )


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
    # F2 fix: the Challenge/Ask feature routes ALL THREE channels (Telegram,
    # Slack, Discord) through the single shared `human_challenge()`
    # entrypoint, which spawns a full LLM invocation on every call and is
    # NOT idempotent/read-only (see orchestrator.human_challenge). Without
    # rate-limiting these webhook paths too, any member of an allowed
    # channel had an unmetered LLM-spend primitive -- contradicting the
    # `budget_daily_usd` policy this project ships. Telegram's webhook path
    # is keyed by a per-deployment secret URL segment
    # (`/webhook/telegram/{url_path}`) rather than a fixed path, so it can't
    # be listed here the same way; it's out of scope for this fix.
    "/webhook/slack",
    "/webhook/discord",
    "/v1/webhook/slack",
    "/v1/webhook/discord",
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
# OTLP metric ingest
# ---------------------------------------------------------------------------
# The agent CLI posts its own metrics here.  Mounted outside /v1 because the
# path is fixed by the OTLP spec: an exporter pointed at ENDPOINT always POSTs
# to ENDPOINT/v1/metrics, so the prefix belongs to OpenTelemetry, not to us.

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})

# An OTLP batch for a single CLI session measured ~7 KB.  A megabyte is far
# above anything legitimate and keeps a stray POST from being a memory event.
_MAX_OTLP_BODY = 1_048_576


def _is_loopback(request: Request) -> bool:
    """Whether this request came from the same host.

    A forwarded header means it travelled through a proxy, so it did not
    originate on loopback whatever the socket says.
    """
    if request.headers.get("x-forwarded-for") or request.headers.get("forwarded"):
        return False
    return bool(request.client and request.client.host in _LOOPBACK_HOSTS)


@app.post("/otlp/v1/metrics")
async def otlp_metrics(request: Request) -> dict[str, Any]:
    """Accept an OTLP/JSON metric batch from a local agent CLI.

    Unauthenticated on purpose.  Authenticating it would mean putting a bearer
    token in ``OTEL_EXPORTER_OTLP_HEADERS``, and ``OTEL_*`` now reaches the
    agent subprocess -- handing an API credential to the sandboxed process is a
    worse trade than accepting metrics from localhost only.

    Always answers 200 once past the guards.  An error here makes the exporter
    retry and log against the very process being measured, so a batch we cannot
    read is dropped quietly rather than turned into noise on the agent.
    """
    if not settings.otel_ingest_enabled:
        raise HTTPException(status_code=404, detail="Not Found")

    if not _is_loopback(request):
        raise HTTPException(status_code=403, detail="OTLP ingest accepts loopback only")

    body = await request.body()
    if len(body) > _MAX_OTLP_BODY:
        logger.warning("otlp ingest: dropping %d-byte body over cap", len(body))
        return {"partialSuccess": {}}

    try:
        payload = json.loads(body or b"{}")
        points = telemetry_service.parse_otlp_metrics(payload)
        telemetry_service.record_metrics(points)
    except Exception:  # noqa: BLE001 - never fail the thing being measured
        logger.exception("otlp ingest: unreadable batch dropped")

    return {"partialSuccess": {}}


@app.post("/otlp/v1/logs")
async def otlp_logs(request: Request) -> dict[str, Any]:
    """Accept an OTLP/JSON LOG batch from a local agent CLI.

    Why this exists: the box had `CLAUDE_CODE_ENABLE_TELEMETRY=1` and
    `OTEL_METRICS_EXPORTER=otlp` but no `OTEL_LOGS_EXPORTER` -- and it is an
    EVENT, `claude_code.api_request`, that carries `cost_usd_micros`, the token
    breakdown, the model and `agent.name`. The precise half of the cost had
    never been exported, and setting the exporter without this route would have
    posted every event into a 404 with nobody looking.

    Same guards and same posture as `/otlp/v1/metrics`: loopback only,
    unauthenticated on purpose (a bearer token in `OTEL_EXPORTER_OTLP_HEADERS`
    would hand a credential to the sandboxed agent subprocess), body capped,
    and ALWAYS 200 past the guards -- an error here makes the exporter retry
    and log against the very process being measured.
    """
    if not settings.otel_ingest_enabled:
        raise HTTPException(status_code=404, detail="Not Found")

    if not _is_loopback(request):
        raise HTTPException(status_code=403, detail="OTLP ingest accepts loopback only")

    body = await request.body()
    if len(body) > _MAX_OTLP_BODY:
        logger.warning("otlp ingest: dropping %d-byte log body over cap", len(body))
        return {"partialSuccess": {}}

    try:
        from hivepilot.services.otlp_events import parse_otlp_events

        payload = json.loads(body or b"{}")
        events = parse_otlp_events(payload)
        if events:
            # Count what arrived, per model, so "no cost data" and "the
            # exporter is not sending" stop looking alike -- the whole reason
            # this endpoint exists.
            telemetry_service.record_api_request_events(events)
    except Exception:  # noqa: BLE001 - never fail the thing being measured
        logger.exception("otlp ingest: unreadable log batch dropped")

    return {"partialSuccess": {}}


# ---------------------------------------------------------------------------
# /v1/ versioned router (Phase 14b)
# ---------------------------------------------------------------------------
v1 = APIRouter(prefix="/v1")


@v1.get("/memory/backends", dependencies=[Depends(require_role("read"))])
def memory_backends(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Recall/store activity per memory backend, plus what each one costs.

    The two backends are deliberately reported side by side on the same
    counters. They are NOT interchangeable and the panel says so: mem0 sends
    content to a third party and answers semantically; Obsidian stays on the
    host and answers by role-scoped search. Measured on this deployment, their
    recalls overlap by 2-4% -- they are complements, and a screen showing only
    one would suggest the other is dead weight.

    `empty_searches` is the comparable KPI. A search returning a full top-k
    means the cap was hit, not that k relevant things exist.
    """
    from hivepilot.services import memory_service

    stats = memory_service.backend_stats(days)
    return {
        "days": days,
        "backends": stats,
        # Stated in the payload, not left to the UI: whether work leaves the
        # host is a property of the backend, and it is the single fact an
        # operator most needs beside these counters.
        "egress": {"mem0": True, "obsidian": False},
    }


@v1.get("/telemetry/cache", dependencies=[Depends(require_role("read"))])
def telemetry_cache(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Prompt-cache economics from the agent CLI's own metrics.

    Exposes a median and a count below break-even rather than a fleet ratio.
    An aggregate is dominated by whichever session read the most, which is how
    an 85% hit rate coexisted here with 1.7M tokens of creation never read
    back -- a screen built on the average would have shown nothing wrong.
    """
    report = telemetry_service.cache_report(days)
    worst = report.worst
    return {
        "sessions": report.sessions,
        "median_amortisation": round(report.median_amortisation, 3),
        "below_one": report.below_one,
        "wasted_tokens": int(report.wasted_tokens),
        "healthy": report.healthy,
        "worst": (
            None
            if worst is None
            else {
                "session_id": worst.session_id,
                "model": worst.model,
                "created": int(worst.created),
                "read": int(worst.read),
                "amortisation": round(worst.amortisation, 3),
            }
        ),
    }


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
    identity — this is what powers the Pollen web client's `useRole()`
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


_RUNS_LIMIT_DEFAULT = 50
_RUNS_LIMIT_MAX = 500


@v1.get("/runs")
@app.get("/runs")
def list_runs(
    caller: token_service.TokenEntry = Depends(require_role("run")),
    limit: int = Query(
        _RUNS_LIMIT_DEFAULT,
        ge=1,
        le=_RUNS_LIMIT_MAX,
        description="How many recent runs to return (1-500).",
    ),
):
    """List runs, filtered to caller's tenant for non-admin roles.

    `limit` is caller-chosen because the board was pinned to 50 with no way
    to ask for fewer — an operator watching one pipeline had to read 50 cards
    to find it. Bounded on both ends by FastAPI (`ge`/`le`) rather than
    clamped silently: an out-of-range value is a 422 the caller can see, not
    a number quietly replaced with a different one.
    """
    if caller.role == "admin":
        return state_service.list_recent_runs(limit=limit)
    return state_service.list_recent_runs(limit=limit, tenant=caller.tenant)


# ---------------------------------------------------------------------------
# Realtime SSE stream (HP-41, Cycle 1 · P1). Turns the durable change bus
# (HP-40, `services/events.py`) into a browser `EventSource` feed so Pollen can
# stop polling: each run/step lifecycle change is pushed as an SSE event whose
# `id:` is the `change_log` id, so a dropped connection reconnects with
# `Last-Event-ID` and replays from exactly where it left off — no gaps, no
# dupes. Non-admin callers only see their own tenant's changes.
# ---------------------------------------------------------------------------


def _format_sse(row: dict[str, Any]) -> str:
    """Render one change_log row as an SSE frame. The `id:` line drives the
    browser's automatic `Last-Event-ID` reconnection; `event:` is the change
    kind so clients can `addEventListener('run.completed', ...)`."""
    data = json.dumps(
        {
            "id": row["id"],
            "kind": row["kind"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "tenant": row["tenant"],
            "payload": row.get("payload"),
        }
    )
    return f"id: {row['id']}\nevent: {row['kind']}\ndata: {data}\n\n"


def sse_stream(
    after_id: int | None,
    *,
    tenant_scope: str | None = None,
    stop: Any | None = None,
    poll_interval: float = 0.5,
    heartbeat_interval: float = 15.0,
    idle_timeout: float | None = None,
) -> Any:
    """Yield SSE frames for changes after `after_id` (defaults to "now"),
    scoped to `tenant_scope` (None = admin/all). Tails the durable `change_log`
    so it is dialect-agnostic and reconnection-safe. Emits a `: keep-alive`
    comment every `heartbeat_interval`s of quiet so proxies don't drop the
    idle connection; `idle_timeout` ends the stream (used by tests / to recycle
    long-idle connections). Blocking generator — FastAPI runs it on a worker."""
    from hivepilot.services import events

    cursor = events.latest_change_id() if after_id is None else after_id
    last_beat = monotonic()
    last_activity = monotonic()
    yield ": connected\n\n"  # open the stream so the client's onopen fires promptly
    while stop is None or not stop.is_set():
        rows = events.read_since(cursor)
        emitted = False
        for row in rows:
            cursor = int(row["id"])
            if tenant_scope is not None and row.get("tenant") != tenant_scope:
                continue  # consumed (cursor advanced) but not visible to this caller
            yield _format_sse(row)
            emitted = True
        now = monotonic()
        if emitted:
            last_activity = now
            last_beat = now
        else:
            if now - last_beat >= heartbeat_interval:
                yield ": keep-alive\n\n"
                last_beat = now
            if idle_timeout is not None and (now - last_activity) >= idle_timeout:
                return
        sleep(poll_interval)


@v1.get("/events/stream")
@app.get("/events/stream")
def events_stream_endpoint(
    request: Request,
    caller: token_service.TokenEntry = Depends(require_role("read")),
    after: int | None = Query(
        None, description="Resume after this change_log id (else stream from now)."
    ),
) -> Any:
    from fastapi.responses import StreamingResponse

    tenant_scope = None if caller.role == "admin" else caller.tenant
    start = after
    if start is None:
        last_event_id = request.headers.get("Last-Event-ID")
        if last_event_id is not None:
            try:
                start = int(last_event_id)
            except ValueError:
                start = None
    return StreamingResponse(
        sse_stream(start, tenant_scope=tenant_scope),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


class ConversationReply(BaseModel):
    """An operator instruction addressed to a role, not to a running agent."""

    role: str
    text: str


@v1.post("/approvals/{run_id}")
@app.post("/approvals/{run_id}")
def handle_approval(
    run_id: int,
    action: ApprovalAction,
    caller: token_service.TokenEntry = Depends(require_role("approve")),
):
    """Approve/deny a run. Non-admin callers may only act on their own tenant's runs.

    Routes through `Orchestrator.approve_run` -- the single shared entrypoint
    (also used by `telegram_bot`/`slack_bot`/`discord_bot`/`chatops_service`/
    the CLI) that discriminates a pipeline-checkpoint approval (dispatches to
    `resume_pipeline`) from a per-task approval (dispatches to `run_approved`),
    so this endpoint no longer KeyErrors on a pipeline checkpoint (Pollen's
    "Approve" 500 -- live traceback was `KeyError: 'noxys'`).

    Explicit-failure-logs sprint, Part A.2: logs the attempt (run_id, approve,
    approver, caller) BEFORE dispatching -- `approve_run`'s own dispatch (see
    `Orchestrator.approve_run`) additionally logs the resolved route, and
    `resume_pipeline`/`run_approved` each log their OWN specific rejection
    reason (unknown run / not pending / wrong checkpoint kind / unknown task)
    before raising -- so both this endpoint AND Telegram/Slack/Discord/
    ChatOps/CLI get the same structured logging for free just by calling
    `approve_run`, without each caller re-implementing it. On failure, a
    known rejection (`ValueError`/`KeyError`) is translated into a clean 400
    instead of an opaque 500; a genuinely unexpected exception is still
    logged with full context before surfacing as a 500.
    """
    from hivepilot.utils.logging import get_logger

    api_logger = get_logger(__name__)

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
    api_logger.info(
        "api.approval.requested",
        run_id=run_id,
        approve=action.approve,
        approver=action.approver,
        caller_role=caller.role,
        caller_tenant=caller.tenant,
    )
    try:
        with run_duration_seconds.time():
            result = _get_orchestrator().approve_run(
                run_id=run_id,
                approve=action.approve,
                approver=action.approver,
                reason=action.reason,
            )
    except (ValueError, KeyError) as exc:
        # Every KNOWN rejection reason (unknown run / not pending / unknown
        # task, or -- before this fix -- a pipeline checkpoint's task-name
        # KeyError) -- `Orchestrator.approve_run` routes pipeline checkpoints
        # to `resume_pipeline` and per-task approvals to `run_approved`, and
        # both log their OWN specific `approval.*_rejected` reason before
        # raising; surface that same reason to the caller as a clean 400
        # rather than letting it fall through as an unhandled 500.
        api_logger.warning("api.approval.rejected", run_id=run_id, error=str(exc))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — genuinely unexpected: log full context, still 500
        api_logger.error("api.approval.failed_unexpected", run_id=run_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Approval processing failed for run {run_id}: {exc}",
        ) from exc
    return {"result": result.__dict__}


# ---------------------------------------------------------------------------
# Partitions (propose -> ratify -> dispatch PRD, Sprint 3 -- spec §5/§7).
#
# `/v1`-only, like every other endpoint added after Phase 14b.
#
# RBAC floors, and why each is where it is:
#   - `GET /v1/partitions`, `GET /v1/partitions/{id}`  -> `run`
#     A partition document carries every task's full PROMPT, which is the
#     same class of content `GET /v1/runs/{run_id}` guards at `run` rather
#     than `read`. A bare `read` token must not be able to harvest it.
#   - `POST /v1/partitions/{id}/ratify`                -> `approve`
#     Ratification IS an approval, and it is the single gate between a
#     proposal and N dispatched agents.
#   - `POST /v1/partitions/{id}/preview`               -> `approve`
#     A DRY RUN of that same gate (Sprint 4, for Pollen's ratification
#     view). It changes nothing, but it reports the exact verdict the
#     ratification would produce -- so anything less than `approve` would
#     make it a cheaper oracle for probing policy than the action itself.
#   - `POST /v1/partitions/{id}/cancel`                -> `run`
#     Mirrors `POST /v1/runs/{run_id}/cancel`: stopping work is deliberately
#     a LOWER bar than starting it.
#
# Tenant isolation mirrors the endpoints each one is modelled on: the GETs
# report a cross-tenant id as 404 (never leaking that another tenant has a
# partition with that id -- same rule as `get_run`), while the two writes
# report 403 (`handle_approval`/`cancel_run`'s shape, where the caller
# already had to hold `approve`/`run` and the distinction is actionable).
#
# **The error mapping is NEVER re-derived here.** Every refusal
# `partition_service` raises carries its own `status_code`/`code` next to the
# rule it belongs to, so this layer TRANSLATES and nothing more -- there is
# exactly one definition of "a stale digest is a 409", and it lives with the
# digest check.
# ---------------------------------------------------------------------------


class PartitionSummary(BaseModel):
    id: str
    tenant: str = "default"
    status: str
    source_kind: str | None = None
    source_ref: str | None = None
    proposed_digest: str | None = None
    ratified_digest: str | None = None
    outward_consent: bool = False
    ratified_by: str | None = None
    ratified_at: str | None = None
    created_ts: str | None = None
    updated_ts: str | None = None


class PartitionTaskRow(BaseModel):
    task_id: str
    status: str
    run_id: int | None = None
    queue_id: int | None = None
    attempt: int = 0
    claimed_by: str | None = None
    claimed_at: str | None = None
    # `None` means the forge did not report a URL. Pollen renders it as "—".
    # It is NEVER a fabricated link.
    pr_url: str | None = None
    cost_usd: float | None = None
    wall_clock_seconds: int | None = None


class PartitionParallelism(BaseModel):
    """The EFFECTIVE parallelism this host would give the plan.

    Surfaced (spec §7) because `runner_throttle` caps the `claude` runner at
    `claude_max_concurrency`, default **1** -- so a `max_parallel: 3` plan is
    one agent three times on a default install, and the ratify UI must say so
    rather than promising three.
    """

    requested: int
    effective: int
    concurrency_limit: int
    runner_cap: int
    runner_kinds: list[str] = []
    notes: list[str] = []


class PartitionDetail(PartitionSummary):
    proposed_json: str | None = None
    ratified_json: str | None = None
    ratified_diff: str | None = None
    outward_actions: list[str] = []
    total_cost_usd: float | None = None
    waves: list[list[str]] = []
    parallelism: PartitionParallelism | None = None
    tasks: list[PartitionTaskRow] = []


class PartitionPreviewRequest(BaseModel):
    """A dry run of the gate over the plan currently in the operator's
    editor. `outward_consent` mirrors the checkbox's live state so the
    verdict the UI shows is the verdict it would actually get."""

    partition_json: str
    outward_consent: bool = False


class PartitionPreviewResponse(BaseModel):
    """The gate's verdict for a plan that has NOT been submitted.

    `ok` is the single thing Pollen gates its dispatch button on — it is
    `validate_ratification`'s own answer, not a browser-side re-derivation of
    the rules. `code`/`status_code`/`detail` are the refusing
    `RatificationError`'s own attributes, carried verbatim so there is still
    exactly one definition of "a stale digest is a 409".

    `outward_actions` is always the LIVE-config-computed footprint (never the
    plan's self-declared `outward` flags), so the consent warning can name
    the exact actions while the checkbox is still unticked.
    """

    ok: bool
    code: str | None = None
    status_code: int | None = None
    detail: str | None = None
    outward_actions: list[str] = []
    total_cost_usd: float | None = None
    waves: list[list[str]] = []
    task_ids: list[str] = []
    parallelism: PartitionParallelism | None = None


class PartitionRatifyRequest(BaseModel):
    partition_json: str
    outward_consent: bool = False
    approver: str = "api"
    expected_digest: str | None = None
    # One click ratifies AND dispatches (spec §12.5): a ratified-but-
    # undispatched partition is a dangling state that goes stale as the repo
    # moves. The CONSENT decoupling (`outward_consent`) is the strong half and
    # is preserved; the STEP decoupling is not.
    dispatch: bool = True


class PartitionRatifyResponse(BaseModel):
    partition_id: str
    status: str
    ratified_digest: str
    outward_actions: list[str]
    outward_consent: bool
    task_ids: list[str]
    diff: str
    warnings: list[str] = []
    idempotent: bool = False
    dispatching: bool = False
    parallelism: PartitionParallelism | None = None


class PartitionCancelResponse(BaseModel):
    partition_id: str
    cancelled_tasks: list[str]


def _partition_row_tenant(caller: token_service.TokenEntry) -> str | None:
    """`None` (every tenant) for admin, else the caller's own tenant --
    the same convention `_analytics_tenant` uses."""
    return None if caller.role == "admin" else caller.tenant


def _parallelism_model(assessment: object) -> PartitionParallelism:
    return PartitionParallelism(
        requested=getattr(assessment, "requested", 1),
        effective=getattr(assessment, "effective", 1),
        concurrency_limit=getattr(assessment, "concurrency_limit", 1),
        runner_cap=getattr(assessment, "runner_cap", 1),
        runner_kinds=list(getattr(assessment, "runner_kinds", ())),
        notes=list(getattr(assessment, "notes", ())),
    )


def _partition_summary(row: dict) -> PartitionSummary:
    return PartitionSummary(
        id=str(row.get("id")),
        tenant=str(row.get("tenant") or "default"),
        status=str(row.get("status") or "unknown"),
        source_kind=row.get("source_kind"),
        source_ref=row.get("source_ref"),
        proposed_digest=row.get("proposed_digest"),
        ratified_digest=row.get("ratified_digest"),
        outward_consent=bool(row.get("outward_consent")),
        ratified_by=row.get("ratified_by"),
        ratified_at=str(row["ratified_at"]) if row.get("ratified_at") is not None else None,
        created_ts=str(row["created_ts"]) if row.get("created_ts") is not None else None,
        updated_ts=str(row["updated_ts"]) if row.get("updated_ts") is not None else None,
    )


@v1.get("/partitions")
def list_partitions_endpoint(
    status_filter: str | None = None,
    limit: int = 50,
    caller: token_service.TokenEntry = Depends(require_role("run")),
) -> list[PartitionSummary]:
    """List partitions, newest first, scoped to the caller's tenant (admin:
    every tenant).

    `limit` is clamped to `[1, 500]` rather than trusted: an unbounded,
    caller-supplied page size on an endpoint that returns whole plan
    metadata is a free amplification lever.
    """
    from hivepilot.services import partition_service

    rows = partition_service.list_partitions(
        tenant=_partition_row_tenant(caller),
        status=status_filter,
        limit=max(1, min(int(limit), 500)),
    )
    return [_partition_summary(row) for row in rows]


@v1.get("/partitions/{partition_id}")
def get_partition_endpoint(
    partition_id: str,
    caller: token_service.TokenEntry = Depends(require_role("run")),
) -> PartitionDetail:
    """A single partition with its plan, journal rows, computed outward
    footprint, wave plan and EFFECTIVE parallelism.

    A cross-tenant id reports 404 exactly like a missing one (`get_run`'s
    rule): a GET must never let an unauthorized caller distinguish "wrong
    tenant" from "doesn't exist".
    """
    from hivepilot.partition import PartitionError, load_partition
    from hivepilot.services import partition_service

    row = partition_service.get_partition(partition_id, tenant=_partition_row_tenant(caller))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Partition not found")

    detail = PartitionDetail(
        **_partition_summary(row).model_dump(),
        proposed_json=row.get("proposed_json"),
        ratified_json=row.get("ratified_json"),
        ratified_diff=row.get("ratified_diff"),
        tasks=[
            PartitionTaskRow(
                task_id=str(task_row["task_id"]),
                status=str(task_row["status"]),
                run_id=task_row.get("run_id"),
                queue_id=task_row.get("queue_id"),
                attempt=int(task_row.get("attempt") or 0),
                claimed_by=task_row.get("claimed_by"),
                claimed_at=(
                    str(task_row["claimed_at"]) if task_row.get("claimed_at") is not None else None
                ),
                pr_url=task_row.get("pr_url"),
                cost_usd=task_row.get("cost_usd"),
                wall_clock_seconds=task_row.get("wall_clock_seconds"),
            )
            for task_row in partition_service.list_partition_tasks(partition_id)
        ],
    )

    document = str(row.get("ratified_json") or row.get("proposed_json") or "")
    try:
        plan = load_partition(document)
    except PartitionError:
        # An unparseable stored plan is reported as "no derived view", never
        # as a 500 and never as a fabricated empty wave plan.
        return detail

    assessment = partition_service.assess_outward(plan)
    detail.outward_actions = sorted(assessment.actions)
    detail.total_cost_usd = assessment.total_cost_usd
    detail.waves = [list(wave) for wave in partition_service.plan_waves(plan)]
    detail.parallelism = _parallelism_model(partition_service.effective_parallelism(plan))
    return detail


@v1.post("/partitions/{partition_id}/preview")
def preview_partition_endpoint(
    partition_id: str,
    body: PartitionPreviewRequest,
    caller: token_service.TokenEntry = Depends(require_role("approve")),
) -> PartitionPreviewResponse:
    """Dry-run the ratification gate over an edited plan. Changes NOTHING.

    Gated at `approve`, the same rank as the ratification itself: this
    endpoint reports the exact policy verdict a ratification would produce,
    so a lower rank here would make it a cheaper oracle for probing policy
    than the action it previews. Cross-tenant is 403 and an unknown id is
    404, both identical to `ratify_partition_endpoint` — the two must not
    disagree about who may look at what.

    Every answer comes from the SAME functions the real gate runs
    (`validate_ratification`, `assess_outward`, `plan_waves`,
    `effective_parallelism`). Nothing here re-derives a rule, and the
    refusing error's own `code`/`status_code`/message travel to the browser
    verbatim.

    A refusal is reported as HTTP 200 with `ok: false`, deliberately. A 4xx
    would be indistinguishable from a network failure in the browser and
    would discard the structured verdict — the outward footprint, the wave
    plan and the effective parallelism the operator needs precisely WHEN the
    plan is being refused. The gate's own status code still travels, as
    data. The authoritative refusal remains `POST .../ratify`, which is
    unchanged and still fail-closed on its own.

    Nothing is persisted: `validate_ratification` writes no denial rows (only
    `ratify_partition` does), so an operator editing JSON never floods the
    audit log.
    """
    from hivepilot.partition import PartitionError, load_partition
    from hivepilot.services import partition_service

    row = partition_service.get_partition(partition_id, tenant=None)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Partition not found")
    row_tenant = str(row.get("tenant") or "default")
    if caller.role != "admin" and row_tenant != caller.tenant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-tenant preview not allowed",
        )

    try:
        plan = load_partition(body.partition_json)
    except PartitionError as exc:
        # Step 1 refused, so there is no plan to derive an outward footprint,
        # a wave plan or a parallelism figure from. Reporting empty lists
        # here would read as "nothing outward" — the UI must show the parse
        # error INSTEAD of a footprint, never alongside a fabricated one.
        return PartitionPreviewResponse(
            ok=False,
            code=partition_service.MalformedPlanError.code,
            status_code=partition_service.MalformedPlanError.status_code,
            detail=str(exc),
        )

    # Computed BEFORE the verdict and outside any `except`: the consent
    # warning must name the outward actions precisely in the case where the
    # gate refuses for want of consent. `assess_outward` is itself
    # fail-closed (unresolvable pipeline config yields the FULL outward set),
    # so an exception escaping here is a genuine 500 — never a silently
    # empty, and therefore reassuring, action list.
    assessment = partition_service.assess_outward(plan)
    waves = [list(wave) for wave in partition_service.plan_waves(plan)]

    parallelism: PartitionParallelism | None = None
    try:
        parallelism = _parallelism_model(partition_service.effective_parallelism(plan))
    except Exception:  # noqa: BLE001 - a display figure renders "—", never a guessed number
        parallelism = None

    verdict = PartitionPreviewResponse(
        ok=True,
        outward_actions=sorted(assessment.actions),
        total_cost_usd=assessment.total_cost_usd,
        waves=waves,
        task_ids=[task.id for task in plan.tasks],
        parallelism=parallelism,
    )

    try:
        partition_service.validate_ratification(
            plan, outward_consent=body.outward_consent, tenant=row_tenant
        )
    except partition_service.RatificationError as exc:
        verdict.ok = False
        verdict.code = exc.code
        verdict.status_code = exc.status_code
        verdict.detail = str(exc)
    return verdict


@v1.post("/partitions/{partition_id}/ratify")
def ratify_partition_endpoint(
    partition_id: str,
    body: PartitionRatifyRequest,
    caller: token_service.TokenEntry = Depends(require_role("approve")),
) -> PartitionRatifyResponse:
    """Ratify a (possibly edited) plan and, by default, dispatch it.

    Cross-tenant ⇒ 403, mirroring `handle_approval`. Every other refusal is
    `partition_service`'s own, translated via its `status_code`: malformed
    400, referential 400, policy 403, consent 403, stale digest 409, unknown
    partition 404. This layer adds NO rules of its own -- adding one here
    would be a second, drifting copy of the gate.
    """
    from hivepilot.services import partition_service

    row = partition_service.get_partition(partition_id, tenant=None)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Partition not found")
    row_tenant = str(row.get("tenant") or "default")
    if caller.role != "admin" and row_tenant != caller.tenant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-tenant ratification not allowed",
        )

    try:
        outcome = partition_service.ratify_partition(
            partition_id,
            partition_json=body.partition_json,
            outward_consent=body.outward_consent,
            approver=body.approver,
            expected_digest=body.expected_digest,
            tenant=row_tenant,
        )
    except partition_service.RatificationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    parallelism = None
    try:
        from hivepilot.partition import load_partition

        parallelism = _parallelism_model(
            partition_service.effective_parallelism(load_partition(body.partition_json))
        )
    except Exception:  # noqa: BLE001 - a display figure must never fail a completed ratification
        parallelism = None

    dispatching = False
    if body.dispatch and not outcome.idempotent:
        # `dispatch_partition` BLOCKS at every wave boundary, so it runs on
        # its own coordinator thread and this handler returns immediately --
        # the same "return the handle, not the result" shape as
        # `POST /v1/runs`.
        partition_service.dispatch_partition_background(
            partition_id,
            orchestrator=_get_orchestrator(),
            tenant=row_tenant,
            claimed_by=body.approver,
        )
        dispatching = True

    return PartitionRatifyResponse(
        partition_id=outcome.partition_id,
        status=outcome.status,
        ratified_digest=outcome.ratified_digest,
        outward_actions=list(outcome.outward_actions),
        outward_consent=outcome.outward_consent,
        task_ids=list(outcome.task_ids),
        diff=outcome.diff,
        warnings=list(outcome.warnings),
        idempotent=outcome.idempotent,
        dispatching=dispatching,
        parallelism=parallelism,
    )


@v1.post("/partitions/{partition_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_partition_endpoint(
    partition_id: str,
    caller: token_service.TokenEntry = Depends(require_role("run")),
) -> PartitionCancelResponse:
    """Veto a proposed partition, or cooperatively cancel a dispatching one.

    Cross-tenant ⇒ 403, mirroring `POST /v1/runs/{run_id}/cancel`. A running
    agent is never killed -- `async_run_service.request_cancel` sets the
    cooperative flag its step loop checks at the next boundary.
    """
    from hivepilot.services import partition_service

    row = partition_service.get_partition(partition_id, tenant=None)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Partition not found")
    row_tenant = str(row.get("tenant") or "default")
    if caller.role != "admin" and row_tenant != caller.tenant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Cross-tenant cancel not allowed"
        )

    cancelled = partition_service.cancel_partition(
        partition_id, actor=f"api:{caller.role}", tenant=row_tenant
    )
    return PartitionCancelResponse(partition_id=partition_id, cancelled_tasks=list(cancelled))


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
    # `unpriceable_steps` is a scalar and belongs in the CSV. `unpriced_reasons`
    # is a dict — it stays JSON-only rather than being flattened into a column
    # nobody can parse.
    "unpriceable_steps",
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


@v1.get("/conversations")
@app.get("/conversations")
def conversations_runs(
    limit: int = 25,
    caller: token_service.TokenEntry = Depends(require_role("read")),
):
    """Runs that carry agent messages, newest first.

    A reader over `interactions`, which has been recording every stage's output
    with its role key all along -- this adds no capture, only a surface.
    """
    from hivepilot.services import conversations_service

    return {
        "runs": [
            {
                "run_id": r.run_id,
                "project": r.project,
                "started_at": r.started_at,
                "message_count": r.message_count,
                "roles": r.roles,
            }
            for r in conversations_service.recent_runs(limit=limit)
        ]
    }


@v1.get("/conversations/{run_id}")
@app.get("/conversations/{run_id}")
def conversations_thread(
    run_id: int,
    caller: token_service.TokenEntry = Depends(require_role("read")),
):
    """One run's thread, oldest message first. An unknown run is empty, not 404
    -- a stale id in an open tab must not break the page."""
    from hivepilot.services import conversations_service

    found = conversations_service.thread(run_id)
    return {
        "run_id": found.run_id,
        "roles": found.roles,
        "messages": [
            {
                "interaction_id": m.interaction_id,
                "actor": m.actor,
                "role": m.role,
                "action": m.action,
                "body": m.body,
                "at": m.at,
            }
            for m in found.messages
        ],
    }


@v1.post("/conversations/reply")
@app.post("/conversations/reply")
def conversations_reply(
    payload: ConversationReply,
    caller: token_service.TokenEntry = Depends(require_role("write")),
):
    """Record an operator instruction for a role, feeding its NEXT run.

    Not a message to a running agent: by the time a thread is readable its
    agents have exited. This appends to the role's corrections file, attributed
    to the operator -- a corpus that files an operator's instruction as the
    agent's own self-correction starts believing its own output.
    """
    from hivepilot.services import conversations_service

    try:
        path = conversations_service.reply(role=payload.role, text=payload.text)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"role": payload.role, "written_to": path}


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


@v1.get("/providers/fallbacks", dependencies=[Depends(require_role("read"))])
@app.get("/providers/fallbacks", dependencies=[Depends(require_role("read"))])
def provider_fallbacks_endpoint(hours: int = 24) -> dict:
    """Recent HP-70 provider fallbacks (HP-73), aggregated by source provider.
    Surfaces the otherwise invisible fallback signal: which runner fell over,
    how often, when last, and why (quota / unavailable). Provider fallback is a
    global infra fact (tenant-agnostic), so this is read-gated but not
    tenant-scoped."""
    from hivepilot.services import events

    hours = max(1, min(hours, 24 * 30))
    rows = events.recent("provider.fallback", hours=hours)
    agg: dict[str, dict[str, Any]] = {}
    for row in rows:  # rows are newest-first, so the first sighting is the latest
        payload = row.get("payload") or {}
        provider = payload.get("from") or row.get("entity_id") or "unknown"
        entry = agg.setdefault(
            provider,
            {
                "provider": provider,
                "count": 0,
                "last_at": None,
                "last_reason": None,
                "last_to": None,
            },
        )
        entry["count"] += 1
        if entry["last_at"] is None:
            entry["last_at"] = row.get("ts")
            entry["last_reason"] = payload.get("reason")
            entry["last_to"] = payload.get("to")
    providers = sorted(agg.values(), key=lambda e: (-e["count"], e["provider"]))
    return {"hours": hours, "providers": providers}


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


@v1.get("/analytics/whales")
@app.get("/analytics/whales")
def analytics_whales(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
    project: str | None = None,
    task: str | None = None,
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    """HP-81 — largest individual model steps by spend, then prompt tokens.

    Aggregates on `/v1/analytics/cost` hide a $1.50 / 300k-token call inside
    "claude · 30d". This list is the same envelopes `cost_summary` already
    meters — never prompt or completion bodies. Tenant-filtered via
    `_analytics_tenant` like every other analytics endpoint.
    """
    return analytics_service.cost_whales(
        tenant=_analytics_tenant(caller),
        days=days,
        project=project,
        task=task,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Pollen data endpoints sprint — GET /v1/models, GET /v1/efficiency.
# Same shape as the analytics endpoints above: Depends(require_role("read")),
# tenant-filtered via `_analytics_tenant` for /v1/models (run/step data);
# `/v1/efficiency`'s `headroom` half is tenant-scoped the same way, but its
# `rtk` half is intentionally NOT tenant-scoped — see
# `hivepilot.services.efficiency_service`'s module docstring for why (it's
# global, machine-level dev-tool telemetry, not hivepilot run/tenant data).
# ---------------------------------------------------------------------------


@v1.get("/models")
@app.get("/models")
def models_endpoint(
    days: int = 30,
    project: str | None = None,
    task: str | None = None,
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    """Per-model rollup (Pollen data endpoints sprint): cost, tokens, step
    count, success rate, share of spend, and an overall cost-per-successful
    -run figure — see `analytics_service.models_summary`'s docstring for the
    full contract (including why p50/p95 latency is honestly omitted rather
    than fabricated)."""
    return analytics_service.models_summary(
        tenant=_analytics_tenant(caller), days=days, project=project, task=task
    )


@v1.get("/sessions/cost")
@app.get("/sessions/cost")
def session_costs_endpoint(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(25, ge=1, le=200),
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    """Per-run cost split by what was billed — input, output, cache read,
    cache write.

    A total answers "how much" and cannot answer "where did it go". On this
    workload the intuitive reading of raw volume is the wrong one: one review
    dispatch recorded 516 982 cache-read tokens against 3 040 input and
    20 455 output. As volume, the reviewers look like they read too much; as
    cost, they write a lot and the reading is cached and cheap. Only the
    split distinguishes the two, and the wrong reading has already sent an
    optimisation effort at the wrong parameter once.

    Tenant-filtered via `_analytics_tenant` like every other analytics
    endpoint. `unpriced_steps` counts steps that plausibly cost something and
    could not be priced, so a partly-unpriceable session never looks cheap.
    """
    return analytics_service.session_costs(tenant=_analytics_tenant(caller), days=days, limit=limit)


@v1.get("/efficiency")
@app.get("/efficiency")
def efficiency_endpoint(
    days: int = 30,
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    """`{"headroom": <real dict, never null>, "rtk": <real dict or null>}`
    (Pollen data endpoints sprint) — see
    `hivepilot.services.efficiency_service`'s module docstring for the full
    investigation behind each source. Never 500s: `efficiency_summary`
    itself never raises (headroom is a zero-safe DB read, rtk is a
    best-effort shell-out that degrades to `None` on any failure)."""
    return efficiency_service.efficiency_summary(tenant=_analytics_tenant(caller), days=days)


# ---------------------------------------------------------------------------
# Mirador Agent Panels backend sprint — GET /v1/agents, GET /v1/lessons,
# GET /v1/verdicts. Same shape as /v1/models and /v1/efficiency above:
# Depends(require_role("read")), tenant-filtered via `_analytics_tenant`.
# `limit`/`days` are bounded (ge=1, sane le=) — closes the gap the
# unbounded `days: int = 30` params on the older analytics endpoints above
# have (see this sprint's spec: "follow the safest existing pattern",
# i.e. the `Query(50, ge=1, le=500)` convention already used by
# GET /v1/memory/evaluations and GET /v1/memory/journal).
# ---------------------------------------------------------------------------


#: A live state read is local socket I/O, so it is bounded like one. The
#: previous 15s default was a pipeline timeout wearing a dashboard's clothes.
_AGENT_PROBE_TIMEOUT_S = 2
#: Ceiling for the WHOLE roster probe. 22 roles x a per-call timeout is what
#: turns a hung backend into a blocked HTTP worker, and Pollen polls this view;
#: bounding each call is not bounding the request.
_AGENT_PROBE_BUDGET_S = 5.0


def _agent_surface_clock() -> float:
    """Monotonic seconds. A seam so the budget is testable without sleeping."""
    import time

    return time.monotonic()


def _agent_surface_run(argv: list[str], **kwargs: Any) -> Any:
    """Execute an agent-surface command. Seam so tests never spawn a process.

    argv only, never a shell string: the text carried here is operator- or
    agent-authored and will contain metacharacters.
    """
    import subprocess  # nosec B404 - argv built by hivepilot.services.agent_surface, never a shell

    return subprocess.run(  # nosec B603
        argv,
        capture_output=True,
        text=True,
        timeout=kwargs.get("timeout", _AGENT_PROBE_TIMEOUT_S),
    )


def _resolve_agent_surface() -> tuple[Any | None, str]:
    """`(driver, detail)`. A `None` driver always comes with a reason.

    "Not configured" and "configured but unreachable" are different answers and
    an operator acts on them differently -- collapsing them is the defect this
    whole series has been closing.
    """
    from hivepilot.services import agent_surface

    backend = (settings.agent_surface_backend or "").strip()
    if not backend:
        return None, (
            "no agent surface configured -- set HIVEPILOT_AGENT_SURFACE_BACKEND "
            "to 'herdr' or 'orca'"
        )
    try:
        return agent_surface.resolve_driver(backend), ""
    except ValueError as exc:
        return None, str(exc)


@v1.get("/agents/live", dependencies=[Depends(require_role("read"))])
@app.get("/agents/live", dependencies=[Depends(require_role("read"))])
def agents_live_endpoint(caller: Any = None) -> dict[str, Any]:
    """Per-role LIVE state, or `unknown` with a reason -- never a fabricated
    idle.

    `GET /v1/agents` answers what a role has DONE. This answers what it is
    doing right now, which is the question asked in front of the screen.
    """
    from hivepilot.services.agent_surface import AgentState

    driver, detail = _resolve_agent_surface()
    role_names = [r.name for r in roles.list_roles()]

    if driver is None:
        return {
            "configured": False,
            "detail": detail,
            "agents": [{"role": n, "state": AgentState.UNKNOWN.value} for n in role_names],
        }

    known = {s.value for s in AgentState}
    agents: list[dict[str, Any]] = []
    started = _agent_surface_clock()
    truncated = False
    for name in role_names:
        state = AgentState.UNKNOWN.value
        if truncated or _agent_surface_clock() - started >= _AGENT_PROBE_BUDGET_S:
            # Budget spent: the rest report `unknown`, the same honest answer
            # as any other probe failure -- and the caller is told why below
            # rather than being handed a roster that looks merely quiet.
            truncated = True
            agents.append({"role": name, "state": state})
            continue
        try:
            result = _agent_surface_run(
                driver.read_argv(name, limit=1), timeout=_AGENT_PROBE_TIMEOUT_S
            )
            if getattr(result, "returncode", 1) == 0:
                candidate = (getattr(result, "stdout", "") or "").strip().lower()
                # An unrecognised string must NOT reach the UI as a state: a
                # backend that changes its vocabulary would otherwise smuggle
                # something we do not model into the dashboard.
                state = candidate if candidate in known else AgentState.UNKNOWN.value
        except Exception as exc:  # noqa: BLE001 - a probe must never 500 the roster
            from hivepilot.utils.logging import get_logger as _gl

            _gl(__name__).warning("api.agents_live.probe_failed", role=name, error=str(exc))
        agents.append({"role": name, "state": state})

    detail_out = (
        f"probe stopped after {_AGENT_PROBE_BUDGET_S:g}s; the remaining roles are unknown"
        if truncated
        else ""
    )
    return {"configured": True, "detail": detail_out, "agents": agents}


@v1.post("/agents/{role}/message", dependencies=[Depends(require_role("run"))])
@app.post("/agents/{role}/message", dependencies=[Depends(require_role("run"))])
def agent_message_endpoint(
    role: str, payload: dict[str, Any], caller: Any = None
) -> dict[str, Any]:
    """Inject text into a live agent.

    Gated on `run`, not `read`: this makes something happen.

    Reports `dispatched`, never `delivered`. The send is fire-and-forget at
    this layer, and claiming the agent received it would be a claim we cannot
    make.
    """
    text = str((payload or {}).get("text") or "").strip()
    if not text:
        return {"dispatched": False, "detail": "empty message"}

    driver, detail = _resolve_agent_surface()
    if driver is None:
        return {"dispatched": False, "detail": detail}

    try:
        result = _agent_surface_run(driver.send_argv(role, text))
    except Exception as exc:  # noqa: BLE001
        from hivepilot.utils.logging import get_logger as _gl

        _gl(__name__).warning("api.agent_message.failed", role=role, error=str(exc))
        return {"dispatched": False, "detail": "the agent surface command failed"}

    if getattr(result, "returncode", 1) != 0:
        return {"dispatched": False, "detail": "the agent surface command failed"}
    return {"dispatched": True, "role": role}


@v1.get("/agents")
@app.get("/agents")
def agents_endpoint(
    days: int | None = Query(None, ge=1, le=3650),
    project: str | None = None,
    task: str | None = None,
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    """Per-role agent activity roster (Pollen Agent Panels backend
    sprint): the full role roster (`hivepilot.roles.list_roles()`)
    LEFT-JOINed with real per-role activity derived from `steps.role` — see
    `analytics_service.agents_summary`'s docstring for the full honesty
    contract (unattributed roles, the NULL-role "unknown" bucket, no
    fabricated latency)."""
    return analytics_service.agents_summary(
        tenant=_analytics_tenant(caller), days=days, project=project, task=task
    )


@v1.get("/lessons")
@app.get("/lessons")
def lessons_endpoint(
    role: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    """Recent lessons, optionally filtered by `role`, plus a per-role
    aggregation (total / validated count / average score) — see
    `analytics_service.lessons_summary`'s docstring. Tenant-scoped via
    `state_service.list_lessons_by_tenant`'s fail-closed `LEFT JOIN runs`
    (never `list_lessons`, which stays project-required/tenant-unaware for
    its own existing callers)."""
    return analytics_service.lessons_summary(
        tenant=_analytics_tenant(caller), role=role, limit=limit
    )


@v1.get("/verdicts")
@app.get("/verdicts")
def verdicts_endpoint(
    role: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    """Recent verdicts, optionally filtered by `role`, plus a per-role
    decision/kind aggregation — see `analytics_service.verdicts_summary`'s
    docstring. Tenant-scoped via `state_service.list_verdicts`'s
    fail-closed `LEFT JOIN runs` (never `list_recent_verdicts`, which stays
    unfiltered-by-tenant for its own existing caller)."""
    return analytics_service.verdicts_summary(
        tenant=_analytics_tenant(caller), role=role, limit=limit
    )


# ---------------------------------------------------------------------------
# Autopilot (guarded objective queue + fail-closed dispatch gate) — read +
# control surface for Pollen. Backed by `hivepilot.services.autopilot_queue`
# (the same service the `autopilot` CLI command group wraps) and the
# project-independent "default" block of `hivepilot.services.autopilot_policy`.
#
# **Tenant-lock, stated honestly.** `autopilot_queue`'s queue/control tables
# (and therefore `GET /v1/autopilot`) ARE genuinely tenant-scoped — a
# non-admin caller only ever sees/controls their own token's tenant (a
# mismatched `?tenant=` is rejected, never silently ignored or overridden;
# see `_resolve_autopilot_tenant`). But the ONE thing that actually turns a
# queued row into a real pipeline run — the scheduler's `source: autopilot`
# entry (`schedule_service.run_entry`) — hardcodes `tenant="default"` when it
# calls `autopilot_queue.drain_one`. So a non-`"default"` tenant's queue can
# accumulate `proposed`/`queued` rows via the API/CLI, but nothing ever
# drains them automatically. This endpoint does not hide that: an admin with
# no explicit `?tenant=` sees the `"default"` tenant (the one the drain
# actually acts on), not a fabricated all-tenants aggregate.
#
# **Real vs. null, field by field.** `queue`/`queue_depth`/`recent_dispatches`/
# `paused` are always real (sourced straight from the `autopilot_queue`/
# `autopilot_control` tables — empty tables just mean empty/`False`, never a
# 500). `budget_daily_usd`/`auto_dispatch_allowlist` come from the
# project-independent "default" block of `policies.yaml` (this endpoint has
# no `project` scope, so per-project overrides in
# `policies.projects.<name>.{budget_daily_usd,auto_dispatch}` are NOT
# reflected — the CLI's `autopilot status` has the same project-agnostic
# view). `budget_daily_usd` is `None` whenever no positive daily budget is
# configured (mirrors `AutopilotPolicy`'s own fail-closed "no budget
# configured" contract) — never fabricated as unlimited or zero.
# `budget_remaining` is `None` iff `budget_daily_usd` is, else
# `max(budget_daily_usd - budget_spent_today, 0.0)` (a budget that's already
# been exceeded reports `0.0` remaining, never negative).
#
# `budget_spent_today`/`budget_remaining` on a spend-lookup FAILURE: both
# report `None` ("unknown"), never a fabricated `0.0`/full-budget pair. A
# silent `0.0` here would be actively misleading during an analytics outage
# (looks like "nothing spent, full budget left" when the real spend is
# simply unknown) -- this mirrors `drain_one`'s own fail-closed handling of
# the same lookup (it denies dispatch on the identical failure), just
# surfaced as "unknown" instead of a number for a read-only dashboard.
# ---------------------------------------------------------------------------


def _resolve_autopilot_tenant(caller: token_service.TokenEntry, tenant: str | None) -> str:
    """Resolve which tenant's autopilot state a caller may see/control.

    Non-admin callers are ALWAYS locked to their own token's tenant — an
    explicit `?tenant=` that disagrees is rejected (403), never silently
    overridden and never silently ignored (either would hide a real
    cross-tenant access attempt, mirroring `cancel_run`'s tenant check).
    Admin callers may pass any `tenant`; omitted defaults to `"default"` —
    the ONLY tenant the schedule-driven drain ever actually dispatches for
    today (see the module comment above), so `"default"` is the honest
    default view, not an arbitrary choice.
    """
    if caller.role != "admin":
        if tenant is not None and tenant != caller.tenant:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cross-tenant autopilot access not allowed",
            )
        return caller.tenant
    return tenant or "default"


_AUTOPILOT_PENDING_STATES = ("proposed", "queued", "running")
_AUTOPILOT_DISPATCHED_STATES = ("done", "blocked")
_AUTOPILOT_RECENT_DISPATCH_LIMIT = 20
# Sentinel project name for resolving the project-independent "default"
# policy block only — guaranteed to never collide with a real project name
# (real project names are config-declared identifiers, never the empty
# string), so `autopilot_policy.get_autopilot_policy("")`'s project-override
# merge step always contributes nothing, leaving only `policies.default`.
_AUTOPILOT_NO_PROJECT_SENTINEL = ""


class AutopilotQueueItem(BaseModel):
    id: int
    pipeline: str
    project: str
    reason: str | None
    state: str
    enqueued_at: str


class AutopilotDispatch(BaseModel):
    pipeline: str
    project: str
    outcome: str
    at: str


class AutopilotStateResponse(BaseModel):
    tenant: str
    paused: bool
    queue: list[AutopilotQueueItem]
    queue_depth: int
    budget_daily_usd: float | None
    budget_spent_today: float | None
    budget_remaining: float | None
    recent_dispatches: list[AutopilotDispatch]
    auto_dispatch_allowlist: list[str]


class AutopilotControlResponse(BaseModel):
    tenant: str
    paused: bool


def _autopilot_state(tenant: str) -> AutopilotStateResponse:
    items = autopilot_queue.list_queue(tenant=tenant)
    queue = [
        AutopilotQueueItem(
            id=item.id,
            pipeline=item.pipeline,
            project=item.project,
            reason=item.reason,
            state=item.state,
            enqueued_at=item.created_ts,
        )
        for item in items
        if item.state in _AUTOPILOT_PENDING_STATES
    ]
    dispatched = [item for item in items if item.state in _AUTOPILOT_DISPATCHED_STATES]
    dispatched.sort(key=lambda item: item.updated_ts, reverse=True)
    recent_dispatches = [
        AutopilotDispatch(
            pipeline=item.pipeline, project=item.project, outcome=item.state, at=item.updated_ts
        )
        for item in dispatched[:_AUTOPILOT_RECENT_DISPATCH_LIMIT]
    ]

    policy = autopilot_policy.get_autopilot_policy(_AUTOPILOT_NO_PROJECT_SENTINEL)
    spent: float | None
    try:
        spent = autopilot_queue.spent_today_usd(tenant=tenant)
    except Exception:  # noqa: BLE001 - lookup failure -> "unknown", never a fabricated 0.0
        spent = None
    if policy.budget_daily_usd is None or spent is None:
        remaining = None
    else:
        remaining = max(policy.budget_daily_usd - spent, 0.0)

    return AutopilotStateResponse(
        tenant=tenant,
        paused=autopilot_queue.is_paused(tenant=tenant),
        queue=queue,
        queue_depth=len(queue),
        budget_daily_usd=policy.budget_daily_usd,
        budget_spent_today=spent,
        budget_remaining=remaining,
        recent_dispatches=recent_dispatches,
        auto_dispatch_allowlist=policy.auto_dispatch,
    )


@v1.get("/autopilot")
@app.get("/autopilot")
def get_autopilot(
    tenant: str | None = None,
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> AutopilotStateResponse:
    """Real-or-honest-empty Autopilot state for Pollen. See the module
    comment block above for the tenant-lock and real-vs-null contract."""
    resolved_tenant = _resolve_autopilot_tenant(caller, tenant)
    return _autopilot_state(resolved_tenant)


@v1.post("/autopilot/pause")
@app.post("/autopilot/pause")
def post_autopilot_pause(
    tenant: str | None = None,
    caller: token_service.TokenEntry = Depends(require_role("run")),
) -> AutopilotControlResponse:
    """Pause the autopilot drain for a tenant. Gated at `run` (a control
    action, like `POST /v1/runs/{id}/cancel` — never `read`). Idempotent:
    pausing an already-paused tenant is a no-op success, matching
    `autopilot_queue.pause`'s own upsert semantics."""
    resolved_tenant = _resolve_autopilot_tenant(caller, tenant)
    autopilot_queue.pause(tenant=resolved_tenant)
    return AutopilotControlResponse(tenant=resolved_tenant, paused=True)


@v1.post("/autopilot/resume")
@app.post("/autopilot/resume")
def post_autopilot_resume(
    tenant: str | None = None,
    caller: token_service.TokenEntry = Depends(require_role("run")),
) -> AutopilotControlResponse:
    """Resume the autopilot drain for a tenant (also clears the `stopped`
    flag, mirroring the CLI's `autopilot resume`). Gated at `run`. Idempotent:
    resuming an already-running tenant is a no-op success."""
    resolved_tenant = _resolve_autopilot_tenant(caller, tenant)
    autopilot_queue.resume(tenant=resolved_tenant)
    return AutopilotControlResponse(tenant=resolved_tenant, paused=False)


# ---------------------------------------------------------------------------
# Memory-quality instrumentation subsystem — backs Pollen's Memory > Quality view.
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
#
# **Known limitation (single-tenant events today).** These endpoints
# themselves are correctly tenant-scoped (see above), but the ONLY current
# writer — `plugins/mem0.py`'s `recall`/`store` hooks — has no tenant signal
# reachable in its hook payload/kwargs (`RunnerPayload` and `TaskConfig` carry
# no `tenant` field; see `plugins/mem0.py`'s instrumentation call sites for
# the full investigation) and so every event lands under `tenant="default"`.
# In a multi-tenant deployment, a non-admin caller whose own tenant isn't
# `"default"` will therefore see an empty Memory > Quality view even once mem0 is
# active, until a real tenant signal is threaded down to the hook — that is
# NOT a bug in the scoping here, it's a gap in what the writer can attribute
# today.
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
    limit: int = Query(50, ge=1, le=500),
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    return {
        "evaluations": memory_service.recent_evaluations(tenant=_memory_tenant(caller), limit=limit)
    }


@v1.get("/memory/journal")
@app.get("/memory/journal")
def memory_journal(
    limit: int = Query(50, ge=1, le=500),
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    return {"journal": memory_service.activity_journal(tenant=_memory_tenant(caller), limit=limit)}


@v1.get("/memory/growth")
@app.get("/memory/growth")
def memory_growth(
    days: int = 30,
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    """Memory growth aggregates (Pollen data endpoints sprint) — see
    `memory_service.growth_summary`'s docstring for the full contract,
    including why `authorship` (human vs. agent) is always `None` rather
    than a fabricated split."""
    return memory_service.growth_summary(tenant=_memory_tenant(caller), days=days)


class MemoryEvaluationRequest(BaseModel):
    namespace: str = Field(..., min_length=1, max_length=200)
    useful: bool
    ref_key: str | None = Field(None, max_length=200)
    note: str | None = Field(None, max_length=2000)

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
# Pollen web UI surface (Sprint 1) — plugin health + mem0 memory search.
# Both are read-only. Sibling to the analytics endpoints above, but NEITHER
# is tenant-scoped: plugin health is process-global state (no per-tenant
# concept applies), and mem0 memories have no tenant->project mapping to
# filter by (see `list_memories`'s docstring for the full scope analysis).
# ---------------------------------------------------------------------------


def _plugin_activity_payload(name: str, *, tenant: str | None) -> dict[str, Any] | None:
    activity = plugin_activity.activity_for(name, tenant=tenant)
    return activity.as_dict() if activity is not None else None


@v1.get("/health/probes", dependencies=[Depends(require_role("read"))])
@app.get("/health/probes", dependencies=[Depends(require_role("read"))])
def health_probes_endpoint() -> dict[str, Any]:
    """Two probes for the things that fail by GOING QUIET.

    `/plugins/health` reports what loaded. These report whether two systems
    that produce continuously are still producing -- the failure mode nothing
    else here can see, because an absence looks exactly like a healthy zero.

    `agent_surface`: is a live-agent backend configured, and does it answer?
    Empty is the default and is reported as `not_configured`, never as a
    fault -- a red badge on every deployment that never asked for the feature
    teaches people to ignore the badge.

    `otel`: is telemetry still ARRIVING? The row count stays healthy forever
    once an exporter has ever worked; only the age of the newest row says
    whether it still does. `never_arrived` and `stale` are different answers
    on purpose -- the first points at configuration, the second at something
    that used to work.

    Never raises. A probe that can 500 a dashboard panel is worse than one
    that reports `unknown`.
    """
    import subprocess

    from hivepilot.services import system_probes
    from hivepilot.utils.logging import get_logger

    # A LOCAL structlog logger: the module-level `logger` here is the stdlib
    # one and takes no keyword fields, which mypy caught rather than a test.
    probe_logger = get_logger(__name__)

    def _run(argv: list[str]) -> int:
        return subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=5
        ).returncode

    try:
        surface = system_probes.probe_agent_surface(
            backend=getattr(settings, "agent_surface_backend", "") or "",
            run_cli=_run,
        )
    except Exception as exc:  # noqa: BLE001
        probe_logger.warning("health.agent_surface_probe_failed", error=str(exc))
        surface = {"state": "unknown", "backend": None}

    try:
        rows, newest = state_service.agent_telemetry_freshness()
        otel = system_probes.probe_otel_arrival(rows=rows, newest=newest)
    except Exception as exc:  # noqa: BLE001
        probe_logger.warning("health.otel_probe_failed", error=str(exc))
        otel = {"state": "unknown", "rows": None, "age_hours": None}

    return {"agent_surface": surface, "otel": otel}


@v1.get("/plugins/health")
@app.get("/plugins/health")
def plugins_health_endpoint(
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
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

    **`activity` is a second, independent answer** (see
    `hivepilot.services.plugin_activity`). `status`/`detail` say whether a
    plugin is installed and configured; `activity` says whether it has
    actually run. Both `headroom` and `mem0` reported `status="ok"` for weeks
    while failing every call, so a green badge alone must not be read as
    "working".

    Two fields, because "no reading" has two distinct causes the operator
    needs told apart:

    - `activity_available=false` — the plugin writes no telemetry (`rtk`,
      `gh` are PATH checks). The health check above is presence-only and the
      UI must say so rather than imply more.
    - `activity_available=true` with `activity=null` — measurable, but the
      read failed. Distinct from `activity.events == 0`, which is a real
      reading meaning "measured, and it has done nothing".

    Unlike the health fields, **`activity` IS tenant-partitioned**: it comes
    from `headroom_*`/`memory_events`, which carry a tenant column, so it is
    scoped via `_analytics_tenant` like the analytics endpoints. Serving one
    tenant's activity timestamps to another would leak their run cadence.
    """
    manager = _get_orchestrator().plugins
    results = manager.check_all()
    tenant = _analytics_tenant(caller)
    measurable = plugin_activity.probed_plugins()
    return {
        "plugins": [
            {
                "name": name,
                "status": health.status,
                "detail": health.detail,
                "activity_available": name in measurable,
                "activity": _plugin_activity_payload(name, tenant=tenant),
            }
            for name, health in sorted(results.items())
        ],
        "disabled": sorted(settings.plugins_disabled),
        "denied": _denied_plugins_payload(manager),
        "not_installed": _uninstalled_plugins_payload(),
    }


def _denied_plugins_payload(manager: Any) -> list[dict[str, Any]]:
    """Plugins that are enabled and installed and did NOT load.

    The third state, and the one with no surface anywhere until now.
    `check_all()` only covers REGISTERED plugins, and a capability-denied
    plugin is rolled back before registration -- so an operator could enable
    it, see the toggle succeed, and find it in neither the healthy list nor
    the disabled list. It simply was not there.

    Observed live: `token_savior` loads under the services' capability policy
    and is denied under a CLI environment that lacks it. Same plugin, same
    flag, opposite outcome, and the UI showed the same thing in both cases --
    nothing.
    """
    denied = getattr(manager, "denied", None) or []
    payload: list[dict[str, Any]] = []
    for record, error in denied:
        payload.append(
            {
                "name": getattr(record, "name", "?"),
                "source": getattr(record, "source", None),
                "error": error,
                "remediation": (
                    "add the declared capability to HIVEPILOT_PLUGINS_CAPABILITY_POLICY "
                    "(plain or CSV, e.g. HIVEPILOT_PLUGINS_CAPABILITY_POLICY=subprocess), "
                    "then restart"
                ),
            }
        )
    return sorted(payload, key=lambda p: str(p["name"]))


def _uninstalled_plugins_payload() -> list[str]:
    """Curated plugins that exist in the repo but are not on this host.

    Plugins are not shipped in the wheel, so a merge does not install them.
    Reporting only what IS installed answers "what is on" while hiding "what
    exists" -- which is how ~23 written plugins sat inert here unnoticed.
    """
    try:
        from hivepilot.services import plugin_installer as pi

        return sorted(name for name in pi.KNOWN_EXAMPLE_PLUGINS if not pi.is_installed(name))
    except Exception:  # noqa: BLE001 - never break the health endpoint
        return []


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


# ---------------------------------------------------------------------------
# GET /v1/plugins/catalog + POST /v1/plugins/{name}/install
#
# `/plugins/health` reports what LOADED. A browsable plugin page needs what
# EXISTS: ~23 of the curated plugins are written and not installed here, and
# that is precisely the set an operator wants to look through and turn on.
#
# The install endpoint writes executable Python onto the host, so it is
# admin-gated and restricted to `KNOWN_EXAMPLE_PLUGINS` -- the same closed
# registry the CLI uses, validated BEFORE any fetch. There is no
# arbitrary-name or arbitrary-URL path.
#
# It deliberately does NOT run `pip install`. Plugin prerequisites are the
# operator's to install: a `pip install` triggered from a web switch runs
# arbitrary package code as the service user, and a heavy one has wedged this
# project's production host before. The prerequisite is REPORTED
# (`prereq_detail`) so the page can show the exact command instead.
# ---------------------------------------------------------------------------


class PluginCatalogEntry(BaseModel):
    name: str
    description: str
    prereq_kind: str
    prereq_detail: str
    installed: bool
    enabled: bool
    env_flag: str


class PluginInstallResponse(BaseModel):
    name: str
    installed_to: str
    enabled: bool
    restart_required: bool
    prereq_detail: str


@v1.get("/plugins/catalog")
@app.get("/plugins/catalog")
def plugins_catalog_endpoint(
    caller: token_service.TokenEntry = Depends(require_role("read")),
) -> dict[str, Any]:
    """The curated plugin registry with per-plugin install/enable state.

    Metadata only -- descriptions, prerequisites and two booleans. No secret,
    no token, no resolved value, which is why `read` suffices here while
    toggle and install require `admin`.
    """
    from hivepilot.services import plugin_installer as pi

    entries = [
        PluginCatalogEntry(
            name=name,
            description=spec.description,
            prereq_kind=str(spec.prereq_kind),
            prereq_detail=spec.prereq_detail,
            installed=pi.is_installed(name),
            enabled=pi.is_enabled(name),
            env_flag=spec.env_flag,
        ).model_dump()
        for name, spec in sorted(pi.KNOWN_EXAMPLE_PLUGINS.items())
    ]
    return {"plugins": entries}


# ---------------------------------------------------------------------------
# HP-76 — MCP command center
# ---------------------------------------------------------------------------


class McpImportRequest(BaseModel):
    text: str


class McpCatalogAddRequest(BaseModel):
    name: str


@v1.get("/mcp/servers", dependencies=[Depends(require_role("read"))])
@app.get("/mcp/servers", dependencies=[Depends(require_role("read"))])
def mcp_servers_endpoint() -> dict:
    """Installed MCP servers + last probe. Stale probes refresh on read
    (60s TTL) so the page stays current without a dedicated scheduler."""
    from hivepilot.services import mcp_probe

    servers = mcp_probe.refresh_stale()
    return {
        "servers": servers,
        "cost_note": (
            "MCP tool calls are not metered yet — HP-73 tracks LLM providers, "
            "not MCP servers. cost_usd is always null here."
        ),
    }


@v1.get("/mcp/catalog", dependencies=[Depends(require_role("read"))])
@app.get("/mcp/catalog", dependencies=[Depends(require_role("read"))])
def mcp_catalog_endpoint() -> dict:
    from hivepilot.services import mcp_registry

    return {"catalog": mcp_registry.catalog()}


@v1.post("/mcp/import")
@app.post("/mcp/import")
def mcp_import_endpoint(
    payload: McpImportRequest,
    _caller: token_service.TokenEntry = Depends(require_role("admin")),
) -> dict:
    """Paste-anything import (JSON / URL / command). Admin-only. Never
    fetches a URL; literal env values are stripped."""
    from hivepilot.services import mcp_registry

    if not payload.text.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty paste")
    try:
        return mcp_registry.import_and_save(payload.text)
    except mcp_registry.McpImportError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@v1.post("/mcp/catalog/add")
@app.post("/mcp/catalog/add")
def mcp_catalog_add_endpoint(
    payload: McpCatalogAddRequest,
    _caller: token_service.TokenEntry = Depends(require_role("admin")),
) -> dict:
    from hivepilot.services import mcp_registry

    try:
        server = mcp_registry.add_from_catalog(payload.name)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown catalog entry '{payload.name}'"
        ) from None
    return {"server": server}


@v1.post("/mcp/servers/{server_id}/probe", dependencies=[Depends(require_role("read"))])
@app.post("/mcp/servers/{server_id}/probe", dependencies=[Depends(require_role("read"))])
def mcp_probe_endpoint(server_id: int) -> dict:
    from hivepilot.services import mcp_probe

    row = mcp_probe.probe_and_store(server_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown server")
    return {"server": row}


@v1.delete("/mcp/servers/{server_id}")
@app.delete("/mcp/servers/{server_id}")
def mcp_delete_endpoint(
    server_id: int, _caller: token_service.TokenEntry = Depends(require_role("admin"))
) -> dict:
    from hivepilot.services import state_service as _state

    if not _state.delete_mcp_server(server_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown server")
    return {"deleted": server_id}


@v1.get("/agents/admin")
@app.get("/agents/admin")
def list_agents_admin_endpoint(
    caller: token_service.TokenEntry = Depends(require_role("admin")),
) -> dict:
    """Every curated agent kind with its capabilities and the SERVICE's view.

    `on_service_path` is THIS process's `shutil.which` — the view that decides
    whether a runner registers. A binary "installed" by a login shell but
    False here is the grok trap: the per-user installer landed it somewhere
    the units' PATH does not reach.
    """
    from hivepilot.services import agent_admin

    return {"agents": agent_admin.list_agents_admin()}


class AgentActionRequest(BaseModel):
    """`consent` is the button's signature on the decision — the
    non-interactive replacement for `agent_install.py`'s TTY "yes". It must be
    EXPLICITLY true; absent-means-no is the only safe default for a field that
    authorises running a vendor's install pipeline."""

    consent: bool = False


@v1.post("/agents/{kind}/{action}")
@app.post("/agents/{kind}/{action}")
def agent_action_endpoint(
    kind: str,
    action: str,
    body: AgentActionRequest,
    caller: token_service.TokenEntry = Depends(require_role("admin")),
) -> dict:
    """Install or update ONE agent binary, on explicit admin consent.

    The REPLACEMENT for `agent_install.py`'s interactive guard, not a bypass:
    admin role + `consent: true` + an audit row carrying the actor and the
    version before/after. Only registry constants ever execute — the kind is
    validated inside the service before anything runs, so no URL or command
    can arrive from the UI. Never called by a run.
    """
    from hivepilot.services import agent_admin

    if body.consent is not True:
        raise HTTPException(
            status_code=400,
            detail=(
                'consent is required: POST {"consent": true} to authorise the '
                f"{action} — the button's signature on the decision."
            ),
        )
    try:
        return agent_admin.perform_agent_action(
            kind,
            action,
            actor=caller.note or caller.role,
            token_hash=caller.token[:16],
        )
    except agent_admin.AgentAdminError as exc:
        # An operator mistake (unknown kind, docs-only install, no verified
        # updater), not a server fault — the distinction decides what Pollen
        # shows.
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@v1.post("/agents/{kind}/login")
@app.post("/agents/{kind}/login")
def agent_login_endpoint(
    kind: str,
    body: AgentActionRequest,
    caller: token_service.TokenEntry = Depends(require_role("admin")),
) -> dict:
    """Start *kind*'s VERIFIED headless login and return the URL to open (#33).

    The grok/cursor flow made one click: the login runs detached AS THE
    SERVICE, prints its validation URL, and the token lands in the service
    home when the operator opens it — born on the box, never transported.

    Same consent shape as install/update: admin role + `consent: true`. The
    response carries the URL ONLY — never the log's other lines (some CLIs
    echo token material on success), and the audit row records THAT a login
    started, never anything from the flow.
    """
    from hivepilot.services import agent_auth

    if body.consent is not True:
        raise HTTPException(
            status_code=400,
            detail='consent is required: POST {"consent": true} to start the login.',
        )
    try:
        result = agent_auth.start_headless_login(kind)
    except agent_auth.AgentAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state_service.record_audit(
        token_hash=caller.token[:16],
        role=f"agent-admin:{caller.note or caller.role}",
        endpoint=f"/v1/agents/{kind}/login",
        method="POST",
        result="login started",
        tenant=caller.tenant,
    )
    # url may be None: the flow printed nothing URL-shaped in the window. The
    # log path is on-box only — useful to an operator, useless to an attacker.
    return {"kind": kind, "url": result["url"], "log": result["log"]}


@v1.post("/plugins/{name}/install")
@app.post("/plugins/{name}/install")
def install_plugin_endpoint(
    name: str,
    caller: token_service.TokenEntry = Depends(require_role("admin")),
) -> PluginInstallResponse:
    """Fetch a CURATED plugin file onto this host and persist its enable flag.

    Mirrors `hivepilot plugins install <name> --yes`. The name is validated
    against `KNOWN_EXAMPLE_PLUGINS` before anything is fetched -- this writes
    Python that later runs in-process, so an unvalidated name must never
    reach the fetcher.

    Does NOT install the plugin's own prerequisites (a pip package, a
    binary). Those are returned as `prereq_detail` for the caller to show.
    """
    from hivepilot.services import plugin_installer as pi

    spec = pi.KNOWN_EXAMPLE_PLUGINS.get(name)
    if spec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown plugin")

    with _plugin_toggle_lock:
        try:
            dest = pi.fetch_plugin(name)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"could not fetch plugin {name!r}: {type(exc).__name__}",
            ) from None
        pi.persist_enabled(name)

    return PluginInstallResponse(
        name=name,
        installed_to=str(dest),
        enabled=True,
        # `PluginManager` scans once at construction, so a freshly installed
        # plugin is inert until the process restarts. A UI implying otherwise
        # would have the operator hunting a plugin that is on disk, enabled,
        # and doing nothing.
        restart_required=True,
        prereq_detail=spec.prereq_detail,
    )


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


# ---------------------------------------------------------------------------
# Agent Studio (HP-25) — store-backed roles CRUD. The mutable roster the visual
# builder (Phase 2) and NL authoring (Phase 3) drive. Reads are `read`-gated;
# writes are `admin`-gated, validated against the Role schema, guarded against
# self-granted dangerous capabilities, and applied live via `refresh_roles()`.
# Writes first `seed_store_from_yaml()` so the store holds the WHOLE roster
# before the first edit (never a single-role store that drops the rest).
# ---------------------------------------------------------------------------


class RoleWrite(BaseModel):
    """Create/update payload for a role. A role needs either `prompt_text`
    (inline, stored in the DB — Agent Studio default) or `prompt_file`."""

    name: str
    title: str
    model_profile: str
    inputs: list[str]
    outputs: list[str]
    can_block: bool
    order: int
    prompt_text: str | None = None
    prompt_file: str | None = None
    display_name: str | None = None
    runner: str | None = None
    model: str | None = None
    models: list[str] | None = None
    optional_inputs: list[str] | None = None
    allowed_tools: list[str] | None = None
    permission_mode: str | None = None
    command_task: str | None = None
    host: str | None = None
    effort: str | None = None


def _apply_role_write(payload: RoleWrite) -> dict:
    if (
        payload.permission_mode == "bypassPermissions"
        and not settings.allow_dangerous_role_capabilities
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "role with permission_mode='bypassPermissions' is refused (fail-closed) — "
                "set HIVEPILOT_ALLOW_DANGEROUS_ROLE_CAPABILITIES=1 to permit it"
            ),
        )
    if not (payload.prompt_text or payload.prompt_file):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a role needs prompt_text or prompt_file",
        )
    try:
        roles.validate_role_fields(payload.model_dump(exclude_none=True))
    except Exception as exc:  # noqa: BLE001 — surface schema errors as 400
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid role: {exc}"
        ) from exc

    from hivepilot.services import state_service

    with _orch_lock:
        roles.seed_store_from_yaml()  # adopt the full YAML roster before editing
        row = payload.model_dump()
        row["tenant"] = "default"
        state_service.upsert_role(row)
        roles.refresh_roles()
        stored = state_service.get_role_row(payload.name)
    result = stored or row
    result.pop("updated_at", None)
    return result


@v1.get("/roles")
@app.get("/roles")
def list_roles_endpoint(caller: token_service.TokenEntry = Depends(require_role("read"))):
    return {"roles": roles.api_roster()}


@v1.get("/roles/{name}")
@app.get("/roles/{name}")
def get_role_endpoint(name: str, caller: token_service.TokenEntry = Depends(require_role("read"))):
    for row in roles.api_roster():
        if row.get("name") == name:
            return row
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no role '{name}'")


@v1.post("/roles", dependencies=[Depends(require_role("admin"))])
@app.post("/roles", dependencies=[Depends(require_role("admin"))])
def create_role_endpoint(payload: RoleWrite) -> dict:
    from hivepilot.services import state_service

    roles.seed_store_from_yaml()
    if state_service.get_role_row(payload.name) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"role '{payload.name}' already exists"
        )
    return _apply_role_write(payload)


@v1.put("/roles/{name}", dependencies=[Depends(require_role("admin"))])
@app.put("/roles/{name}", dependencies=[Depends(require_role("admin"))])
def update_role_endpoint(name: str, payload: RoleWrite) -> dict:
    if payload.name != name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="path name must match payload name"
        )
    return _apply_role_write(payload)


@v1.delete("/roles/{name}", dependencies=[Depends(require_role("admin"))])
@app.delete("/roles/{name}", dependencies=[Depends(require_role("admin"))])
def delete_role_endpoint(name: str) -> dict:
    from hivepilot.services import state_service

    with _orch_lock:
        roles.seed_store_from_yaml()
        existed = state_service.get_role_row(name) is not None
        state_service.delete_role(name)
        roles.refresh_roles()
    return {"deleted": existed, "name": name}


# ---------------------------------------------------------------------------
# Espaces (HP-45, Cycle 1 · P2) — conversation rooms. A space has >=1
# participant, each a human or a role, so it models a human<->agent DM AND an
# agent<->agent room. Reads are `read`-gated; creating a space or posting a
# message is `run`-gated. Every posted message is announced on the realtime bus
# (HP-40) so subscribers (HP-41 SSE) see it live. Tenant-scoped: a non-admin
# only ever sees/uses its own tenant's spaces.
# ---------------------------------------------------------------------------


class SpaceParticipant(BaseModel):
    type: str  # "human" | "role"
    id: str | None = None


class SpaceCreate(BaseModel):
    participants: list[SpaceParticipant]
    kind: str = "dm"
    title: str | None = None


class SpaceMessageCreate(BaseModel):
    body: str
    sender_type: str = "human"
    sender_id: str | None = None


def _space_tenant_or_404(space_id: int, caller: token_service.TokenEntry) -> dict:
    from hivepilot.services import state_service

    space = state_service.get_space(space_id, tenant=caller.tenant)
    if space is None and caller.role == "admin":
        # Admin may address any tenant's space — look it up tenant-free.
        for candidate in state_service.list_spaces():
            if int(candidate["id"]) == space_id:
                space = candidate
                break
    if space is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no space {space_id}")
    return space


@v1.get("/spaces")
@app.get("/spaces")
def list_spaces_endpoint(caller: token_service.TokenEntry = Depends(require_role("read"))):
    from hivepilot.services import state_service

    tenant = None if caller.role == "admin" else caller.tenant
    return {"spaces": state_service.list_spaces(tenant=tenant)}


@v1.post("/spaces")
@app.post("/spaces")
def create_space_endpoint(
    payload: SpaceCreate, caller: token_service.TokenEntry = Depends(require_role("run"))
) -> dict:
    from hivepilot.services import state_service

    if not payload.participants:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="a space needs >=1 participant"
        )
    known_roles = {role.name for role in roles.list_roles()}
    for participant in payload.participants:
        if participant.type not in ("human", "role"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"participant type must be 'human' or 'role', got {participant.type!r}",
            )
        if participant.type == "role" and (participant.id or "") not in known_roles:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown role participant {participant.id!r}",
            )
    space_id = state_service.create_space(
        [p.model_dump() for p in payload.participants],
        kind=payload.kind,
        title=payload.title,
        tenant=caller.tenant,
    )
    from hivepilot.services import events

    events.emit(
        "space.created", "space", space_id, tenant=caller.tenant, payload={"space_id": space_id}
    )
    return state_service.get_space(space_id, tenant=caller.tenant) or {"id": space_id}


@v1.get("/spaces/{space_id}")
@app.get("/spaces/{space_id}")
def get_space_endpoint(
    space_id: int, caller: token_service.TokenEntry = Depends(require_role("read"))
) -> dict:
    return _space_tenant_or_404(space_id, caller)


@v1.delete("/spaces/{space_id}")
@app.delete("/spaces/{space_id}")
def delete_space_endpoint(
    space_id: int, caller: token_service.TokenEntry = Depends(require_role("admin"))
) -> dict:
    from hivepilot.services import state_service

    space = _space_tenant_or_404(space_id, caller)
    state_service.delete_space(space_id, tenant=space.get("tenant", caller.tenant))
    return {"deleted": True, "id": space_id}


@v1.get("/spaces/{space_id}/messages")
@app.get("/spaces/{space_id}/messages")
def list_space_messages_endpoint(
    space_id: int,
    caller: token_service.TokenEntry = Depends(require_role("read")),
    after: int = Query(
        0, ge=0, description="Return messages with id > after (for incremental fetch)."
    ),
):
    from hivepilot.services import state_service

    space = _space_tenant_or_404(space_id, caller)
    tenant = space.get("tenant", caller.tenant)
    return {"messages": state_service.list_space_messages(space_id, tenant=tenant, after_id=after)}


@v1.post("/spaces/{space_id}/messages")
@app.post("/spaces/{space_id}/messages")
def post_space_message_endpoint(
    space_id: int,
    payload: SpaceMessageCreate,
    caller: token_service.TokenEntry = Depends(require_role("run")),
) -> dict:
    from hivepilot.services import events, state_service

    space = _space_tenant_or_404(space_id, caller)
    tenant = space.get("tenant", caller.tenant)
    if not payload.body.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty message")
    msg_id = state_service.add_space_message(
        space_id,
        payload.sender_type,
        payload.body,
        sender_id=payload.sender_id,
        tenant=tenant,
    )
    events.emit(
        "space.message",
        "space",
        space_id,
        tenant=tenant,
        payload={"space_id": space_id, "message_id": msg_id, "sender_type": payload.sender_type},
    )
    # Dépose/relève (HP-46): a HUMAN message triggers the async agent reply loop
    # (only human — a role's own reply must never trigger another). Returns
    # immediately; the agents work in the background and post their battements.
    if payload.sender_type == "human":
        from hivepilot.services import spaces_responder

        spaces_responder.dispatch_reply(space_id, tenant=tenant)
    return {"id": msg_id, "space_id": space_id}


# ---------------------------------------------------------------------------
# Orchestrator (HP-49) — decompose a feature into a MissionPlan and surface it
# in the project's persistent Orchestrateur Espace. `run`-gated. The plan's
# `strategy` (HP-69) and per-role model+repli (HP-70) hang off the result.
# ---------------------------------------------------------------------------


class DecomposeRequest(BaseModel):
    goal: str
    project: str | None = None
    #: HP-69 — optional execution/merge strategy from the UI mode card. An
    #: unknown name is ignored server-side (the plan keeps a valid strategy).
    strategy: str | None = None


@v1.get("/orchestrator/strategies", dependencies=[Depends(require_role("read"))])
@app.get("/orchestrator/strategies", dependencies=[Depends(require_role("read"))])
def orchestrator_strategies_endpoint() -> dict:
    """The catalog of execution/merge strategy presets (HP-69) — one per mockup
    mode card, in display order. The Pollen decomposition panel renders these
    directly (stages / dispatch / merge policy / guarantee label)."""
    from hivepilot.services import mission_plan

    return {
        "strategies": [
            mission_plan.STRATEGY_PRESETS[name].to_dict() for name in mission_plan.STRATEGIES
        ],
        "default": mission_plan.DEFAULT_STRATEGY,
    }


@v1.post("/orchestrator/decompose")
@app.post("/orchestrator/decompose")
def orchestrator_decompose_endpoint(
    payload: DecomposeRequest, caller: token_service.TokenEntry = Depends(require_role("run"))
) -> dict:
    if not payload.goal.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty goal")
    from hivepilot.services import orchestrator_service

    return orchestrator_service.decompose_feature(
        payload.goal, payload.project or "default", tenant=caller.tenant, strategy=payload.strategy
    )


@v1.post("/orchestrator/mission")
@app.post("/orchestrator/mission")
def orchestrator_mission_endpoint(
    payload: DecomposeRequest, caller: token_service.TokenEntry = Depends(require_role("run"))
) -> dict:
    if not payload.goal.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty goal")
    from hivepilot.services import orchestrator_service

    return orchestrator_service.launch_mission(
        payload.goal, payload.project or "default", tenant=caller.tenant, strategy=payload.strategy
    )


@v1.get("/orchestrator/missions/{mission_id}")
@app.get("/orchestrator/missions/{mission_id}")
def orchestrator_mission_status_endpoint(
    mission_id: int, caller: token_service.TokenEntry = Depends(require_role("read"))
) -> dict:
    from hivepilot.services import orchestrator_service

    result = orchestrator_service.check_mission(mission_id, tenant=caller.tenant)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no mission {mission_id}"
        )
    return result


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
    (`id`/`metadata`/`score`) rather than just the text, since the Pollen
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
def list_memories(query: str, limit: int = 20, user_id: str | None = None) -> dict[str, Any]:
    """Pollen Mem0 view — semantic search proxy over mem0.

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

    # mem0 v3 REQUIRES a non-empty `filters`. Probed against the live API on
    # 2026-08-17: omitting it answers 400 "This field is required", and
    # `filters={}` answers 400 "filters cannot be empty". So this endpoint had
    # never worked against that major version.
    #
    # There is no documented "match everything" filter, and inventing one would
    # be a guess dressed as a fix. `plugins/mem0.py` uses
    # `filters={"user_id": <task identity>}`, so the caller supplies the same
    # key here. Without it, say exactly what is missing rather than forwarding
    # an opaque 400 -- an operator cannot act on "search failed".
    if not user_id:
        return {
            "configured": True,
            "memories": [],
            "error": "user_id required",
            "detail": (
                "mem0 v3 requires a non-empty filter; pass ?user_id=<identity> "
                "(the same key plugins/mem0.py stores under)"
            ),
        }

    try:
        results = client.search(query, limit=limit, filters={"user_id": user_id})
    except Exception as exc:  # noqa: BLE001 — a mem0 client failure must never 500
        from hivepilot.utils.logging import get_logger

        get_logger(__name__).warning("api.memories.search_failed", error=str(exc))
        # `configured` answers "is mem0 set up", and nothing else. Returning
        # False here made a BROKEN search indistinguishable from an ABSENT
        # configuration -- which is why the v3 breakage above read as an
        # unused feature for a whole major version, and why it sent an
        # operator to check a setting that was already correct.
        return {
            "configured": True,
            "memories": [],
            "error": "mem0 search failed",
            "detail": "mem0 is configured, but the search call failed -- see api logs",
        }

    memories = _extract_memory_items(results)[:limit]
    return {"configured": True, "memories": memories}


# ---------------------------------------------------------------------------
# Pollen web UI surface (Sprint 3) — plugin panels. Read-only, sibling to
# the plugin-health/mem0 endpoints above.
# ---------------------------------------------------------------------------


@v1.get("/panels", dependencies=[Depends(require_role("read"))])
@app.get("/panels", dependencies=[Depends(require_role("read"))])
def list_panels_endpoint() -> dict[str, Any]:
    """Every registered Pollen panel (name/title/min_role), mirroring the
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
        "meta": dict(data.meta),
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
    """Every registered graph source (name/title/min_role/params, plus any
    enumerable values for those params) — mirrors `list_panels_endpoint`
    above. Source metadata is configuration, not secret; every `read` token
    sees the full list regardless of its own role (a source's own `min_role`
    only gates *fetching its data*, via `get_graph_endpoint`/
    `get_graph_node_detail_endpoint` below).

    `param_options` is a HINT for building a pick-list instead of a
    free-text box (see `GraphSourceSpec.param_options`). It never validates
    anything: a source still rejects an unknown value itself, and a param
    absent from the mapping simply has no enumerable values.
    `safe_param_options` guarantees this listing cannot 500 because one
    source's option provider raised."""
    sources = graph_module.list_graph_sources()
    return {
        "sources": [
            {
                "name": s.name,
                "title": s.title or s.name,
                "min_role": s.min_role,
                "params": list(s.params),
                "param_options": graph_module.safe_param_options(s),
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
# Pollen web UI (Sprint 2) — serves the pre-built static bundle committed
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
