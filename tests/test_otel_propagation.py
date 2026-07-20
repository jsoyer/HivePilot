"""
Tests for Phase 18's two additive OTel-propagation gaps:

- `hivepilot.observability.tracing.traceparent_env()` — builds a W3C
  `TRACEPARENT` env-var mapping from the currently-active recording span.
- `hivepilot.utils.env.merge_environments()` — injects that mapping as a
  low-priority layer so every runner subprocess picks it up for free.
- `hivepilot.utils.remote.build_invocation`/`ssh_wrap` — the remote (SSH)
  path already forwards whatever `env` dict it's given as inline shell
  assignments, so a `TRACEPARENT` present in the merged env flows through
  end-to-end with NO code change to `remote.py` itself.
- `hivepilot.utils.logging._bind_trace_context` — binds `trace_id`/`span_id`
  onto structlog event dicts for log <-> trace correlation.

Mirrors the style of `tests/test_tracing.py`: a real `TracerProvider` +
`InMemorySpanExporter` for the enabled path, and
`monkeypatch.setitem(sys.modules, "opentelemetry", None)` for the
OTel-absent path.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from hivepilot.observability import tracing
from hivepilot.utils import logging as hp_logging
from hivepilot.utils.env import merge_environments
from hivepilot.utils.remote import build_invocation

_TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


@pytest.fixture()
def in_memory_tracer():
    """A real OTel tracer bound to a fresh, isolated TracerProvider +
    InMemorySpanExporter — never touches the process-global tracer
    provider, so tests stay fully isolated from each other."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("hivepilot-test")
    return tracer, exporter


# ---------------------------------------------------------------------------
# traceparent_env()
# ---------------------------------------------------------------------------


class TestTraceparentEnv:
    def test_returns_valid_w3c_traceparent_when_span_active(self, in_memory_tracer) -> None:
        tracer, _exporter = in_memory_tracer
        with tracer.start_as_current_span("step.run"):
            result = tracing.traceparent_env()
        assert set(result) == {"TRACEPARENT"}
        assert _TRACEPARENT_RE.match(result["TRACEPARENT"])

    def test_returns_empty_dict_when_no_active_span(self) -> None:
        # No span active on this real (un-patched) global tracer — the
        # default OTel API tracer produces non-recording spans.
        assert tracing.traceparent_env() == {}

    def test_returns_empty_dict_when_otel_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "opentelemetry", None)
        monkeypatch.setitem(sys.modules, "opentelemetry.trace", None)
        monkeypatch.setitem(sys.modules, "opentelemetry.propagate", None)
        assert tracing.traceparent_env() == {}

    def test_never_raises_on_unexpected_failure(
        self, monkeypatch: pytest.MonkeyPatch, in_memory_tracer
    ) -> None:
        tracer, _exporter = in_memory_tracer

        def _boom(*_args, **_kwargs):
            raise RuntimeError("propagator exploded")

        monkeypatch.setattr("opentelemetry.propagate.inject", _boom)
        with tracer.start_as_current_span("step.run"):
            assert tracing.traceparent_env() == {}  # must not raise


# ---------------------------------------------------------------------------
# merge_environments() — the single choke point every runner funnels through
# ---------------------------------------------------------------------------


class TestMergeEnvironmentsTraceparent:
    def test_includes_traceparent_when_span_active(
        self, in_memory_tracer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tracer, _exporter = in_memory_tracer
        monkeypatch.delenv("TRACEPARENT", raising=False)
        with tracer.start_as_current_span("step.run"):
            merged = merge_environments({"FOO": "bar"})
        assert "TRACEPARENT" in merged
        assert _TRACEPARENT_RE.match(merged["TRACEPARENT"])
        assert merged["FOO"] == "bar"

    def test_explicit_layer_traceparent_wins_over_injected(
        self, in_memory_tracer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tracer, _exporter = in_memory_tracer
        monkeypatch.delenv("TRACEPARENT", raising=False)
        with tracer.start_as_current_span("step.run"):
            merged = merge_environments({"TRACEPARENT": "explicit-override"})
        assert merged["TRACEPARENT"] == "explicit-override"

    def test_byte_identical_when_tracing_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The critical invariant: with no active/recording span (tracing
        off, the default state for this test process), the merged env has
        NO `TRACEPARENT` key at all — exact dict equality with the
        pre-Phase-18-gap-closure behavior."""
        monkeypatch.delenv("TRACEPARENT", raising=False)
        import os

        expected = dict(os.environ)
        expected.update({"FOO": "bar"})
        merged = merge_environments({"FOO": "bar"})
        assert merged == expected
        assert "TRACEPARENT" not in merged

    def test_byte_identical_when_otel_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "opentelemetry", None)
        monkeypatch.setitem(sys.modules, "opentelemetry.trace", None)
        monkeypatch.setitem(sys.modules, "opentelemetry.propagate", None)
        monkeypatch.delenv("TRACEPARENT", raising=False)
        import os

        expected = dict(os.environ)
        expected.update({"FOO": "bar"})
        merged = merge_environments({"FOO": "bar"})
        assert merged == expected


# ---------------------------------------------------------------------------
# Remote (SSH) path — build_invocation/ssh_wrap already forward whatever env
# dict they're given as inline shell assignments; no code change to
# remote.py was needed, this proves TRACEPARENT flows through end-to-end.
# ---------------------------------------------------------------------------


class TestRemotePropagation:
    def test_ssh_invocation_includes_traceparent_when_span_active(
        self, in_memory_tracer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tracer, _exporter = in_memory_tracer
        monkeypatch.delenv("TRACEPARENT", raising=False)
        with tracer.start_as_current_span("step.run"):
            env = merge_environments({"FOO": "bar"})
            argv, cwd, run_env = build_invocation(
                ["claude", "--print"], Path("/repo"), env, host="user@hostB"
            )
        assert argv[0] == "ssh"
        assert cwd is None
        assert run_env is None
        remote_cmd = argv[-1]
        match = re.search(r"TRACEPARENT=(\S+)", remote_cmd)
        assert match is not None
        traceparent = match.group(1).strip("'")
        assert _TRACEPARENT_RE.match(traceparent)

    def test_ssh_invocation_has_no_traceparent_when_tracing_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRACEPARENT", raising=False)
        env = merge_environments({"FOO": "bar"})
        argv, _cwd, _run_env = build_invocation(
            ["claude", "--print"], Path("/repo"), env, host="user@hostB"
        )
        assert "TRACEPARENT" not in argv[-1]


# ---------------------------------------------------------------------------
# Log <-> trace correlation
# ---------------------------------------------------------------------------


class TestBindTraceContext:
    def test_binds_trace_id_and_span_id_when_recording(self, in_memory_tracer) -> None:
        tracer, _exporter = in_memory_tracer
        with tracer.start_as_current_span("step.run") as span:
            span_context = span.get_span_context()
            out = hp_logging._bind_trace_context(None, "info", {"event": "x"})
        assert out["trace_id"] == format(span_context.trace_id, "032x")
        assert out["span_id"] == format(span_context.span_id, "016x")
        assert re.fullmatch(r"[0-9a-f]{32}", out["trace_id"])
        assert re.fullmatch(r"[0-9a-f]{16}", out["span_id"])

    def test_unchanged_when_no_active_span(self) -> None:
        event = {"event": "x"}
        out = hp_logging._bind_trace_context(None, "info", dict(event))
        assert out == event

    def test_unchanged_when_otel_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "opentelemetry", None)
        monkeypatch.setitem(sys.modules, "opentelemetry.trace", None)
        event = {"event": "x"}
        out = hp_logging._bind_trace_context(None, "info", dict(event))
        assert out == event

    def test_never_raises_on_unexpected_failure(
        self, monkeypatch: pytest.MonkeyPatch, in_memory_tracer
    ) -> None:
        tracer, _exporter = in_memory_tracer

        def _boom(*_args, **_kwargs):
            raise RuntimeError("span context exploded")

        monkeypatch.setattr("opentelemetry.trace.get_current_span", _boom)
        event = {"event": "x"}
        out = hp_logging._bind_trace_context(None, "info", dict(event))  # must not raise
        assert out == event
