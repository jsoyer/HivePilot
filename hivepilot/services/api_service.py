from __future__ import annotations

import csv
import io
import threading
import uuid
from collections import defaultdict
from time import time
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import generate_latest
from pydantic import BaseModel, field_validator

from hivepilot.config import settings
from hivepilot.orchestrator import Orchestrator
from hivepilot.services import analytics_service, chatops_service, state_service, token_service
from hivepilot.services.metrics import registry, run_duration_seconds
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

_RATE_LIMITED_PATHS = {
    "/run",
    "/v1/run",
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


@v1.post("/run", dependencies=[Depends(require_role("run"))])
@app.post("/run", dependencies=[Depends(require_role("run"))])
def run_task(request: RunRequest):
    with run_duration_seconds.time():
        results = _get_orchestrator().run_task(
            project_names=request.projects,
            task_name=request.task,
            extra_prompt=request.extra_prompt,
            auto_git=request.auto_git,
        )
    return {"results": [result.__dict__ for result in results]}


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


def _pdf_response(rows: list[dict[str, Any]], title: str, columns: list[str]) -> Response:
    """Render `rows`/`columns` (the same shape `_csv_response` consumes) as a
    simple tabular PDF. fpdf2 is an OPTIONAL extra (`pip install
    hivepilot[pdf]`) — lazy-imported here so the core API never depends on
    it. If it's missing, fail gracefully with a clear message instead of a
    500/traceback.
    """
    try:
        from fpdf import FPDF
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="PDF export requires the 'pdf' extra: pip install hivepilot[pdf]",
        ) from exc

    pdf = FPDF(orientation="L")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=14)
    pdf.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=9)
    with pdf.table() as table:
        header_row = table.row()
        for column in columns:
            header_row.cell(column)
        for row in rows:
            data_row = table.row()
            for column in columns:
                data_row.cell(_pdf_safe(row.get(column)))
    pdf_bytes = bytes(pdf.output())
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
        ]
    }


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
from fastapi.responses import FileResponse  # noqa: E402

from hivepilot import webui  # noqa: E402

# NOTE: import the module (`webui`), not its names — `webui.STATIC_DIR` /
# `webui.INDEX_HTML` are read fresh via attribute access below (and are what
# tests monkeypatch); `from hivepilot.webui import INDEX_HTML` would instead
# bind a stale copy at import time that a monkeypatched `webui.INDEX_HTML`
# could never reach.


def _webui_enabled() -> bool:
    return bool(settings.enable_webui) and webui.static_available()


@app.get("/ui")
@app.get("/ui/{sub_path:path}")
def serve_webui(sub_path: str = "") -> FileResponse:
    if not _webui_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    file_path = webui.resolve_static_path(sub_path) or webui.INDEX_HTML
    return FileResponse(str(file_path))


app.include_router(v1)
