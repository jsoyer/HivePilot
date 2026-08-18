"""Ingest the agent CLI's own OpenTelemetry metrics.

Why this exists
---------------
HivePilot records cost and tokens per step, but only for the calls it routes
itself. Calls that build their own payload -- the review path, which is the
expensive one -- are invisible to that bookkeeping, so a measurement taken
from our own tables can read as a confident zero for spending that really
happened. The agent CLI emits its own metrics, and those do not depend on our
instrumentation being wired correctly. That independence is the point.

Why OTLP and not the console exporter
-------------------------------------
``OTEL_METRICS_EXPORTER=console`` was the obvious zero-infrastructure choice
and it is unusable here. Measured on a real call, it writes to **stdout** --
248 lines, 8.8 KB, interleaved with an 11-character answer -- and stdout is
exactly where the runner reads the agent's result from. Enabling it would
corrupt every run. It also emits Node's ``util.inspect`` format (unquoted
keys, trailing commas), which is not JSON and not a stable contract.

OTLP over ``http/json`` posts out-of-band to ``{endpoint}/v1/metrics``, leaves
stdout untouched, and is a versioned wire format. The endpoint is the API this
project already serves, so there is still no collector and nothing leaves the
host.

Identity
--------
Every data point arrives tagged with ``user.email``, ``user.id``,
``user.account_id``, ``user.account_uuid`` and ``organization.id``. None of it
answers "what did this cost", and all of it would land in a database read far
more widely than a mail header should be -- so it is dropped at ingest rather
than filtered at query time. ``session.id`` is kept: it correlates points to a
call and identifies nobody.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from hivepilot.services import db

logger = logging.getLogger(__name__)

__all__ = [
    "IDENTITY_ATTRS",
    "CacheReport",
    "MetricPoint",
    "SessionCache",
    "cache_report",
    "cache_sessions",
    "median",
    "parse_otlp_metrics",
    "record_metrics",
]

# Dropped on the way in.  A value that is never stored cannot leak from a
# query someone writes later.
IDENTITY_ATTRS: frozenset[str] = frozenset(
    {
        "user.email",
        "user.id",
        "user.account_id",
        "user.account_uuid",
        "organization.id",
    }
)

# Attributes promoted to their own column, because every useful question is
# grouped by one of them.  Anything else stays in the JSON blob.
_PROMOTED = frozenset({"type", "model", "effort", "query_source", "session.id"})

# Cache creation is billed at 1.25x base input and a read at 0.1x, so a prefix
# must be read about 12.5 times before its creation pays for itself.
# Amortisation below 1.0 is not "suboptimal" -- it is spending more than not
# caching at all would have cost.
CACHE_CREATE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1


@dataclass(frozen=True)
class MetricPoint:
    """One OTLP data point, stripped of identity."""

    metric: str
    value: float
    unit: str = ""
    session_id: str | None = None
    kind: str | None = None  # the `type` attribute: input / output / cacheRead / ...
    model: str | None = None
    effort: str | None = None
    query_source: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)


def _ensure_schema() -> None:
    """Create the table if this is the first write.

    Imported lazily: state_service pulls in a good deal of the orchestrator,
    and a module-level import here would make telemetry ingest depend on it.
    """
    from hivepilot.services import state_service

    state_service.init_db()


def _attr_value(raw: dict[str, Any]) -> str:
    """Flatten an OTLP AnyValue to a string.

    OTLP wraps every attribute value in a one-key object naming its type
    (``stringValue``, ``intValue``, ``boolValue``...).  We only group by these,
    so the string form is enough and avoids caring which arm was used.
    """
    value = raw.get("value")
    if not isinstance(value, dict) or not value:
        return ""
    return str(next(iter(value.values())))


def _point_value(point: dict[str, Any]) -> float | None:
    """Read a data point's numeric value, whichever arm carries it."""
    for key in ("asDouble", "asInt"):
        if key in point:
            try:
                return float(point[key])
            except (TypeError, ValueError):
                return None
    return None


def parse_otlp_metrics(payload: dict[str, Any]) -> list[MetricPoint]:
    """Turn an OTLP/JSON ExportMetricsServiceRequest into flat points.

    Deliberately tolerant: an unknown metric arm or a missing value skips that
    point rather than rejecting the batch.  An error response makes the
    *exporter* retry and log, which would turn schema drift in a dependency
    into noise on the very thing being measured.
    """
    points: list[MetricPoint] = []

    for resource in payload.get("resourceMetrics") or []:
        for scope in resource.get("scopeMetrics") or []:
            for metric in scope.get("metrics") or []:
                name = metric.get("name")
                if not name:
                    continue
                unit = metric.get("unit") or ""

                # sum / gauge / histogram all carry `dataPoints`; take whichever
                # arm is present rather than enumerating the ones known today.
                data_points: list[dict[str, Any]] = []
                for arm in ("sum", "gauge", "histogram", "exponentialHistogram"):
                    body = metric.get(arm)
                    if isinstance(body, dict):
                        data_points = body.get("dataPoints") or []
                        break

                for raw_point in data_points:
                    value = _point_value(raw_point)
                    if value is None:
                        continue

                    attrs = {
                        a["key"]: _attr_value(a)
                        for a in raw_point.get("attributes") or []
                        if a.get("key") and a["key"] not in IDENTITY_ATTRS
                    }

                    points.append(
                        MetricPoint(
                            metric=name,
                            value=value,
                            unit=unit,
                            session_id=attrs.get("session.id"),
                            kind=attrs.get("type"),
                            model=attrs.get("model"),
                            effort=attrs.get("effort"),
                            query_source=attrs.get("query_source"),
                            attributes={k: v for k, v in attrs.items() if k not in _PROMOTED},
                        )
                    )

    return points


def record_api_request_events(events: list) -> int:
    """Fan `claude_code.api_request` events out into the SAME rows a metric
    batch produces, and persist them.

    Deliberately not a new table. The event carries exactly what the
    `claude_code.cost.usage` / `token.usage` METRICS carry, just per request and
    with integer micros instead of a float -- so it lands in the machinery that
    already reads `agent_telemetry` rather than beside it.

    `metric` names mirror the OTel metric names so existing queries see both
    sources; `kind` reuses the metric vocabulary (`input`/`output`/`cacheRead`/
    `cacheCreation`). `query_source` carries `agent.name`, which is how a cost
    lands on a ROLE rather than in a total.

    A field the exporter did not send is SKIPPED, never written as zero: zero
    means "this request cost nothing", absent means "we were not told", and
    collapsing them is how a cost table quietly under-reports.
    """
    points: list[MetricPoint] = []
    for event in events:
        common = {
            "session_id": event.session_id,
            "model": event.model,
            # `agent.name`, which is empty for a headless top-level run. Left
            # as-is rather than back-filled with `query_source` (`main` /
            # `auxiliary`): writing a SUBSYSTEM into a column read as a role
            # would make every cost look attributed while none of it was.
            #
            # Attribution lives in `steps.role`, which the engine fills because
            # it knows what it dispatched.
            "query_source": event.agent_name,
        }
        if event.cost_usd_micros is not None:
            points.append(
                MetricPoint(
                    metric="claude_code.cost.usage",
                    # Stored in dollars so it is comparable with the metric
                    # source; the integer micros are what crossed the wire, so
                    # the division happens once, here, and never accumulates.
                    value=event.cost_usd_micros / 1_000_000,
                    unit="USD",
                    **common,
                )
            )
        for kind, value in (
            ("input", event.input_tokens),
            ("output", event.output_tokens),
            ("cacheRead", event.cache_read_tokens),
            ("cacheCreation", event.cache_creation_tokens),
        ):
            if value is not None:
                points.append(
                    MetricPoint(
                        metric="claude_code.token.usage",
                        value=float(value),
                        unit="tokens",
                        kind=kind,
                        **common,
                    )
                )
    return record_metrics(points)


def record_metrics(points: list[MetricPoint]) -> int:
    """Persist parsed points.  Returns how many rows were written."""
    if not points:
        return 0

    _ensure_schema()
    with db.connect() as conn:
        conn.executemany(
            """
            INSERT INTO agent_telemetry
                (metric, kind, model, effort, query_source, session_id, unit, value)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    p.metric,
                    p.kind,
                    p.model,
                    p.effort,
                    p.query_source,
                    p.session_id,
                    p.unit,
                    p.value,
                )
                for p in points
            ],
        )
    return len(points)


# ---------------------------------------------------------------------------
# Cache economics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionCache:
    """Cache creation vs reads for one agent session."""

    session_id: str
    model: str | None
    created: float
    read: float

    @property
    def amortisation(self) -> float:
        """Reads per unit created.  Below 1.0 the cache cost more than it saved."""
        if self.created <= 0:
            return 0.0
        return self.read / self.created

    @property
    def wasted_tokens(self) -> float:
        """Creation tokens that were never read back."""
        return max(0.0, self.created - self.read)


def cache_sessions(limit_days: int = 30) -> list[SessionCache]:
    """Per-session cache creation vs reads, newest first."""
    _ensure_schema()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT session_id,
                   model,
                   SUM(CASE WHEN kind = 'cacheCreation' THEN value ELSE 0 END) AS created,
                   SUM(CASE WHEN kind = 'cacheRead'     THEN value ELSE 0 END) AS read_
              FROM agent_telemetry
             WHERE metric = 'claude_code.token.usage'
               AND session_id IS NOT NULL
               AND recorded_at > datetime('now', ?)
             GROUP BY session_id, model
             HAVING created > 0
             ORDER BY MAX(recorded_at) DESC
            """,
            (f"-{int(limit_days)} days",),
        ).fetchall()

    return [
        SessionCache(session_id=r[0], model=r[1], created=float(r[2]), read=float(r[3]))
        for r in rows
    ]


@dataclass(frozen=True)
class CacheReport:
    """What the cache is actually doing, framed to expose the bad cases.

    Reported as a median and a count below 1.0, never as a total.  A fleet-wide
    ratio is dominated by whichever session read the most, so a handful of
    sessions burning creation tokens for nothing sit comfortably inside a
    healthy-looking aggregate -- which is how an 85% hit rate coexisted here
    with 1.7M tokens of wasted creation.
    """

    sessions: int
    median_amortisation: float
    below_one: int
    worst: SessionCache | None
    wasted_tokens: float

    @property
    def healthy(self) -> bool:
        return self.below_one == 0


def cache_report(limit_days: int = 30) -> CacheReport:
    """Median amortisation and the count of losing sessions."""
    sessions = cache_sessions(limit_days)
    if not sessions:
        return CacheReport(0, 0.0, 0, None, 0.0)

    ratios = [s.amortisation for s in sessions]
    losing = [s for s in sessions if s.amortisation < 1.0]

    return CacheReport(
        sessions=len(sessions),
        median_amortisation=median(ratios),
        below_one=len(losing),
        worst=min(sessions, key=lambda s: s.amortisation),
        wasted_tokens=sum(s.wasted_tokens for s in losing),
    )
