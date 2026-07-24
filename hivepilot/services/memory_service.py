"""Memory-quality instrumentation subsystem — backs Mirador's "Réalité" view.

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
# ---------------------------------------------------------------------------


def record_search(
    *,
    namespace: str,
    query: str | None,
    result_count: int | None,
    actor: str | None,
    tenant: str = "default",
    freshness_seconds: float | None = None,
) -> None:
    """Record a memory search event. Best-effort: NEVER raises."""
    try:
        init_db()
        with db.connect() as conn:
            conn.execute(
                db.ph(
                    "INSERT INTO memory_events "
                    "(tenant, op, namespace, query_or_key, result_count, freshness_seconds, actor) "
                    "VALUES (?, 'search', ?, ?, ?, ?, ?)"
                ),
                (tenant, namespace, query, result_count, freshness_seconds, actor),
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
    freshness_seconds: float | None = None,
) -> None:
    """Record a memory read (fetch-by-key) event. Best-effort: NEVER raises."""
    try:
        init_db()
        with db.connect() as conn:
            conn.execute(
                db.ph(
                    "INSERT INTO memory_events "
                    "(tenant, op, namespace, query_or_key, found, freshness_seconds, actor) "
                    "VALUES (?, 'read', ?, ?, ?, ?, ?)"
                ),
                (tenant, namespace, key, int(bool(found)), freshness_seconds, actor),
            )
    except Exception as exc:  # noqa: BLE001 — instrumentation must never break the caller
        logger.warning("memory_service.record_read_failed", error=str(exc))


def record_store(
    *,
    namespace: str,
    key: str | None,
    actor: str | None,
    tenant: str = "default",
) -> None:
    """Record a memory store (write) event. Best-effort: NEVER raises."""
    try:
        init_db()
        with db.connect() as conn:
            conn.execute(
                db.ph(
                    "INSERT INTO memory_events "
                    "(tenant, op, namespace, query_or_key, actor) "
                    "VALUES (?, 'store', ?, ?, ?)"
                ),
                (tenant, namespace, key, actor),
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
