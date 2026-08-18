"""Read `claude_code.api_request` events out of an OTLP/JSON log batch.

Measured on the box, 2026-08-18: `CLAUDE_CODE_ENABLE_TELEMETRY=1` and
`OTEL_METRICS_EXPORTER=otlp` are set, and `OTEL_LOGS_EXPORTER` is **absent**.
Metrics leave the machine; events do not. And it is an EVENT --
`claude_code.api_request` -- that carries `cost_usd_micros`, the token
breakdown, the model and `agent.name`. The precise half of the cost had never
been exported.

Adding the exporter alone would have been worse than doing nothing: the OTLP
receiver only served `/otlp/v1/metrics`, so events would have posted to
`/otlp/v1/logs` and taken a 404 with nobody looking. Silence, not failure.

Why it matters beyond neatness: today cost comes from the JSON envelope on
stdout of `--print` mode. The moment an agent runs inside a herdr pane that
stdout is gone. Until events carry cost, putting an agent in a pane means
making it invisible in the figures -- the hole the ORCA-1 PRD called blocking.

This module parses; it records nothing. Same split as `parse_otlp_metrics`, and
it keeps the shape-handling testable without a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The only event carrying cost and tokens. Others (`tool_result`,
#: `user_prompt`, ...) are skipped rather than stored: keeping them would
#: inflate the count of things we claim to have measured.
_API_REQUEST = "claude_code.api_request"


@dataclass(frozen=True)
class ApiRequestEvent:
    """One agent API call, as the CLI reported it.

    Every field is optional because the exporter is a third party: a field we
    were not told about must read as *unknown*, never as zero. Zero is a
    measurement -- a request that cost nothing -- and collapsing the two is how
    a cost table quietly under-reports.
    """

    model: str | None = None
    #: Integer millionths of a dollar. Preferred over `cost_usd` because these
    #: are summed across thousands of requests and integers do not drift.
    cost_usd_micros: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    session_id: str | None = None
    #: Claude Code's own SUBAGENT type. Measured on a real run: empty for a
    #: headless top-level agent, because there is no subagent -- so it is NOT
    #: the field role attribution can rely on.
    agent_name: str | None = None
    #: The HivePilot role, stated by US via `OTEL_RESOURCE_ATTRIBUTES`. A cost
    #: we cannot attribute is an aggregate, and an aggregate hides the case
    #: that matters.
    role: str | None = None


def _attr_value(raw: Any) -> Any:
    """Unwrap one OTLP AnyValue. Unknown wrappers return None, not a guess."""
    if not isinstance(raw, dict):
        return None
    if "stringValue" in raw:
        return raw["stringValue"]
    if "intValue" in raw:
        # OTLP/JSON encodes 64-bit integers as STRINGS.
        try:
            return int(raw["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in raw:
        return raw["doubleValue"]
    if "boolValue" in raw:
        return raw["boolValue"]
    return None


def _as_int(value: Any) -> int | None:
    """An int, or None. A non-numeric value is REJECTED, never coerced --
    `int("lots")` raising here would be dropped by the caller's guard and the
    whole batch lost with it."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def parse_otlp_events(payload: Any) -> list[ApiRequestEvent]:
    """Extract every `claude_code.api_request` from an OTLP/JSON batch.

    Never raises. The exporter retries and logs against the very process being
    measured, so an unreadable batch is dropped rather than turned into noise
    on the agent.
    """
    if not isinstance(payload, dict):
        return []

    out: list[ApiRequestEvent] = []
    for resource in payload.get("resourceLogs") or []:
        if not isinstance(resource, dict):
            continue
        # Resource attributes are a DEFAULT for the whole batch: this is where
        # `OTEL_RESOURCE_ATTRIBUTES` lands, one level above the log record. Not
        # reading here would drop the very value the engine took the trouble to
        # inject.
        resource_attrs: dict[str, Any] = {}
        res = resource.get("resource")
        if isinstance(res, dict):
            for attr in res.get("attributes") or []:
                if isinstance(attr, dict) and isinstance(attr.get("key"), str):
                    resource_attrs[attr["key"]] = _attr_value(attr.get("value"))
        for scope in resource.get("scopeLogs") or []:
            if not isinstance(scope, dict):
                continue
            for record in scope.get("logRecords") or []:
                if not isinstance(record, dict):
                    continue
                # The record is the more specific statement, so it wins.
                attrs: dict[str, Any] = dict(resource_attrs)
                for attr in record.get("attributes") or []:
                    if isinstance(attr, dict) and isinstance(attr.get("key"), str):
                        attrs[attr["key"]] = _attr_value(attr.get("value"))

                if attrs.get("event.name") != _API_REQUEST:
                    continue

                model = attrs.get("model")
                session = attrs.get("session.id")
                agent = attrs.get("agent.name")
                role = attrs.get("hivepilot.role")
                out.append(
                    ApiRequestEvent(
                        model=model if isinstance(model, str) else None,
                        cost_usd_micros=_as_int(attrs.get("cost_usd_micros")),
                        input_tokens=_as_int(attrs.get("input_tokens")),
                        output_tokens=_as_int(attrs.get("output_tokens")),
                        cache_read_tokens=_as_int(attrs.get("cache_read_tokens")),
                        cache_creation_tokens=_as_int(attrs.get("cache_creation_tokens")),
                        session_id=session if isinstance(session, str) else None,
                        agent_name=agent if isinstance(agent, str) else None,
                        role=role if isinstance(role, str) else None,
                    )
                )
    return out
