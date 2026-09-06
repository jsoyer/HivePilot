"""Memory-quality instrumentation subsystem — backs Pollen's Memory > Quality view.

Core HivePilot has RUN analytics (`analytics_service.py`) but no source of
truth for how well the *memory* layer (e.g. the `mem0` plugin's `recall`/
`store` hooks) is actually serving agents. This module is that source: it
records memory search/read/store events plus human evaluations, and exposes
tenant-scoped, windowed aggregates over them — mirroring
`analytics_service.py`'s query/window/aggregate style and `state_service.py`'s
migration/connection idioms exactly (``db.connect()`` / ``db.ph()`` /
``db.autoincrement_pk()`` / ``db.insert_returning_id()``).

**Additive-only, own tables.** Two new SQLite tables (``memory_events``,
``memory_evaluations``) are created via ``init_db()`` (idempotent
``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS``, called at
the top of every public function below, exactly like every function in
`state_service.py` calls its own ``init_db()``). No existing table is ever
touched — this module does not import or modify `state_service.py`, it just
shares the same underlying ``state.db`` file via `db.py`.

**Everything OPT-IN and additive.** Nothing in core HivePilot calls
``record_search``/``record_read``/``record_store`` today except the `mem0`
plugin's `recall`/`store` hooks (and only when ``settings.mem0_enabled`` is
True). When nothing is instrumented, both tables stay empty and every query
function below returns zeros/``[]`` — NEVER fabricated data (see each
function's docstring for its specific zero-safe contract).

**Best-effort recording, never raises.** ``record_search``/``record_read``/
``record_store``/``record_evaluation`` each wrap their body in a broad
``try/except Exception`` and log-and-swallow any failure (bad DB, bad
input, whatever) — mirrors the "a hook must never crash a run" discipline
`plugins/mem0.py`'s own `recall`/`store` already follow. Instrumenting a
memory operation must never be able to break that operation.

**Tenant scoping.** Every query function accepts ``tenant: str | None`` —
``None`` means unscoped (all tenants; mirrors `analytics_service.py`'s
``_analytics_tenant`` convention: an ``admin`` caller passes ``None``, every
other caller passes their own ``tenant``). A query function ALWAYS filters
by the given tenant when one is provided; there is no code path in this
module that can return another tenant's rows when a concrete tenant string
is passed in — the security-critical invariant `tests/test_memory_service.py`
`TestTenantIsolation` asserts directly.

**Known limitation: writers may not always have a real tenant.** This
module's own scoping is correct end-to-end (every `record_*`/query function
above takes/filters an explicit ``tenant``), but a *caller* that has no
tenant signal available (e.g. `plugins/mem0.py`'s `recall`/`store` hooks —
see their own docstrings) falls back to the ``tenant="default"`` default on
every `record_*` function. Until such a caller has a real tenant to thread
through, its events are effectively single-tenant (`"default"`) data —
that's a gap in what the CALLER can attribute, not a scoping bug here.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from hivepilot.services import db
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Migration (mirrors state_service.init_db()'s idempotent CREATE TABLE IF
# NOT EXISTS pattern, called at the top of every public function below).
# ---------------------------------------------------------------------------


def init_db() -> None:
    pk = db.autoincrement_pk()
    with db.connect() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS memory_events (
                id {pk},
                tenant TEXT NOT NULL DEFAULT 'default',
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                op TEXT NOT NULL,
                namespace TEXT,
                query_or_key TEXT,
                result_count INTEGER,
                found INTEGER,
                freshness_seconds REAL,
                actor TEXT
            )
            """
        )
        # Added after the fact: two backends write here now and `namespace` is
        # the same project:task:role key for both, so nothing else could tell
        # a mem0 recall from an Obsidian one. Rows predating this column are
        # mem0's -- it was the only instrumented backend, which is precisely
        # why Obsidian read as idle.
        from hivepilot.services.state_service import _add_column_if_missing

        _add_column_if_missing(conn, "memory_events", "backend TEXT")
        # The run a recall served. Without it the table records THAT a search
        # happened and never what the step did with it, so "does memory change
        # the output" cannot be asked of the data -- which makes every choice
        # between memory backends a preference dressed as a decision.
        _add_column_if_missing(conn, "memory_events", "run_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_events_tenant_ts ON memory_events(tenant, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_events_tenant_ns "
            "ON memory_events(tenant, namespace)"
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS memory_evaluations (
                id {pk},
                tenant TEXT NOT NULL DEFAULT 'default',
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                namespace TEXT,
                ref_key TEXT,
                useful INTEGER,
                note TEXT,
                actor TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_evaluations_tenant_ts "
            "ON memory_evaluations(tenant, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_evaluations_tenant_ns "
            "ON memory_evaluations(tenant, namespace)"
        )


# ---------------------------------------------------------------------------
# Recording — best-effort, NEVER raise into the caller (see module docstring).
#
# NOT redacted through `config_provenance.redact_text` (unlike
# `plugins/mem0.py`'s `store()` CONTENT path, which explicitly does redact —
# see that function's own "Defense-in-depth" comment). `namespace`/
# `query`/`key` here are task/step identity keys (e.g. `project:task[:role]`
# — see `plugins/mem0.py`'s `_memory_key`) or a short task/step-derived
# search query, never resolved secret VALUES or arbitrary user/agent
# content — there is no path today where a `${secret:NAME}` value could
# land in one of these fields. If a future caller ever derives one of these
# from free-text content instead of a task/step identifier, it MUST redact
# before calling into this module — this module does not do it for you.
# ---------------------------------------------------------------------------


def record_search(
    *,
    namespace: str,
    query: str | None,
    result_count: int | None,
    actor: str | None,
    tenant: str = "default",
    backend: str | None = None,
    run_id: int | None = None,
    freshness_seconds: float | None = None,
) -> None:
    """Record a memory search event. Best-effort: NEVER raises."""
    try:
        init_db()
        with db.connect() as conn:
            conn.execute(
                db.ph(
                    "INSERT INTO memory_events "
                    "(tenant, op, namespace, query_or_key, result_count, "
                    "freshness_seconds, actor, backend, run_id) "
                    "VALUES (?, 'search', ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    tenant,
                    namespace,
                    query,
                    result_count,
                    freshness_seconds,
                    actor,
                    backend,
                    run_id,
                ),
            )
    except Exception as exc:  # noqa: BLE001 — instrumentation must never break the caller
        logger.warning("memory_service.record_search_failed", error=str(exc))


def record_read(
    *,
    namespace: str,
    key: str | None,
    found: bool,
    actor: str | None,
    tenant: str = "default",
    backend: str | None = None,
    run_id: int | None = None,
    freshness_seconds: float | None = None,
) -> None:
    """Record a memory read (fetch-by-key) event. Best-effort: NEVER raises."""
    try:
        init_db()
        with db.connect() as conn:
            conn.execute(
                db.ph(
                    "INSERT INTO memory_events "
                    "(tenant, op, namespace, query_or_key, found, freshness_seconds, "
                    "actor, backend, run_id) "
                    "VALUES (?, 'read', ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    tenant,
                    namespace,
                    key,
                    int(bool(found)),
                    freshness_seconds,
                    actor,
                    backend,
                    run_id,
                ),
            )
    except Exception as exc:  # noqa: BLE001 — instrumentation must never break the caller
        logger.warning("memory_service.record_read_failed", error=str(exc))


def record_store(
    *,
    namespace: str,
    key: str | None,
    actor: str | None,
    tenant: str = "default",
    backend: str | None = None,
    run_id: int | None = None,
) -> None:
    """Record a memory store (write) event. Best-effort: NEVER raises."""
    try:
        init_db()
        with db.connect() as conn:
            conn.execute(
                db.ph(
                    "INSERT INTO memory_events "
                    "(tenant, op, namespace, query_or_key, actor, backend, run_id) "
                    "VALUES (?, 'store', ?, ?, ?, ?, ?)"
                ),
                (tenant, namespace, key, actor, backend, run_id),
            )
    except Exception as exc:  # noqa: BLE001 — instrumentation must never break the caller
        logger.warning("memory_service.record_store_failed", error=str(exc))


def record_evaluation(
    *,
    namespace: str,
    useful: bool,
    actor: str | None,
    ref_key: str | None = None,
    note: str | None = None,
    tenant: str = "default",
) -> None:
    """Record a human evaluation of a memory ("was this useful?"). Best-effort:
    NEVER raises. ``useful`` is coerced via ``bool()`` — any truthy caller
    value is accepted without raising (defensive; the API layer's Pydantic
    model is the real type gate for HTTP callers)."""
    try:
        init_db()
        with db.connect() as conn:
            conn.execute(
                db.ph(
                    "INSERT INTO memory_evaluations "
                    "(tenant, namespace, ref_key, useful, note, actor) "
                    "VALUES (?, ?, ?, ?, ?, ?)"
                ),
                (tenant, namespace, ref_key, int(bool(useful)), note, actor),
            )
    except Exception as exc:  # noqa: BLE001 — instrumentation must never break the caller
        logger.warning("memory_service.record_evaluation_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Time-window resolution (mirrors analytics_service._resolve_window, minus
# the since/until override — this module only ever takes a relative `days`
# window, matching the "days"/window query param the /v1/memory/* endpoints
# expose, mirroring /analytics/*'s convention).
# ---------------------------------------------------------------------------

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _resolve_window(days: int | None) -> str | None:
    """Return a SQL-comparable ``since`` timestamp, or ``None`` for an
    unbounded (all-history) window when *days* is ``None``."""
    if days is None:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.strftime(_TS_FORMAT)


def _scope(tenant: str | None, since: str | None) -> tuple[list[str], list[Any]]:
    """Return ``(clauses, params)`` for tenant/window scoping, to be ANDed
    with whatever additional predicate a query function appends. An empty
    ``tenant`` clause list means unscoped (``tenant=None`` — admin / all
    tenants), mirroring `analytics_service.py`'s `_query_runs`."""
    clauses: list[str] = []
    params: list[Any] = []
    if tenant is not None:
        clauses.append("tenant=?")
        params.append(tenant)
    if since is not None:
        clauses.append("ts>=?")
        params.append(since)
    return clauses, params


def _where(clauses: list[str]) -> str:
    return f"WHERE {' AND '.join(clauses)}" if clauses else ""


# ---------------------------------------------------------------------------
# Queries — tenant-scoped, windowed, ALWAYS zero-safe (never divide-by-zero,
# never crash on an empty/absent table — see each docstring).
# ---------------------------------------------------------------------------


def reality_summary(tenant: str | None = None, days: int | None = 30) -> dict[str, Any]:
    """Aggregate memory-quality summary for *tenant* over the last *days*.

    Fail-safe: every rate is ``0.0`` when its denominator is ``0`` (empty
    table / empty window) — NEVER a ``ZeroDivisionError``, never ``None``.
    ``avg_freshness_seconds`` is ``0.0`` when no event in-window carries a
    freshness value (SQL ``AVG`` over zero rows is ``NULL`` — mapped to
    ``0.0`` here rather than surfaced as a JSON ``null`` the view would have
    to special-case).
    """
    init_db()
    since = _resolve_window(days)
    base_clauses, base_params = _scope(tenant, since)

    search_clauses = base_clauses + ["op='search'"]
    with db.connect() as conn:
        search_row = conn.execute(
            db.ph(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN result_count=0 THEN 1 ELSE 0 END) AS no_result "
                f"FROM memory_events {_where(search_clauses)}"
            ),
            tuple(base_params),
        ).fetchone()
        total_searches = search_row["total"] or 0
        no_result_count = search_row["no_result"] or 0

        fresh_row = conn.execute(
            db.ph(
                "SELECT AVG(freshness_seconds) AS avg_fresh FROM memory_events "
                f"{_where(base_clauses + ['freshness_seconds IS NOT NULL'])}"
            ),
            tuple(base_params),
        ).fetchone()
        avg_freshness = fresh_row["avg_fresh"]

        eval_row = conn.execute(
            db.ph(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN useful=1 THEN 1 ELSE 0 END) AS useful_count "
                f"FROM memory_evaluations {_where(base_clauses)}"
            ),
            tuple(base_params),
        ).fetchone()
        total_evaluations = eval_row["total"] or 0
        useful_count = eval_row["useful_count"] or 0

    search_success_rate = (
        round((total_searches - no_result_count) / total_searches, 4) if total_searches > 0 else 0.0
    )
    declared_reliability = (
        round(useful_count / total_evaluations, 4) if total_evaluations > 0 else 0.0
    )

    return {
        "search_success_rate": search_success_rate,
        "total_searches": total_searches,
        "no_result_count": no_result_count,
        "avg_freshness_seconds": round(avg_freshness, 3) if avg_freshness is not None else 0.0,
        "declared_reliability": declared_reliability,
        "total_evaluations": total_evaluations,
    }


def gaps_by_namespace(
    tenant: str | None = None, days: int | None = 30, *, top_queries_limit: int = 5
) -> list[dict[str, Any]]:
    """No-result searches (``result_count=0``) grouped by ``namespace``,
    sorted by descending gap count. Each group's ``top_queries`` is its most
    frequent non-empty query strings (deterministic ``Counter.most_common``
    tie-break: insertion/encounter order). Returns ``[]`` when there are no
    no-result searches in-window for *tenant* — never crashes on an empty
    table."""
    init_db()
    since = _resolve_window(days)
    clauses, params = _scope(tenant, since)
    clauses = clauses + ["op='search'", "result_count=0"]

    with db.connect() as conn:
        rows = conn.execute(
            db.ph(f"SELECT namespace, query_or_key FROM memory_events {_where(clauses)}"),
            tuple(params),
        ).fetchall()

    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[row["namespace"] or "unknown"].append(row["query_or_key"])

    result: list[dict[str, Any]] = []
    for namespace, queries in grouped.items():
        query_counts = Counter(q for q in queries if isinstance(q, str) and q)
        result.append(
            {
                "namespace": namespace,
                "no_result_count": len(queries),
                "top_queries": [q for q, _ in query_counts.most_common(top_queries_limit)],
            }
        )
    result.sort(key=lambda r: -r["no_result_count"])
    return result


def recent_evaluations(tenant: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Most recent human evaluations for *tenant*, newest first. Returns
    ``[]`` when there are none — never crashes on an empty table."""
    init_db()
    clauses, params = _scope(tenant, None)
    with db.connect() as conn:
        rows = conn.execute(
            db.ph(
                "SELECT ts, namespace, ref_key, useful, note, actor FROM memory_evaluations "
                f"{_where(clauses)} ORDER BY ts DESC, id DESC LIMIT ?"
            ),
            (*params, limit),
        ).fetchall()
    return [
        {
            "ts": row["ts"],
            "namespace": row["namespace"],
            "ref_key": row["ref_key"],
            "useful": bool(row["useful"]) if row["useful"] is not None else None,
            "note": row["note"],
            "actor": row["actor"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# growth_summary (Pollen data endpoints sprint) -- GET /v1/memory/growth.
# ---------------------------------------------------------------------------


def growth_summary(tenant: str | None = None, days: int | None = 30) -> dict[str, Any]:
    """Memory growth aggregates for Pollen's growth panel, derived from
    `memory_events` (`op='store'` rows only -- search/read events don't
    represent a memory being CREATED).

    **What's real vs. what's not.** `total`/`memories_by_namespace`/
    `growth_series`/`by_actor` are all real counts of STORE EVENTS this
    module has recorded (i.e. how many times `plugins/mem0.py`'s `store()`
    hook fired) -- NOT necessarily the current distinct memory count inside
    mem0's own store (mem0 may dedupe/merge/evict independently, and this
    module has no way to query mem0's client for that count: `mem0ai` isn't
    even installed as a hivepilot dependency -- see `plugins/mem0.py`'s own
    docstring for the "optional, lazily imported" contract). ``source``
    documents this explicitly so a caller never mistakes an event count for
    mem0's live store size.

    A TRUE human vs. agent authorship split is investigated and confirmed
    NOT available: every recorded store event originates from the SAME
    automated `mem0` plugin hook (`recall`/`store`, both lifecycle hooks
    the orchestrator invokes) -- there is no human-initiated write path
    into mem0 today. `authorship` is therefore always ``None`` here (never
    fabricated as e.g. ``{"human": 0, "agent": N}``, which would falsely
    imply the distinction is tracked). `by_actor` is offered instead: a
    REAL breakdown by the ``actor`` field each store event recorded (the
    invoking task's ROLE, or ``"system"`` as a fallback -- see
    `plugins/mem0.py`'s `record_store` call site) -- genuinely available
    data, just not the same thing as human/agent authorship.

    Tenant-scoped and windowed exactly like `reality_summary`/
    `gaps_by_namespace` (mirrors `_scope`/`_resolve_window`). Zero-safe:
    every list is ``[]`` and ``total`` is ``0`` on an empty/no-match table
    -- never crashes, never a fabricated number.
    """
    init_db()
    since = _resolve_window(days)
    clauses, params = _scope(tenant, since)
    clauses = clauses + ["op='store'"]

    with db.connect() as conn:
        rows = conn.execute(
            db.ph(f"SELECT ts, namespace, actor FROM memory_events {_where(clauses)}"),
            tuple(params),
        ).fetchall()

    total = len(rows)
    by_namespace: dict[str, int] = defaultdict(int)
    by_actor: dict[str, int] = defaultdict(int)
    by_day: dict[str, int] = defaultdict(int)
    for row in rows:
        by_namespace[row["namespace"] or "unknown"] += 1
        by_actor[row["actor"] or "unknown"] += 1
        day_key = (row["ts"] or "")[:10]
        if day_key:
            by_day[day_key] += 1

    memories_by_namespace = [
        {"namespace": namespace, "count": count}
        for namespace, count in sorted(by_namespace.items(), key=lambda kv: -kv[1])
    ]

    by_actor_list = [
        {"actor": actor, "count": count}
        for actor, count in sorted(by_actor.items(), key=lambda kv: -kv[1])
    ]

    growth_series = [{"date": day, "created": count} for day, count in sorted(by_day.items())]

    return {
        "total": total,
        "memories_by_namespace": memories_by_namespace,
        "growth_series": growth_series,
        "authorship": None,
        "by_actor": by_actor_list,
        "source": (
            "memory_events (recorded store events fired by the mem0 plugin hook) -- "
            "not queried directly from mem0's own store, which may dedupe/evict "
            "independently; see growth_summary()'s docstring for the full caveat."
        ),
    }


def activity_journal(tenant: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Most recent memory events (search/read/store) for *tenant*, newest
    first. Returns ``[]`` when there are none — never crashes on an empty
    table. ``result_count``/``found`` are both always present in each row
    (whichever doesn't apply to a given ``op`` is ``None``) so callers never
    have to branch on ``op`` to know which key to read."""
    init_db()
    clauses, params = _scope(tenant, None)
    with db.connect() as conn:
        rows = conn.execute(
            db.ph(
                "SELECT ts, op, namespace, query_or_key, result_count, found, "
                "freshness_seconds, actor FROM memory_events "
                f"{_where(clauses)} ORDER BY ts DESC, id DESC LIMIT ?"
            ),
            (*params, limit),
        ).fetchall()
    return [
        {
            "ts": row["ts"],
            "op": row["op"],
            "namespace": row["namespace"],
            "query_or_key": row["query_or_key"],
            "result_count": row["result_count"],
            "found": bool(row["found"]) if row["found"] is not None else None,
            "freshness_seconds": row["freshness_seconds"],
            "actor": row["actor"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Per-backend KPIs
# ---------------------------------------------------------------------------

#: Backends that always appear in a report, even with nothing recorded.
#: A backend rendered as ABSENT reads as "not applicable"; rendered as zero it
#: reads as "measured and idle". Only one of those is true, and getting it
#: wrong is how Obsidian looked useless while simply being uninstrumented.
KNOWN_BACKENDS: tuple[str, ...] = ("mem0", "obsidian", "hindsight")

#: Rows written before the `backend` column existed. Every one of them is
#: mem0's -- it was the only backend calling these recorders, verified against
#: production before this default was chosen.
_LEGACY_BACKEND = "mem0"


def backend_stats(days: int = 30) -> dict[str, dict[str, Any]]:
    """Recall/store counts per memory backend.

    `empty_searches` is the number that matters. A search returning a FULL
    top-k means the CAP was reached, not that k relevant things exist -- 115 of
    150 production searches returned exactly 5, which says nothing about
    quality. How often a recall came back with nothing is the honest signal,
    and it is the one KPI both backends can be compared on.
    """

    # Annotated explicitly: inferred from the literal alone this is
    # `dict[str, int | None]`, and assigning a timestamp string to
    # `last_activity` below is then a type error rather than the intended
    # mixed-value row.
    def _blank() -> dict[str, Any]:
        return {
            "searches": 0,
            "empty_searches": 0,
            "stores": 0,
            "reads": 0,
            "last_activity": None,
            "actors": 0,
        }

    empty: dict[str, dict[str, Any]] = {name: _blank() for name in KNOWN_BACKENDS}

    try:
        init_db()
        with db.connect() as conn:
            rows = conn.execute(
                db.ph(
                    "SELECT COALESCE(backend, ?) AS b, op, "
                    "COUNT(*), "
                    "SUM(CASE WHEN op = 'search' AND COALESCE(result_count, 0) = 0 "
                    "THEN 1 ELSE 0 END), "
                    "MAX(ts), COUNT(DISTINCT actor) "
                    "FROM memory_events "
                    "WHERE ts > datetime('now', ?) "
                    "GROUP BY b, op"
                ),
                (_LEGACY_BACKEND, f"-{int(days)} days"),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 — a KPI panel must not break on a bad DB
        logger.warning("memory_service.backend_stats_failed", error=str(exc))
        return empty

    for backend, op, count, empty_count, last_ts, actors in rows:
        slot = empty.setdefault(backend, _blank())
        if op == "search":
            slot["searches"] = int(count or 0)
            slot["empty_searches"] = int(empty_count or 0)
        elif op == "store":
            slot["stores"] = int(count or 0)
        elif op == "read":
            slot["reads"] = int(count or 0)
        slot["actors"] = max(slot["actors"], int(actors or 0))
        if last_ts and (slot["last_activity"] is None or str(last_ts) > slot["last_activity"]):
            slot["last_activity"] = str(last_ts)

    return empty


def searches_for_run(run_id: int) -> list[dict[str, Any]]:
    """Every memory search recorded against *run_id*, oldest first.

    This is the join that was missing. It makes the only question worth asking
    answerable: for a given step, which backend answered, with how many
    results, and what did that step then produce.
    """
    try:
        init_db()
        with db.connect() as conn:
            rows = conn.execute(
                db.ph(
                    "SELECT ts, backend, namespace, actor, result_count "
                    "FROM memory_events WHERE op = 'search' AND run_id = ? ORDER BY id"
                ),
                (run_id,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 — analysis must not break a caller
        logger.warning("memory_service.searches_for_run_failed", run_id=run_id, error=str(exc))
        return []

    return [
        {
            "ts": str(r[0]) if r[0] else None,
            "backend": r[1] or _LEGACY_BACKEND,
            "namespace": r[2],
            "actor": r[3],
            "result_count": int(r[4] or 0),
        }
        for r in rows
    ]
