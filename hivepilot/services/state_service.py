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

# Runner kinds that cannot have invoked a model — they execute a command.
# THE single definition of "not agent work", used for three decisions that
# must agree: `record_step` refuses to attribute these steps to a role,
# `analytics_service` classifies them, and `orchestrator.resolve_step_runner`
# lets a step declaring one of them keep it even when its task has a role (a
# role's agent runner cannot execute a shell command). Separate copies of
# this set would eventually disagree, and the disagreement would surface as
# spend belonging to no one — or as a test suite handed to a model.
NON_MODEL_PROVIDERS = frozenset({"shell", "container"})

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
    # Bug 1 fix (run 243, live incident): a runner process killed by a POSIX
    # signal (SIGKILL/SIGABRT/SIGSEGV/... -- see
    # `hivepilot.runners.base.classify_signal_exit`) never ran to completion,
    # so it can never be a TEST_FAILURE (that bucket implies the command ran
    # and either the tests or the agent's logic were at fault). Distinct
    # bucket so the operator investigates the HOST/container (memory,
    # resource limits) instead of the codebase. A deliberate SIGTERM
    # (operator/service-triggered stop) is reported as CANCELLED instead --
    # see `Orchestrator._classify_stage_failure`.
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"

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
        # Idempotent migration (Mirador Agent Panels backend sprint): persist
        # the role that executed a step, same additive ALTER TABLE ... ADD
        # COLUMN pattern as provider/model/usage above -- safe on an existing
        # prod DB. Every row written before this migration ships (and any
        # row from a non-role task) has role=NULL -- never backfilled with a
        # guess. Callers that read this honestly surface NULL as an explicit
        # "unknown" bucket (see analytics_service.cost_summary/agents_summary)
        # rather than dropping or misattributing those rows.
        _add_column_if_missing(conn, "steps", "role TEXT")
        # Idempotent migration (usage-capture-modelusage fix): prompt-cache
        # read/creation tokens are billed at DIFFERENT rates from base
        # input/output tokens and from each other (see
        # `claude_runner._extract_model_usage` / `pricing.estimate_cost`) --
        # tracked as their own columns rather than folded into
        # input_tokens/output_tokens, same additive ALTER TABLE ... ADD
        # COLUMN pattern as every other steps migration above.
        # Not a cost column: the discriminator that tells a short step apart
        # from a wasteful prefix when reading the two cache columns below.
        # Historic rows stay NULL and are reported as unclassified rather
        # than folded into either answer.
        _add_column_if_missing(conn, "steps", "turns INTEGER")
        _add_column_if_missing(conn, "steps", "cache_read_tokens INTEGER")
        _add_column_if_missing(conn, "steps", "cache_creation_tokens INTEGER")
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
        # Context truncation, made visible instead of only logged. Run 639 is
        # why: `cap` mode kept the TAIL of the joined prior context, ~90% of the
        # run vanished with both verdicts the gate needed, and the gate then
        # refused a release on a clearance that HAD been given. The only trace
        # was a `logger.warning` in a file nobody opens until it is too late.
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS context_truncations (
                id {pk},
                run_id INTEGER,
                project TEXT,
                role TEXT,
                total_chars INTEGER,
                budget INTEGER,
                dropped_chars INTEGER,
                stages INTEGER,
                largest_stage_chars INTEGER,
                -- 'derived' from the model's real window, or 'fallback' to a
                -- configured constant. The warning this replaces never carried
                -- it, and it is the field that says whether the budget is a
                -- measurement or a guess.
                budget_basis TEXT,
                tenant TEXT NOT NULL DEFAULT 'default',
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        # Swarm Phase 1 (peer federation bus): the audit/dedupe/claim source
        # of truth for EVERY swarm event, regardless of which
        # `hivepilot.swarm.transport.Transport` delivered it -- see
        # `hivepilot.services.swarm_service`. `id` is the caller-supplied,
        # DETERMINISTIC event id (`hivepilot.swarm.models.compute_event_id`),
        # so it is the table's own PRIMARY KEY rather than an autoincrement
        # column: a second `insert_swarm_event` for the same id is a no-op
        # INSERT (see `ON CONFLICT(id) DO NOTHING` below), which is exactly
        # the publish-time DEDUPE contract. `status` is one of
        # pending/claimed/running/done/skipped/failed. `running` (HIGH #2
        # fix, opus security review) is the atomic gate between a winning
        # claim and actual handler invocation -- see
        # `mark_swarm_event_running` -- closing a TOCTOU race where a claim
        # race-LOSER could still read a `claimed` row and run the handler
        # too. `failed` (bug-debt fix) is the OTHER defined terminal state a
        # `running` row can reach: a handler that raises moves the row
        # straight to `failed` rather than leaving it `running` forever with
        # no reaper -- see `mark_swarm_event_failed` and `swarm_service.
        # process_claimed_event`'s docstring for the at-most-once rationale
        # (never a silent re-queue). `deduped` is an
        # application-level outcome -- see
        # `hivepilot.services.swarm_service.PublishStatus`/`ClaimStatus` --
        # never actually written to this column, since the row this table
        # would attach it to already has a truthful status of its own; see
        # that module's docstring for the full rationale).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS swarm_events (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                payload TEXT NOT NULL,
                tenant TEXT NOT NULL DEFAULT 'default',
                origin_instance TEXT,
                sig TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                claimed_by TEXT,
                ts REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Propose -> ratify -> dispatch PRD, Sprint 2 (spec §8, "the journal").
        # `runs`/`steps`/`interactions` above already give durable execution
        # history; what is genuinely absent is (a) the (partition, task) ->
        # run mapping, (b) a claim lifecycle over it, (c) the outward-consent
        # record, and (d) the PR link. These two tables hold exactly that and
        # nothing that already exists elsewhere.
        #
        # The `approvals` table above is deliberately NOT reused: it is
        # `PRIMARY KEY(run_id)`, whereas a partition PRECEDES and SPANS N
        # runs -- there is no single run_id to key it by at ratification
        # time.
        #
        # `id` is a caller-supplied opaque string (a uuid4 hex from
        # `partition_service.create_partition`), so it is the PRIMARY KEY
        # directly rather than an autoincrement column -- same shape as
        # `swarm_events` above, and it keeps a partition id stable across
        # instances/exports.
        #
        # status: proposed | ratified | dispatching | completed | failed |
        #         vetoed | expired
        # Every transition is a conditional `UPDATE ... WHERE status=?`
        # returning `rowcount == 1` (see `partition_service`), literally the
        # `claim_swarm_event`/`mark_swarm_event_running` idiom below -- not a
        # second mechanism, the same one.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS partitions (
                id TEXT PRIMARY KEY,
                tenant TEXT NOT NULL DEFAULT 'default',
                source_kind TEXT,
                source_ref TEXT,
                source_digest TEXT,
                proposer_run_id INTEGER,
                proposed_json TEXT NOT NULL,
                proposed_digest TEXT NOT NULL,
                ratified_json TEXT,
                ratified_digest TEXT,
                ratified_diff TEXT,
                outward_consent INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'proposed',
                ratified_by TEXT,
                ratified_at TIMESTAMP,
                created_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # status: pending | claimed | running | committed | failed | skipped |
        #         cancelled
        # `committed` means the task's run terminated successfully AND, when
        # outward was consented, a PR URL was captured. `pr_url` stays NULL
        # when the forge cannot cheaply produce one -- the journal shows "—".
        # NEVER a fabricated URL (same stated position as the `steps.role`
        # migration comment above: an unknown value is surfaced honestly as
        # NULL, never backfilled with a guess).
        # `attempt` supports retry-as-a-NEW-run under the same
        # (partition_id, task_id): a ratified partition is immutable, so a
        # retry never mutates the plan, only adds an attempt.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS partition_tasks (
                partition_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                queue_id INTEGER,
                run_id INTEGER,
                attempt INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                claimed_by TEXT,
                claimed_at TIMESTAMP,
                pr_url TEXT,
                cost_usd REAL,
                wall_clock_seconds INTEGER,
                updated_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (partition_id, task_id),
                FOREIGN KEY(partition_id) REFERENCES partitions(id) ON DELETE CASCADE
            )
            """
        )
        # Metrics the agent CLI reports about itself, over OTLP.  Independent
        # of our own per-step bookkeeping on purpose: the calls that build
        # their own payload never reach that bookkeeping, so a total taken
        # from `steps` alone reads as zero for spending that did happen.
        #
        # No identity columns.  The exporter tags every point with the user's
        # email and account UUIDs; telemetry_service drops them before they
        # get here, and there is deliberately nowhere to put them.
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS agent_telemetry (
                id {pk},
                metric TEXT NOT NULL,
                kind TEXT,
                model TEXT,
                effort TEXT,
                query_source TEXT,
                session_id TEXT,
                unit TEXT,
                value REAL NOT NULL,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # The PIPELINE run a verdict belongs to. `run_id` keeps pointing at
        # whatever produced it -- sometimes a task, sometimes the pipeline --
        # and changing that meaning would silently reinterpret existing rows.
        # Without this column `LEFT JOIN approvals x verdicts ON run_id`
        # returns nothing, because approvals are pipeline-level and half the
        # verdicts are not: the agreement rate the autonomy ladder needs was
        # not blocked on volume, it was blocked on the join.
        _add_column_if_missing(conn, "verdicts", "pipeline_run_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_telemetry_lookup "
            "ON agent_telemetry (metric, session_id, recorded_at)"
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
    role: str | None = None,
    cache_read_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    turns: int | None = None,
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

    ``role`` is additive and optional (Mirador Agent Panels backend sprint —
    per-role activity attribution): existing callers that omit it are
    unaffected and persist ``NULL``, exactly as before this sprint. Callers
    pass the resolved role name (``TaskConfig.role``) when one genuinely
    applies to the step being recorded; a plain, non-role step passes
    ``None`` (honest NULL) rather than an invented role.

    ``cache_read_tokens``/``cache_creation_tokens`` are additive and optional
    (usage-capture-modelusage fix): prompt-cache tokens, billed at different
    rates than base input/output tokens — existing callers that omit them
    are unaffected and persist ``NULL`` for both, exactly as before this fix.

    ``turns`` is additive and optional, and is not a cost field: it is what
    makes the two cache columns readable. A step that ran a single turn wrote
    its prompt to cache and ended before it could read much of it back, so
    its ``cache_read / cache_creation`` falls below 1.0 by construction no
    matter how well the prompt is ordered. Without it, a short step and a
    genuinely wasteful prefix look identical and the cache detector called
    both pathological. Omitting callers persist ``NULL``, which the detector
    reports as unclassified rather than resolving by guess.
    """
    init_db()
    # Choke point: `detail` often carries `str(exc)` from a failed step, which
    # may echo a resolved ${secret:NAME} value an agent printed. Redact before
    # it's persisted to SQLite.
    from hivepilot.services.config_provenance import redact_text

    detail = redact_text(detail) if detail is not None else detail
    # Choke point: a step no agent performed must not carry an agent. Role is
    # declared per *task*, but a task's stages can mix agent work with plain
    # shell commands, so the task's role reaches both. Persisting it on a
    # `shell` step would credit an agent with work no model did — inflating
    # its step count and making every per-agent figure mean less the more
    # shell a pipeline uses.
    #
    # Only providers KNOWN not to invoke a model strip attribution. A NULL
    # provider is a telemetry gap, not proof that no agent ran: dropping the
    # role there would erase real attribution to guard against a case we
    # cannot confirm, so the uncertain direction keeps it.
    if provider is not None and provider in NON_MODEL_PROVIDERS:
        role = None
    with db.connect() as conn:
        conn.execute(
            db.ph(
                "INSERT INTO steps "
                "(run_id, step, status, detail, provider, model, "
                "input_tokens, output_tokens, cost_usd, role, "
                "cache_read_tokens, cache_creation_tokens, turns) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                run_id,
                step,
                status,
                detail,
                provider,
                model,
                input_tokens,
                output_tokens,
                cost_usd,
                role,
                cache_read_tokens,
                cache_creation_tokens,
                turns,
            ),
        )
    # Announce the step on the event stream. Until this existed the stream
    # carried a run's endpoints and never its middle -- `state.run_start`,
    # then silence for ten minutes, then `state.verdict` -- so anything
    # watching could see that a pipeline ran but never the roles working.
    #
    # Emitted here, after the insert and after the attribution correction
    # above, so the event says exactly what the database says. Computing
    # "which agent did this" a second time somewhere else is how the two
    # drift, and a live board that credits an agent with `bash` work is
    # worse than one that shows nothing.
    #
    # `detail` is already redacted at the top of this function, which matters
    # more here than for SQLite: this stream is meant to be rendered into a
    # terminal pane.
    logger.info(
        "state.step",
        run_id=run_id,
        step=step,
        status=status,
        role=role,
        provider=provider,
        model=model,
        cost_usd=cost_usd,
        detail=detail,
    )
    if _METRICS_AVAILABLE and _metrics is not None:
        try:
            _metrics.steps_total.labels(status=status).inc()
        except Exception:  # noqa: BLE001
            pass


def attach_run_artifacts(run_id: int, artifacts_path: str) -> None:
    """Point a finished run at the artifact directory holding its output.

    A *successful* run records no ``detail`` — `complete_run(run_id,
    "success")` is called without one, so the agent's actual output lives
    only in ``<run_dir>/artifacts/results.json`` on disk with nothing in the
    database pointing at it. When delivery of that output failed (see the
    `_reply_results` fix), the work became unreachable: the run row said
    "success" and held nothing else (observed: run 267, `noxys-ciso`).

    Writes ONLY into an empty ``detail`` — a failure/denial message already
    stored there is the more important record and is never overwritten. This
    is deliberately a pointer, not the output itself: agent dumps reach
    hundreds of KB and the runs table is queried on every status request.
    """
    init_db()
    from hivepilot.services.config_provenance import redact_text

    pointer = redact_text(f"artifacts: {artifacts_path}")
    with db.connect() as conn:
        conn.execute(
            db.ph(
                "UPDATE runs SET detail=? WHERE id=? "
                "AND (detail IS NULL OR detail='')"  # never clobber a failure message
            ),
            (pointer, run_id),
        )


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
                db.ph(
                    "SELECT * FROM runs WHERE tenant=? ORDER BY started_at DESC, id DESC LIMIT ?"
                ),
                (tenant, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                db.ph("SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?"), (limit,)
            ).fetchall()
    return [dict(row) for row in rows]


def get_steps_for_run(run_id: int) -> list[dict[str, Any]]:
    init_db()
    with db.connect() as conn:
        rows = conn.execute(
            db.ph("SELECT * FROM steps WHERE run_id=? ORDER BY timestamp"), (run_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def interaction_role(interaction_id: int | None) -> str | None:
    """Return the role KEY an interaction was recorded under, or ``None``.

    `interactions.actor` holds a human-facing label ("Hugo (CISO)") built from
    a role's display name and title. Parsing it back would bake a tenant's
    persona naming into the engine, so the machine key travels in `metadata`
    instead, alongside `pipeline`/`stage_index`.

    Returns ``None`` for a missing row, absent metadata, unparseable JSON, or
    a row written before the key existed -- attribution is a nice-to-have on
    a diagnostic path and must never raise into a run.
    """
    if not interaction_id:
        return None
    try:
        with db.connect() as conn:
            row = conn.execute(
                db.ph("SELECT metadata FROM interactions WHERE id = ?"), (interaction_id,)
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        meta = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    role = meta.get("role") if isinstance(meta, dict) else None
    return role.strip() if isinstance(role, str) and role.strip() else None


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
    pipeline_run_id: int | None = None,
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
                "(run_id, project, task, role, kind, decision, confidence, summary, "
                "pipeline_run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                run_id,
                project,
                task,
                role,
                kind,
                decision,
                confidence,
                summary,
                pipeline_run_id,
            ),
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


def list_failed_steps(run_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Steps of *run_id* that did not succeed, oldest first.

    Feeds the lessons distiller, which until now saw only verdicts,
    interactions and the run-level outcome -- all of which describe
    COMPLETED work being judged. A step that died produced none of them, so
    the failures that actually cost money taught nothing.

    Oldest first because the first failure is usually the cause and the rest
    the consequence: a denied tool, then a context blowout, then a
    fail-fast. Reading them in order is reading the causal chain.

    `detail` was already redacted by `record_step`'s choke point, so it is
    safe to hand onward -- but the distiller redacts the whole prompt again
    before egress regardless.
    """
    init_db()
    with db.connect() as conn:
        rows = conn.execute(
            db.ph(
                "SELECT step, role, status, detail, provider, model FROM steps "
                "WHERE run_id=? AND status NOT IN ('success', 'skipped') "
                "ORDER BY id ASC LIMIT ?"
            ),
            (run_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


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


def list_verdicts(
    tenant: str | None = None,
    role: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Tenant-scoped, optionally role-filtered read of `verdicts`, newest
    first (Mirador Agent Panels backend sprint, backing `GET /v1/verdicts`).

    Does NOT weaken/replace `list_recent_verdicts` above -- that function is
    unchanged and keeps its own (unfiltered-by-tenant, run_id-only) contract
    for existing callers (`orchestrator._distill_lessons_for_run`).

    Fail-closed tenant scoping: `verdicts` carries no `tenant` column of its
    own (unlike `steps`/`runs`) -- every orchestrator call site today passes
    a real `run_id`, but the column itself is nullable and does not enforce
    that. Resolving a caller's tenant is therefore only possible via `LEFT
    JOIN runs`: when a concrete `tenant` is requested, the WHERE clause
    requires `r.tenant = ?`, which a NULL `run_id` (or a `run_id` that
    doesn't match any `runs` row) can never satisfy -- such a row is
    excluded from every tenant-scoped view rather than risking cross-tenant
    leakage. Only an unscoped call (`tenant=None`, i.e. an admin caller)
    can see a row whose tenant is unresolvable.
    """
    init_db()
    clauses: list[str] = []
    params: list[Any] = []
    if tenant is not None:
        clauses.append("r.tenant=?")
        params.append(tenant)
    if role is not None:
        clauses.append("v.role=?")
        params.append(role)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT v.* FROM verdicts v
        LEFT JOIN runs r ON r.id = v.run_id
        {where}
        ORDER BY v.id DESC LIMIT ?
    """
    params.append(limit)
    with db.connect() as conn:
        rows = conn.execute(db.ph(sql), tuple(params)).fetchall()
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


def list_lessons_by_tenant(
    tenant: str | None = None,
    role: str | None = None,
    validated_only: bool | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Tenant-scoped, optionally role-filtered read of `lessons`, newest
    first (Mirador Agent Panels backend sprint, backing `GET /v1/lessons`).

    Sibling to `list_verdicts` above -- same fail-closed `LEFT JOIN runs`
    tenant-scoping rationale applies here verbatim (`lessons` also carries
    no `tenant` column of its own). Does NOT weaken/replace `list_lessons`:
    that function keeps its own (project-required, tenant-unaware,
    validated_only defaulting to True) contract unchanged for existing
    callers. Unlike `list_lessons`, `validated_only` here defaults to
    ``None`` (no filter -- both validated lessons AND fresh candidates are
    returned) since a tenant-wide activity view has no single project to
    scope validation status to; pass ``True``/``False`` explicitly to
    filter.
    """
    init_db()
    clauses: list[str] = []
    params: list[Any] = []
    if tenant is not None:
        clauses.append("r.tenant=?")
        params.append(tenant)
    if role is not None:
        clauses.append("l.role=?")
        params.append(role)
    if validated_only is not None:
        clauses.append("l.validated=?")
        params.append(int(validated_only))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT l.* FROM lessons l
        LEFT JOIN runs r ON r.id = l.run_id
        {where}
        ORDER BY l.id DESC LIMIT ?
    """
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
    # A lesson with NO role is a GENERAL lesson -- it applies to every role,
    # and its `task` is the pipeline it came from rather than a stage.
    #
    # Filtering it on equality made 214 of the 219 lessons on the box
    # unreachable: the pipeline-end distiller stores `role=NULL,
    # task=<pipeline>` (correctly -- a distillation covering fifteen stages
    # belongs to no single role), while injection asks per stage with
    # `role="reviewer", task="noxys-reviewer"`. The two could never meet, so
    # lessons were distilled, scored, validated, stored -- and never read
    # back. `retrieve_lessons("noxys")` returned 5; with a role, 0.
    #
    # Role-specific rows stay narrowed exactly as before, task included, so a
    # `release_manager` lesson still cannot appear in a `reviewer`'s context.
    if role is not None:
        clauses.append("(role IS NULL OR role=?)")
        params.append(role)
    if task is not None:
        clauses.append("(role IS NULL OR task IS NULL OR task=?)")
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
    """Every run row, most recent first. Pollen graph-cascade rebuild:
    `started_at` is SQLite `CURRENT_TIMESTAMP` (SECOND resolution) — two
    runs recorded within the same wall-clock second tie on that column
    alone, so `id DESC` (the table's monotonically increasing, always-
    distinct autoincrement PK) breaks the tie deterministically in favor of
    the LAST-inserted row, instead of leaving "the most recent run" to
    SQLite's unspecified tie order. Callers relying on recency —
    `hivepilot/graph_sources/pipeline_source.py`'s "last run" resolution
    chief among them — depend on this being deterministic."""
    init_db()
    with db.connect() as conn:
        if tenant is not None:
            rows = conn.execute(
                db.ph("SELECT * FROM runs WHERE tenant=? ORDER BY started_at DESC, id DESC"),
                (tenant,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC, id DESC").fetchall()
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


def agent_telemetry_freshness() -> tuple[int, str | None]:
    """`(row count, newest timestamp)` for agent telemetry.

    Both halves, because the count alone is the misleading one: it stays
    healthy forever once an exporter has ever worked, and only the age of the
    newest row says whether it still does.
    """
    init_db()
    with db.connect() as conn:
        row = conn.execute("SELECT COUNT(*), MAX(recorded_at) FROM agent_telemetry").fetchone()
    if not row:
        return 0, None
    return int(row[0] or 0), row[1]


def record_context_truncation(
    *,
    run_id: int | None,
    project: str | None,
    role: str | None,
    total_chars: int,
    budget: int,
    dropped_chars: int,
    stages: int,
    largest_stage_chars: int,
    budget_basis: str | None,
    tenant: str = "default",
) -> int:
    """Persist one truncation, so it stops being findable only in a log file.

    Run 639 took a week to diagnose because the only trace of it was a
    `logger.warning`: `cap` mode had kept the TAIL of the joined prior context,
    ~90% of the run vanished along with both verdicts the release gate needed,
    and the gate then refused a release on a clearance that HAD been given.

    `budget_basis` is the field the warning never carried -- `derived` from the
    model's real window, or `fallback` to a configured constant. Without it a
    budget figure cannot be told apart from a guess, and the number that
    matters here is exactly the one somebody will want to tune.

    No free text is stored. Every column is a count or a fixed token, so this
    table cannot become a way for prompt content to reach a dashboard.
    """
    init_db()
    with db.connect() as conn:
        row_id = db.insert_returning_id(
            conn,
            "INSERT INTO context_truncations "
            "(run_id, project, role, total_chars, budget, dropped_chars, stages, "
            "largest_stage_chars, budget_basis, tenant) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                project,
                role,
                total_chars,
                budget,
                dropped_chars,
                stages,
                largest_stage_chars,
                budget_basis,
                tenant,
            ),
        )
    return row_id


def context_truncations(*, tenant: str = "default", limit: int = 200) -> list[dict]:
    """Recent truncation rows, newest first.

    An empty list means nothing was RECORDED -- never that nothing was
    truncated. `summarise_truncations` keeps that distinction in the numbers it
    reports; this function only has to avoid destroying it.
    """
    init_db()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT run_id, project, role, total_chars, budget, dropped_chars, "
            "stages, largest_stage_chars, budget_basis, recorded_at "
            "FROM context_truncations WHERE tenant = ? "
            "ORDER BY id DESC LIMIT ?",
            (tenant, int(limit)),
        ).fetchall()
    keys = (
        "run_id",
        "project",
        "role",
        "total_chars",
        "budget",
        "dropped_chars",
        "stages",
        "largest_stage_chars",
        "budget_basis",
        "recorded_at",
    )
    return [dict(zip(keys, row, strict=False)) for row in rows]


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


# ---------------------------------------------------------------------------
# Swarm Phase 1 -- swarm_events persistence (the audit/dedupe/claim source of
# truth for hivepilot.services.swarm_service, regardless of transport).
# ---------------------------------------------------------------------------


def insert_swarm_event(event: Any) -> bool:
    """Idempotently persist *event* (a `hivepilot.swarm.models.Event`) as a
    new `pending` row. Returns `True` iff THIS call actually inserted a new
    row (a genuinely new event id); `False` when a row for `event.id` already
    existed (`ON CONFLICT(id) DO NOTHING` no-op) -- the caller
    (`swarm_service.publish_event`) treats `False` as DEDUPED. `ON CONFLICT
    ... DO NOTHING` (rather than SQLite-only `INSERT OR IGNORE`) is portable
    to the optional Postgres backend (see `hivepilot.services.db`).
    """
    init_db()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                "INSERT INTO swarm_events "
                "(id, type, payload, tenant, origin_instance, sig, status, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?) "
                "ON CONFLICT(id) DO NOTHING"
            ),
            (
                event.id,
                event.type,
                json.dumps(event.payload),
                event.tenant,
                event.origin_instance,
                event.sig,
                event.ts,
            ),
        )
        inserted = cur.rowcount == 1
    logger.info(
        "state.swarm_publish",
        event_id=event.id,
        type=event.type,
        tenant=event.tenant,
        inserted=inserted,
    )
    return inserted


def get_swarm_event(event_id: str) -> dict[str, Any] | None:
    """Return the single `swarm_events` row for *event_id*, or `None`."""
    init_db()
    with db.connect() as conn:
        row = conn.execute(db.ph("SELECT * FROM swarm_events WHERE id=?"), (event_id,)).fetchone()
    return dict(row) if row else None


def claim_swarm_event(event_id: str, *, claimed_by: str) -> bool:
    """Atomically claim *event_id* for *claimed_by* -- the exactly-once
    primitive `hivepilot.swarm.transport.Transport.claim` implementations
    delegate to (both "poll" and "redis" call this SAME function, so
    exactly-once is guaranteed identically regardless of which broker
    delivered the notification -- see `hivepilot/swarm/redis_transport.py`'s
    module docstring for why Redis consumer groups are belt-and-suspenders
    on top of this, not a replacement for it).

    Returns `True` iff THIS call's `UPDATE ... WHERE status='pending'`
    actually changed the row (this call won the race); `False` when the row
    doesn't exist, or was no longer `pending` (already claimed/done by
    someone else).
    """
    init_db()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                "UPDATE swarm_events SET status='claimed', claimed_by=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'"
            ),
            (claimed_by, event_id),
        )
        claimed = cur.rowcount == 1
    logger.info("state.swarm_claim", event_id=event_id, claimed_by=claimed_by, claimed=claimed)
    return claimed


def mark_swarm_event_running(event_id: str, *, claimed_by: str) -> bool:
    """Atomically transition *event_id* from `claimed` -> `running`, but
    ONLY when it is currently claimed BY *claimed_by* -- the atomic
    handler-dispatch gate `swarm_service.process_claimed_event` uses BEFORE
    ever invoking a handler (HIGH #2 fix, opus security review).

    A plain `get_swarm_event()` + `if status == 'claimed'` read-then-act
    check is a TOCTOU race: it never verifies WHO holds the claim, so a
    race-LOSER instance B (whose own `claim_swarm_event` call already
    returned `False`) could still read the row as `status='claimed'`
    (claimed by WINNER instance A) and run the handler too -- a second,
    unauthorized execution of whatever `pr_ready` (or any future event type)
    triggers. Gating on a single conditional `UPDATE ... WHERE id=? AND
    status='claimed' AND claimed_by=?` closes this: only the actual owner's
    call can ever flip `claimed` -> `running` (`rowcount == 1`); every other
    caller (a different `claimed_by`, or a second call for an event already
    past `claimed`) gets `rowcount == 0` -> `False` -> no handler invocation.
    This ALSO makes handler invocation itself exactly-once even for the
    legitimate owner: two concurrent `process_claimed_event` calls for the
    SAME owner can never both win this same atomic transition.
    """
    init_db()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                "UPDATE swarm_events SET status='running', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='claimed' AND claimed_by=?"
            ),
            (event_id, claimed_by),
        )
        running = cur.rowcount == 1
    logger.info("state.swarm_running", event_id=event_id, claimed_by=claimed_by, running=running)
    return running


def mark_swarm_event_done(event_id: str) -> bool:
    """Atomically transition *event_id* from `running` -> `done` (a handler
    finished processing it). Returns `False` (no-op) when the row isn't
    currently `running` -- e.g. it never passed through
    `mark_swarm_event_running` (must be claimed AND running first) or is
    already `done` (a duplicate/redelivered completion call) -- this is the
    persistence half of handler idempotency-by-event.id (see
    `hivepilot.services.swarm_service.process_claimed_event`).
    """
    init_db()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                "UPDATE swarm_events SET status='done', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='running'"
            ),
            (event_id,),
        )
        done = cur.rowcount == 1
    logger.info("state.swarm_done", event_id=event_id, done=done)
    return done


def mark_swarm_event_failed(event_id: str) -> bool:
    """Atomically transition *event_id* from `running` -> `failed` (bug-debt
    fix: a claimed event whose handler RAISED must land in a DEFINED
    terminal state, never stay `running` forever with no reaper).

    `failed` is a genuine terminal state, exactly like `done`/`skipped` --
    NOT `pending` -- so `claim_swarm_event`'s `WHERE status='pending'` gate
    makes it structurally impossible for this or any other instance to ever
    re-claim/re-run a failed event (see `swarm_service.process_claimed_
    event`'s docstring for the at-most-once rationale: a handler triggers a
    pipeline run, which is not a safe-to-silently-retry side effect).
    Returns `False` (no-op) when the row isn't currently `running` -- e.g. a
    duplicate/redelivered failure call for an event already `failed`/`done`.
    """
    init_db()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                "UPDATE swarm_events SET status='failed', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='running'"
            ),
            (event_id,),
        )
        failed = cur.rowcount == 1
    logger.info("state.swarm_failed", event_id=event_id, failed=failed)
    return failed


def mark_swarm_event_skipped(event_id: str) -> bool:
    """Atomically transition *event_id* from `pending` -> `skipped` (e.g. a
    signature that failed verification -- a permanently-corrupt event should
    never be retried by any instance in the fleet). Returns `False` (no-op)
    when the row isn't currently `pending` (already claimed/done/skipped) --
    a signature failure detected AFTER a legitimate claim never overwrites
    that claim's true status.
    """
    init_db()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                "UPDATE swarm_events SET status='skipped', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='pending'"
            ),
            (event_id,),
        )
        skipped = cur.rowcount == 1
    logger.info("state.swarm_skipped", event_id=event_id, skipped=skipped)
    return skipped


def list_pending_swarm_events(
    types: list[str] | None = None,
    *,
    tenants: list[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return `pending` `swarm_events` rows, oldest first (FIFO claim order),
    optionally filtered by *types* and/or *tenants* -- every query is
    parameterized (never string-interpolated) so a caller-controlled
    tenant/type list can never inject SQL.
    """
    init_db()
    clauses = ["status='pending'"]
    params: list[Any] = []
    if types:
        clauses.append(f"type IN ({','.join('?' for _ in types)})")
        params.extend(types)
    if tenants:
        clauses.append(f"tenant IN ({','.join('?' for _ in tenants)})")
        params.extend(tenants)
    where = " AND ".join(clauses)
    sql = f"SELECT * FROM swarm_events WHERE {where} ORDER BY created_at ASC, id ASC LIMIT ?"
    params.append(limit)
    with db.connect() as conn:
        rows = conn.execute(db.ph(sql), tuple(params)).fetchall()
    return [dict(row) for row in rows]


def verdicts_for_pipeline_run(pipeline_run_id: int) -> list[dict[str, Any]]:
    """Every verdict recorded against *pipeline_run_id*, oldest first."""
    init_db()
    with db.connect() as conn:
        rows = conn.execute(
            db.ph(
                "SELECT id, run_id, role, kind, decision, confidence, summary "
                "FROM verdicts WHERE pipeline_run_id = ? ORDER BY id"
            ),
            (pipeline_run_id,),
        ).fetchall()
    return [
        {
            "id": r[0],
            "run_id": r[1],
            "role": r[2],
            "kind": r[3],
            "decision": r[4],
            "confidence": r[5],
            "summary": r[6],
        }
        for r in rows
    ]


def agreement_rows(limit: int = 500) -> list[dict[str, Any]]:
    """What the agents decided beside what the human then did.

    The join the autonomy ladder is built on, and which returned zero rows
    until verdicts carried a pipeline run id: approvals are pipeline-level and
    half the verdicts were not.

    A row per verdict that belongs to a run a human also acted on. Rows where
    nobody acted are excluded -- an unanswered approval is not a disagreement,
    and counting it as one would flatter or damn the agents arbitrarily.
    """
    init_db()
    with db.connect() as conn:
        rows = conn.execute(
            db.ph(
                "SELECT v.id, v.pipeline_run_id, v.role, v.decision, v.confidence, "
                "a.status, a.approved_by "
                "FROM verdicts v "
                "JOIN approvals a ON a.run_id = v.pipeline_run_id "
                "WHERE v.decision IS NOT NULL AND a.status IS NOT NULL "
                "ORDER BY v.id DESC LIMIT ?"
            ),
            (int(limit),),
        ).fetchall()
    return [
        {
            "verdict_id": r[0],
            "pipeline_run_id": r[1],
            "role": r[2],
            "decision": r[3],
            "confidence": r[4],
            "human": r[5],
            "approved_by": r[6],
        }
        for r in rows
    ]
