"""The precise half of the cost has never left the machine.

Measured on the box, 2026-08-18: `CLAUDE_CODE_ENABLE_TELEMETRY=1` and
`OTEL_METRICS_EXPORTER=otlp` are set, and `OTEL_LOGS_EXPORTER` is **absent**.
Metrics leave; events do not. And it is an EVENT --
`claude_code.api_request` -- that carries `cost_usd_micros`, the token
breakdown, the model, and `agent.name`.

Worse, adding the exporter alone would have made it silent rather than
working: HivePilot's OTLP receiver only serves `/otlp/v1/metrics`. Events would
have posted to `/otlp/v1/logs` and taken a 404 with nobody looking.

Why this matters beyond tidiness: cost comes from the JSON envelope on stdout
of `--print` mode today. The moment an agent runs inside a herdr pane that
stdout is gone. Until events carry cost, putting an agent in a pane means
making it invisible in the figures -- the exact hole the ORCA-1 PRD called
blocking.

`cost_usd_micros` is preferred over `cost_usd` deliberately: integer millionths
sum without float drift, and these are summed across thousands of requests.
"""

from __future__ import annotations

import pytest

from hivepilot.services.otlp_events import parse_otlp_events


def _event(**attrs) -> dict:
    """One OTLP/JSON log record, in the shape Claude Code actually emits."""
    return {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {
                        "logRecords": [
                            {"attributes": [{"key": k, "value": _val(v)} for k, v in attrs.items()]}
                        ]
                    }
                ]
            }
        ]
    }


def _val(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


class TestItReadsAnApiRequest:
    def test_the_cost_and_tokens_are_extracted(self):
        payload = _event(
            **{
                "event.name": "claude_code.api_request",
                "model": "claude-sonnet-5",
                "cost_usd_micros": 141_339,
                "input_tokens": 3950,
                "output_tokens": 708,
                "cache_read_tokens": 99244,
                "cache_creation_tokens": 16029,
                "session.id": "sess-1",
            }
        )

        events = parse_otlp_events(payload)

        assert len(events) == 1
        e = events[0]
        assert e.model == "claude-sonnet-5"
        assert e.cost_usd_micros == 141_339
        assert e.input_tokens == 3950
        assert e.output_tokens == 708
        assert e.cache_read_tokens == 99244
        assert e.cache_creation_tokens == 16029
        assert e.session_id == "sess-1"

    def test_the_agent_name_is_kept_for_attribution(self):
        """`agent.name` is how a cost lands on a ROLE rather than a total --
        and an aggregate hides the case that matters."""
        payload = _event(**{"event.name": "claude_code.api_request", "agent.name": "reviewer"})

        assert parse_otlp_events(payload)[0].agent_name == "reviewer"

    def test_micros_are_preferred_over_the_float(self):
        """Integer millionths sum without drift across thousands of requests;
        the float is a convenience field."""
        payload = _event(
            **{
                "event.name": "claude_code.api_request",
                "cost_usd_micros": 141_339,
                "cost_usd": 0.1413391,
            }
        )

        assert parse_otlp_events(payload)[0].cost_usd_micros == 141_339


class TestItIgnoresWhatItDoesNotUnderstand:
    def test_other_event_names_are_skipped(self):
        """A tool_result or user_prompt carries no cost. Keeping them would
        inflate the count of things we claim to have measured."""
        payload = _event(**{"event.name": "claude_code.tool_result", "tool_name": "Read"})

        assert parse_otlp_events(payload) == []

    def test_an_event_with_no_name_is_skipped(self):
        assert parse_otlp_events(_event(model="x")) == []

    @pytest.mark.parametrize("payload", [{}, {"resourceLogs": []}, {"resourceLogs": [{}]}, None])
    def test_a_malformed_batch_yields_nothing_rather_than_raising(self, payload):
        """The exporter retries and logs against the very process being
        measured, so an unreadable batch must be dropped, never raised."""
        assert parse_otlp_events(payload) == []

    def test_a_missing_cost_is_none_not_zero(self):
        """Zero is a measurement: it means a request that cost nothing. Absent
        means we were not told. Collapsing them is how a cost table quietly
        under-reports."""
        payload = _event(**{"event.name": "claude_code.api_request", "model": "m"})

        assert parse_otlp_events(payload)[0].cost_usd_micros is None

    def test_a_non_numeric_cost_is_rejected_not_coerced(self):
        payload = _event(**{"event.name": "claude_code.api_request", "cost_usd_micros": "lots"})

        assert parse_otlp_events(payload)[0].cost_usd_micros is None


class TestItHandlesRealBatchShapes:
    def test_several_records_across_several_scopes(self):
        one = _event(**{"event.name": "claude_code.api_request", "cost_usd_micros": 1})
        two = _event(**{"event.name": "claude_code.api_request", "cost_usd_micros": 2})
        merged = {"resourceLogs": one["resourceLogs"] + two["resourceLogs"]}

        assert [e.cost_usd_micros for e in parse_otlp_events(merged)] == [1, 2]
