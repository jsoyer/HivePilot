from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from hivepilot.config import settings
from hivepilot.services import db
from hivepilot.utils.logging import get_logger

if TYPE_CHECKING:
    # Import-time only: avoids a circular import, since
    # `drift_service.scan_and_record` imports this module at runtime.
    from hivepilot.services.drift_service import DriftResult

try:
    from hivepilot.services import metrics as _metrics  # noqa: F401

    _METRICS_AVAILABLE = True
except ImportError:
    _metrics = None  # type: ignore[assignment]
    _METRICS_AVAILABLE = False

logger = get_logger(__name__)
# Keep DB_PATH as a module-level name: retry_service.py and tests reference it.
DB_PATH = settings.resolve_path(settings.state_db)

# ---------------------------------------------------------------------------
# Formal run-status enum
# ---------------------------------------------------------------------------

# The enum values deliberately match the historical string literals stored in
# the SQLite ``status`` column so existing rows remain fully compatible.


class RunStatus(str, Enum):
    """Canonical pipeline run-status values.

    Inherits ``str`` so that ``RunStatus.RUNNING == "running"`` is ``True``
    and values can be stored directly in the SQLite ``status`` column without
    conversion.

    Backward-compatible: the legacy strings ``'running'``, ``'pending'``, and
    ``'complete'`` are accepted via :meth:`from_str`.
    """

    # --- primary states ---
    NEW = "new"
    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    REVIEW = "review"
    APPROVAL = "approval"
    COMPLETE = "complete"

    # --- terminal-by-operator states ---
    # Mirador actionable dashboard PRD, Sprint 4 (`POST /v1/runs/{run_id}/
    # cancel`): a run an operator cooperatively stopped mid-execution, at the
    # next step boundary (see `Orchestrator._execute_task_body`'s step loop
    # and `async_run_service.is_cancel_requested`). Terminal like COMPLETE/
    # the failure states below -- `state_service.complete_run` sets
    # `finished_at` for it exactly like every other terminal status. There is
    # no separate "is this terminal" classification helper in this module to
    # update -- `from_str` already handles it via the generic
    # `cls(normalised)` value lookup below, no special-casing needed.
    CANCELLED = "cancelled"

    # --- failure states ---
    RATE_LIMIT = "rate_limit"
    AUTH_EXPIRED = "auth_expired"
    TEST_FAILURE = "test_failure"
    SECURITY_BLOCKER = "security_blocker"

    @classmethod
    def from_str(cls, value: str) -> "RunStatus":
        """Return the ``RunStatus`` for *value*.

        Accepts:
        - Any ``RunStatus`` member name (case-insensitive), e.g. ``"RUNNING"``
        - Any ``RunStatus`` member value, e.g. ``"running"``
        - Legacy alias ``"pending"`` -> :attr:`NEW`

        Raises
        ------
        ValueError
            If *value* cannot be mapped to any known status.
        """
        normalised = value.strip().lower()

        # Legacy alias
        if normalised == "pending":
            return cls.NEW

        # Try by value first (covers "running", "complete", ...)
        try:
            return cls(normalised)
        except ValueError:
            pass

        # Try by name (covers "RUNNING", "running" as name, ...)
        upper = normalised.upper()
        try:
            return cls[upper]
        except KeyError:
            pass

        raise ValueError(f"Unknown status: {value!r}")


def _add_column_if_missing(conn: Any, table: str, coldef: str) -> None:
    """Idempotently add a column to *table*, race-safe under concurrent callers.

    ``init_db()`` can be invoked concurrently from multiple threads (e.g. an
    async-run background worker and the request thread) against the same
    sqlite file. The ``column_exists`` check is kept as a fast-path guard to
    avoid the exception on the common case, but the ``ALTER TABLE`` itself is
    wrapped in a narrow try/except: if a racing caller wins and adds the
    column first, sqlite raises ``OperationalError: duplicate column name``,
    which is swallowed here. Any other ``OperationalError`` is re-raised.
    """
    if db.column_exists(conn, table, coldef.split()[0]):
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def init_db() -> None:
    pk = db.autoincrement_pk()
    with db.connect() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS runs (
                id {pk},
                project TEXT,
                task TEXT,
                status TEXT,
                detail TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS steps (
                id {pk},
                run_id INTEGER,
                step TEXT,
                status TEXT,
                detail TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
            )
            """
        )
        # Idempotent migration (Phase 24b.1): persist provider/model per step.
        # Additive-only, same ALTER TABLE ... ADD COLUMN pattern as the
        # 'tenant' migrations below — safe to run against an existing DB.
        _add_column_if_missing(conn, "steps", "provider TEXT")
        _add_column_if_missing(conn, "steps", "model TEXT")
        # Idempotent migration (Phase 24b.2a): persist opt-in usage capture
        # (tokens/cost) per step, same additive ALTER TABLE ... ADD COLUMN
        # pattern as provider/model above — safe to run against an existing DB.
        _add_column_if_missing(conn, "steps", "input_tokens INTEGER")
        _add_column_if_missing(conn, "steps", "output_tokens INTEGER")
        _add_column_if_missing(conn, "steps", "cost_usd REAL")
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS interactions (
                id {pk},
                run_id INTEGER,
                actor TEXT,
                action TEXT,
                target TEXT,
                summary TEXT,
                metadata TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule_runs (
                name TEXT PRIMARY KEY,
                last_run TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                run_id INTEGER PRIMARY KEY,
                project TEXT,
                task TEXT,
                metadata TEXT,
                status TEXT DEFAULT 'pending',
                requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_by TEXT,
                approved_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                token TEXT PRIMARY KEY,
                role TEXT,
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS audit_log (
                id {pk},
                token_hash TEXT, role TEXT, endpoint TEXT, method TEXT, result TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS retry_queue (
                id {pk},
                schedule_name TEXT, task TEXT, projects TEXT, error TEXT,
                attempt INTEGER, max_attempts INTEGER, status TEXT DEFAULT 'pending',
                next_retry_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Idempotent migration: add context column if missing
        _add_column_if_missing(conn, "retry_queue", "context TEXT")
        # Idempotent multi-tenant migrations
        _add_column_if_missing(conn, "runs", "tenant TEXT DEFAULT 'default'")
        _add_column_if_missing(conn, "approvals", "tenant TEXT DEFAULT 'default'")
        _add_column_if_missing(conn, "audit_log", "tenant TEXT DEFAULT 'default'")
        _add_column_if_missing(conn, "tokens", "tenant TEXT DEFAULT 'default'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workers (
                name TEXT PRIMARY KEY,
                url TEXT,
                status TEXT,
                detail TEXT,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Phase 20 D2: persist IaC drift-scan results (history + baseline).
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS drift_scans (
                id {pk},
                project TEXT NOT NULL,
                runner TEXT NOT NULL,
                drifted INTEGER NOT NULL,
                to_add INTEGER,
                to_change INTEGER,
                to_destroy INTEGER,
                status TEXT NOT NULL,
                detail TEXT,
                tenant TEXT NOT NULL DEFAULT 'default',
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Debate Judge & Consensus PRD, Sprint 3: persist debate-judge /
        # challenge-arbiter Verdicts (redacted) for later review (PRD 2).
        # Sibling to `interactions` -- same run_id FK/CASCADE shape. Only
        # structured, non-secret fields are dedicated columns (decision,
        # confidence, kind); any free-text `summary` is redacted before
        # INSERT, same choke-point pattern as `record_interaction` below.
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS verdicts (
                id {pk},
                run_id INTEGER,
                project TEXT,
                task TEXT,
                role TEXT,
                kind TEXT,
                decision TEXT,
                confidence REAL,
                summary TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
            )
            """
        )
        # Auto-Learning Lessons Loop PRD, Sprint 2: persist opt-in,
        # LLM-distilled "lessons" candidates correlating a run's verdicts +
        # interactions + outcomes into structured, scored guidance for future
        # runs. Sibling to `verdicts`/`interactions` -- same `run_id` FK/
        # CASCADE shape, plus optional FKs back to the specific `verdicts`/
        # `interactions` row a lesson was distilled from (nullable -- a
        # lesson need not trace to exactly one source row). `validated`
        # defaults to 0/False: Sprint 2 only ever inserts CANDIDATE lessons
        # (see `lessons_service.distill_lessons`) -- Sprint 3 owns turning a
        # candidate into a validated, retrievable lesson via real outcome
        # signal, never the distiller's own self-reported score. `text` is
        # the only free-text column and is routed through `redact_text`
        # before INSERT in `record_lesson` below -- same choke-point pattern
        # as `verdicts.summary`/`interactions.summary`.
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS lessons (
                id {pk},
                run_id INTEGER,
                project TEXT,
                role TEXT,
                task TEXT,
                source_verdict_id INTEGER,
                source_interaction_id INTEGER,
                text TEXT,
                score REAL,
                confidence REAL,
                category TEXT,
                validated INTEGER DEFAULT 0,
                use_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE,
                FOREIGN KEY(source_verdict_id) REFERENCES verdicts(id),
                FOREIGN KEY(source_interaction_id) REFERENCES interactions(id)
            )
            """
        )


def upsert_worker(name: str, url: str, status: str, detail: str | None = None) -> None:
    """Record/refresh a worker's health (pull model: hub pinged its /health)."""
    init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
            INSERT INTO workers (name, url, status, detail, last_seen)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
                url=excluded.url, status=excluded.status,
                detail=excluded.detail, last_seen=CURRENT_TIMESTAMP
            """
            ),
            (name, url, status, detail),
        )


def list_workers() -> list[dict[str, Any]]:
    init_db()
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM workers ORDER BY name").fetchall()
    return [dict(row) for row in rows]


def record_run_start(
    project: str, task: str, status: str = "running", tenant: str = "default"
) -> int:
    init_db()
    with db.connect() as conn:
        run_id = db.insert_returning_id(
            conn,
            "INSERT INTO runs (project, task, status, tenant) VALUES (?, ?, ?, ?)",
            (project, task, status, tenant),
        )
        logger.info(
            "state.run_start",
            run_id=run_id,
            project=project,
            task=task,
            status=status,
            tenant=tenant,
        )
        return run_id


def record_step(
    run_id: int,
    step: str,
    status: str,
    detail: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """Record a step outcome.

    ``provider``/``model`` are additive and optional (Phase 24b.1 — persist
    provider/model per step): existing callers that omit them are unaffected
    and persist ``NULL`` for both, exactly as before this sprint.

    ``input_tokens``/``output_tokens``/``cost_usd`` are additive and optional
    (Phase 24b.2a — opt-in usage capture): existing callers that omit them are
    unaffected and persist ``NULL`` for all three, exactly as before this
    sprint. Cost here is whatever the runner's CLI self-reports — there is no
    price-map lookup in this sprint (that's a later phase).
    """
    init_db()
    # Choke point: `detail` often carries `str(exc)` from a failed step, which
    # may echo a resolved ${secret:NAME} value an agent printed. Redact before
    # it's persisted to SQLite.
    from hivepilot.services.config_provenance import redact_text

    detail = redact_text(detail) if detail is not None else detail
    with db.connect() as conn:
        conn.execute(
            db.ph(
                "INSERT INTO steps "
                "(run_id, step, status, detail, provider, model, "
                "input_tokens, output_tokens, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (run_id, step, status, detail, provider, model, input_tokens, output_tokens, cost_usd),
        )
    if _METRICS_AVAILABLE and _metrics is not None:
        try:
            _metrics.steps_total.labels(status=status).inc()
        except Exception:  # noqa: BLE001
            pass


def complete_run(run_id: int, status: str, detail: str | None = None) -> None:
    init_db()
    # Choke point: same rationale as record_step — `detail` may carry `str(exc)`.
    from hivepilot.services.config_provenance import redact_text

    detail = redact_text(detail) if detail is not None else detail
    with db.connect() as conn:
        conn.execute(
            db.ph("UPDATE runs SET status=?, detail=?, finished_at=CURRENT_TIMESTAMP WHERE id=?"),
            (status, detail, run_id),
        )
    logger.info("state.run_complete", run_id=run_id, status=status)
    if _METRICS_AVAILABLE and _metrics is not None:
        try:
            _metrics.runs_total.labels(status=status).inc()
        except Exception:  # noqa: BLE001
            pass


def get_run(run_id: int) -> dict[str, Any] | None:
    """Return the single `runs` row for *run_id*, or `None` if it doesn't
    exist. Mirador actionable dashboard PRD, Sprint 4 -- `POST /v1/runs/
    {run_id}/cancel` resolves the run's `tenant` through this, exactly like
    `POST /v1/approvals/{run_id}`'s `state_service.get_approval` resolves
    the approval row's tenant for its own tenant check.
    """
    init_db()
    with db.connect() as conn:
        row = conn.execute(db.ph("SELECT * FROM runs WHERE id=?"), (run_id,)).fetchone()
    return dict(row) if row else None


def list_recent_runs(limit: int = 50, tenant: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with db.connect() as conn:
        if tenant is not None:
            rows = conn.execute(
                db.ph("SELECT * FROM runs WHERE tenant=? ORDER BY started_at DESC LIMIT ?"),
                (tenant, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                db.ph("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?"), (limit,)
            ).fetchall()
    return [dict(row) for row in rows]


def get_steps_for_run(run_id: int) -> list[dict[str, Any]]:
    init_db()
    with db.connect() as conn:
        rows = conn.execute(
            db.ph("SELECT * FROM steps WHERE run_id=? ORDER BY timestamp"), (run_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def record_interaction(
    actor: str,
    action: str,
    target: str | None,
    summary: str,
    timestamp: str | None = None,
    run_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    init_db()
    # Choke point: `summary` is often a stage's aggregated agent output
    # (Orchestrator.run_pipeline's `stage_output`), which can echo a resolved
    # ${secret:NAME} value. Redact before it's persisted to SQLite.
    from hivepilot.services.config_provenance import redact_text

    summary = redact_text(summary)
    with db.connect() as conn:
        interaction_id = db.insert_returning_id(
            conn,
            """
            INSERT INTO interactions (run_id, actor, action, target, summary, metadata, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (
                run_id,
                actor,
                action,
                target,
                summary,
                json.dumps(metadata) if metadata is not None else None,
                timestamp,
            ),
        )
        logger.info(
            "state.interaction",
            interaction_id=interaction_id,
            actor=actor,
            action=action,
            run_id=run_id,
        )
        return interaction_id


def list_recent_interactions(limit: int = 50, run_id: int | None = None) -> list[dict[str, Any]]:
    init_db()
    with db.connect() as conn:
        if run_id is not None:
            rows = conn.execute(
                db.ph("SELECT * FROM interactions WHERE run_id=? ORDER BY id DESC LIMIT ?"),
                (run_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                db.ph("SELECT * FROM interactions ORDER BY id DESC LIMIT ?"), (limit,)
            ).fetchall()
    return [dict(row) for row in rows]


def record_verdict(
    *,
    run_id: int | None,
    project: str | None,
    task: str | None,
    role: str | None,
    kind: str,
    decision: str | None,
    confidence: float | None,
    summary: str | None = None,
) -> int:
    """Persist a debate-judge / challenge-arbiter :class:`Verdict` (Debate
    Judge & Consensus PRD, Sprint 3). ``kind`` is ``"debate"``
    (``Orchestrator._adjudicate``) or ``"challenge"``
    (``Orchestrator._adjudicate_challenge``) -- see the module-level
    ``Verdict`` dataclass in ``orchestrator.py`` for the contract this
    mirrors.

    Only structured, non-secret fields are dedicated columns (``decision``,
    ``confidence``, ``kind``); any free-text ``summary`` is routed through
    ``redact_text`` before INSERT -- same choke-point pattern as
    ``record_interaction``/``record_step`` above, since a judge's raw
    rationale can echo a resolved ``${secret:NAME}`` value and must never
    reach SQLite unredacted.
    """
    init_db()
    from hivepilot.services.config_provenance import redact_text

    summary = redact_text(summary) if summary is not None else None
    with db.connect() as conn:
        verdict_id = db.insert_returning_id(
            conn,
            db.ph(
                "INSERT INTO verdicts "
                "(run_id, project, task, role, kind, decision, confidence, summary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (run_id, project, task, role, kind, decision, confidence, summary),
        )
        logger.info(
            "state.verdict",
            verdict_id=verdict_id,
            kind=kind,
            run_id=run_id,
            decision=decision,
            confidence=confidence,
        )
        return verdict_id


def list_recent_verdicts(limit: int = 50, run_id: int | None = None) -> list[dict[str, Any]]:
    """Read back persisted verdicts (redacted summary), newest first."""
    init_db()
    with db.connect() as conn:
        if run_id is not None:
            rows = conn.execute(
                db.ph("SELECT * FROM verdicts WHERE run_id=? ORDER BY id DESC LIMIT ?"),
                (run_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                db.ph("SELECT * FROM verdicts ORDER BY id DESC LIMIT ?"), (limit,)
            ).fetchall()
    return [dict(row) for row in rows]


def record_lesson(
    *,
    run_id: int | None,
    project: str | None,
    role: str | None,
    task: str | None,
    source_verdict_id: int | None = None,
    source_interaction_id: int | None = None,
    text: str,
    score: float | None,
    confidence: float | None,
    category: str | None,
    validated: bool = False,
) -> int:
    """Persist a distilled :class:`lessons_service.Lesson` candidate
    (Auto-Learning Lessons Loop PRD, Sprint 2) and return the new row id.

    ``text`` AND ``category`` are the only free-text fields and are both
    routed through ``redact_text`` before INSERT -- same choke-point pattern
    as ``record_verdict``'s ``summary``/``record_interaction``'s
    ``summary``, since a distilled lesson (or a direct API caller supplying
    its own, unredacted ``category``) can echo a resolved ``${secret:NAME}``
    value from the verdicts/interactions it was built from. Redacting only
    ``text`` would leave ``category`` as a bypass for the same class of leak
    this table exists to guard against.

    ``validated`` defaults to ``False`` -- Sprint 2's distillation path
    (``lessons_service.distill_lessons`` -> the orchestrator wiring) always
    calls this with ``validated=False``; real validation is Sprint 3's job.
    """
    init_db()
    from hivepilot.services.config_provenance import redact_text

    text = redact_text(text)
    category = redact_text(category) if category is not None else None
    with db.connect() as conn:
        lesson_id = db.insert_returning_id(
            conn,
            db.ph(
                "INSERT INTO lessons "
                "(run_id, project, role, task, source_verdict_id, source_interaction_id, "
                "text, score, confidence, category, validated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                run_id,
                project,
                role,
                task,
                source_verdict_id,
                source_interaction_id,
                text,
                score,
                confidence,
                category,
                int(validated),
            ),
        )
        logger.info(
            "state.lesson",
            lesson_id=lesson_id,
            run_id=run_id,
            project=project,
            role=role,
            category=category,
            validated=validated,
        )
        return lesson_id


def list_lessons(
    project: str,
    role: str | None = None,
    task: str | None = None,
    *,
    validated_only: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return lessons for *project*, newest first (ranking is Sprint 3's
    job -- this is a simple recent-first read), optionally filtered by
    *role*/*task*. ``validated_only`` (default ``True``) restricts to rows
    with ``validated=1``; Sprint 2 never sets that flag, so callers must
    pass ``validated_only=False`` to see Sprint 2's freshly-distilled
    candidates until Sprint 3's validation gate promotes them."""
    init_db()
    clauses = ["project=?"]
    params: list[Any] = [project]
    if role is not None:
        clauses.append("role=?")
        params.append(role)
    if task is not None:
        clauses.append("task=?")
        params.append(task)
    if validated_only:
        clauses.append("validated=1")
    where = " AND ".join(clauses)
    sql = f"SELECT * FROM lessons WHERE {where} ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db.connect() as conn:
        rows = conn.execute(db.ph(sql), tuple(params)).fetchall()
    return [dict(row) for row in rows]


def mark_lesson_used(lesson_id: int) -> None:
    """Increment a lesson's ``use_count`` (called when a lesson is injected
    into a future run's context -- Sprint 3/4's retrieval + injection
    path)."""
    init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph("UPDATE lessons SET use_count = use_count + 1 WHERE id=?"),
            (lesson_id,),
        )


def update_lesson_validation(lesson_id: int, *, validated: bool, score: float) -> None:
    """Update an already-persisted lesson CANDIDATE's ``validated``/``score``
    columns (Auto-Learning Lessons Loop PRD, Sprint 3), after
    `lessons_service.validate_lesson` computes them from REAL outcome
    signal.

    Deliberately INSERT-then-UPDATE, never a combined upsert at INSERT
    time: `record_lesson` always persists a fresh candidate with
    ``validated=False``/``score=None`` first (Sprint 2's contract, and
    Sprint 2's own tests assert exactly that) -- this function only ever
    runs as a SEPARATE, later step against an id that row already has,
    keeping that INSERT-time contract fully intact for any caller that
    stops after `record_lesson` (e.g. `enable_lesson_distillation=False`
    or a validation-step failure -- see the orchestrator wiring's
    best-effort discipline around this call).
    """
    init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph("UPDATE lessons SET validated=?, score=? WHERE id=?"),
            (int(validated), score, lesson_id),
        )
    logger.info("state.lesson_validated", lesson_id=lesson_id, validated=validated, score=score)


def list_ranked_lessons(
    project: str,
    role: str | None = None,
    task: str | None = None,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return VALIDATED lessons for *project* (optionally filtered by
    *role*/*task*), ranked ``score DESC, created_at DESC`` (then ``id
    DESC`` as a final deterministic tiebreak among same-timestamp rows),
    capped at *limit* -- the read `lessons_service.retrieve_lessons` (Sprint
    3) wraps into `Lesson` objects for retrieval/injection.

    Always restricted to ``validated=1`` -- unlike `list_lessons`, there is
    NO ``validated_only`` toggle here: retrieval/injection must never be
    able to surface an unvalidated (or not-yet-validated) candidate, so
    that filter is unconditional rather than caller-controlled.
    """
    init_db()
    clauses = ["project=?", "validated=1"]
    params: list[Any] = [project]
    if role is not None:
        clauses.append("role=?")
        params.append(role)
    if task is not None:
        clauses.append("task=?")
        params.append(task)
    where = " AND ".join(clauses)
    sql = (
        f"SELECT * FROM lessons WHERE {where} ORDER BY score DESC, created_at DESC, id DESC LIMIT ?"
    )
    params.append(limit)
    with db.connect() as conn:
        rows = conn.execute(db.ph(sql), tuple(params)).fetchall()
    return [dict(row) for row in rows]


def get_schedule_last_run(name: str) -> datetime | None:
    init_db()
    with db.connect() as conn:
        row = conn.execute(
            db.ph("SELECT last_run FROM schedule_runs WHERE name=?"), (name,)
        ).fetchone()
    if row and row["last_run"]:
        dt = datetime.fromisoformat(row["last_run"])
        # SQLite's CURRENT_TIMESTAMP (written by update_schedule_run) is UTC
        # but stored/parsed as a NAIVE datetime -- attach UTC tzinfo so every
        # caller (schedule_service.due_schedules(), drift_schedule's
        # due_drift_projects(), cli.py's `schedule list`) can safely compare/
        # subtract this against an aware `datetime.now(timezone.utc)` without
        # a "can't compare offset-naive and offset-aware datetimes" TypeError.
        # Leave an already-aware value untouched.
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def update_schedule_run(name: str) -> None:
    init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
            INSERT INTO schedule_runs (name, last_run) VALUES (?, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET last_run=CURRENT_TIMESTAMP
            """
            ),
            (name,),
        )


def record_approval_request(
    run_id: int,
    project: str,
    task: str,
    metadata: dict[str, Any],
    tenant: str = "default",
) -> None:
    init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
            INSERT OR REPLACE INTO approvals (run_id, project, task, metadata, status, tenant)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """
            ),
            (run_id, project, task, json.dumps(metadata), tenant),
        )


def get_pending_approvals(tenant: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with db.connect() as conn:
        if tenant is not None:
            rows = conn.execute(
                db.ph(
                    "SELECT * FROM approvals WHERE status='pending' AND tenant=? ORDER BY requested_at"
                ),
                (tenant,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE status='pending' ORDER BY requested_at"
            ).fetchall()
    return [dict(row) for row in rows]


def get_approval(run_id: int) -> dict[str, Any] | None:
    init_db()
    with db.connect() as conn:
        row = conn.execute(db.ph("SELECT * FROM approvals WHERE run_id=?"), (run_id,)).fetchone()
    return dict(row) if row else None


def update_approval(run_id: int, status: str, approver: str | None = None) -> None:
    init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
            UPDATE approvals
            SET status=?, approved_by=?, approved_at=CURRENT_TIMESTAMP
            WHERE run_id=?
            """
            ),
            (status, approver, run_id),
        )


def update_approval_metadata(run_id: int, metadata: dict[str, Any]) -> None:
    """Update the metadata JSON blob for an existing approval row."""
    init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph("UPDATE approvals SET metadata=? WHERE run_id=?"),
            (json.dumps(metadata), run_id),
        )


def store_token(entry) -> None:
    init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph("INSERT OR REPLACE INTO tokens (token, role, note, tenant) VALUES (?, ?, ?, ?)"),
            (entry.token, entry.role, entry.note, getattr(entry, "tenant", "default")),
        )


def delete_token(token: str) -> None:
    init_db()
    with db.connect() as conn:
        conn.execute(db.ph("DELETE FROM tokens WHERE token=?"), (token,))


def get_token(token: str) -> dict[str, Any] | None:
    init_db()
    with db.connect() as conn:
        row = conn.execute(db.ph("SELECT * FROM tokens WHERE token=?"), (token,)).fetchone()
    return dict(row) if row else None


def list_all_runs(tenant: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with db.connect() as conn:
        if tenant is not None:
            rows = conn.execute(
                db.ph("SELECT * FROM runs WHERE tenant=? ORDER BY started_at DESC"),
                (tenant,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC").fetchall()
    return [dict(row) for row in rows]


def record_audit(
    token_hash: str,
    role: str,
    endpoint: str,
    method: str,
    result: str,
    tenant: str = "default",
) -> None:
    init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                "INSERT INTO audit_log (token_hash, role, endpoint, method, result, tenant) VALUES (?,?,?,?,?,?)"
            ),
            (token_hash, role, endpoint, method, result, tenant),
        )


def list_audit_log(limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    with db.connect() as conn:
        rows = conn.execute(
            db.ph("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?"), (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Drift-scan persistence (Phase 20 D2)
# ---------------------------------------------------------------------------


def record_drift_scan(result: "DriftResult", *, tenant: str = "default") -> int:
    """Persist a single `drift_service.DriftResult` and return the new row id.

    `status` is derived from *result*: `'error'` when `result.error` is set,
    else `'drift'` when `result.drifted`, else `'ok'`. `to_add`/`to_change`/
    `to_destroy` are taken from `result.summary` when present, else stored as
    NULL. `detail` is the redacted error message (defense-in-depth choke
    point, same idiom as `record_step`/`complete_run` — D1's `detect_drift`
    already only raises tool+code-only messages, but this table must never
    become the exception to that discipline) or NULL when there's no error.
    """
    init_db()
    # Choke point: same rationale as record_step/complete_run — `detail` may
    # carry `str(exc)` from a failed drift check.
    from hivepilot.services.config_provenance import redact_text

    if result.error is not None:
        status = "error"
    elif result.drifted:
        status = "drift"
    else:
        status = "ok"
    detail = redact_text(result.error) if result.error is not None else None
    summary = result.summary
    with db.connect() as conn:
        row_id = db.insert_returning_id(
            conn,
            "INSERT INTO drift_scans "
            "(project, runner, drifted, to_add, to_change, to_destroy, status, detail, tenant) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.project,
                result.runner,
                int(result.drifted),
                summary.to_add if summary is not None else None,
                summary.to_change if summary is not None else None,
                summary.to_destroy if summary is not None else None,
                status,
                detail,
                tenant,
            ),
        )
        logger.info(
            "state.drift_scan",
            row_id=row_id,
            project=result.project,
            runner=result.runner,
            status=status,
            tenant=tenant,
        )
        return row_id


def get_recent_drift_scans(
    project: str | None = None, *, limit: int = 50, tenant: str | None = None
) -> list[dict[str, Any]]:
    """Return recent drift-scan rows, newest first (then id descending for
    determinism among same-timestamp rows), optionally filtered by
    *project* and/or *tenant*."""
    init_db()
    clauses: list[str] = []
    params: list[Any] = []
    if project is not None:
        clauses.append("project=?")
        params.append(project)
    if tenant is not None:
        clauses.append("tenant=?")
        params.append(tenant)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM drift_scans{where} ORDER BY checked_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with db.connect() as conn:
        rows = conn.execute(db.ph(sql), tuple(params)).fetchall()
    return [dict(row) for row in rows]


def get_drift_baseline(project: str, *, tenant: str = "default") -> dict[str, Any] | None:
    """Return the most-recent no-drift (`status='ok'`) scan for *project*
    within *tenant*, or `None` when there isn't one."""
    init_db()
    with db.connect() as conn:
        row = conn.execute(
            db.ph(
                "SELECT * FROM drift_scans WHERE project=? AND tenant=? AND status='ok' "
                "ORDER BY checked_at DESC, id DESC LIMIT 1"
            ),
            (project, tenant),
        ).fetchone()
    return dict(row) if row else None
