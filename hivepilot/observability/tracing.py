"""OpenTelemetry distributed tracing for pipeline/task/step execution.

Phase 18 — OPT-IN, zero-overhead when off, import-guarded so the core
install is unaffected when the `tracing` extra isn't installed:

- `init_tracing(settings)` wires up a real `TracerProvider` + OTLP exporter
  ONLY when `settings.enable_tracing` is True AND the OTel SDK is
  importable. Otherwise it does nothing — the global tracer stays
  OTel's own no-op default (or, if the API package itself isn't
  installed at all, `get_tracer()` below falls back to a local no-op
  tracer so callers never need to know the difference).
- `get_tracer()` always returns something with a `start_as_current_span`
  context manager, whether or not OpenTelemetry is installed/enabled, so
  instrumented code can unconditionally do:

      with get_tracer().start_as_current_span("name") as span:
          ...

- `record_exception_on_span(span, exc)` records an exception + sets ERROR
  status on `span` — safe to call on both real OTel spans and the local
  no-op span (both implement `record_exception`/`set_status`).
- `current_context()` / `use_context(ctx)` capture and re-attach the
  active OTel context across a thread-pool boundary (contextvars are NOT
  automatically inherited by `concurrent.futures.ThreadPoolExecutor`
  worker threads), so a `task.run` span opened in a worker thread still
  nests correctly under the `pipeline.run` span opened on the calling
  thread. No-op-safe: both return/accept `None` when OTel isn't
  installed.

NEVER put secret values in span attributes/events — only step/task/
pipeline identifiers, runner kind, and status/outcome.
"""

from __future__ import annotations

import contextlib
import traceback
from typing import Any, Iterator

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Guards `init_tracing` so it only wires up the SDK once per process, even
# though it's called from multiple "a run begins" entry points (API server
# startup, CLI pipeline entry, scheduler daemon start).
_initialized = False


class _NoOpSpan:
    """Local no-op span used only when `opentelemetry` isn't installed at
    all. Implements the subset of the real `Span` API instrumented code
    calls, so callers never need an import guard of their own."""

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: ANN401
        pass

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        pass

    def record_exception(self, exception: BaseException, **kwargs: Any) -> None:  # noqa: ANN401
        pass

    def set_status(self, status: Any, description: str | None = None) -> None:  # noqa: ANN401
        pass

    def is_recording(self) -> bool:
        return False

    def end(self, end_time: int | None = None) -> None:
        pass


class _NoOpTracer:
    """Local no-op tracer used only when `opentelemetry` isn't installed at
    all — mirrors the shape of `opentelemetry.trace.Tracer` enough for
    `start_as_current_span(...)` to be used unconditionally as a context
    manager."""

    @contextlib.contextmanager
    def start_as_current_span(self, name: str, *args: Any, **kwargs: Any) -> Iterator[_NoOpSpan]:  # noqa: ANN401
        yield _NoOpSpan()


_NOOP_TRACER = _NoOpTracer()


def init_tracing(settings: Any) -> None:  # noqa: ANN401 — avoid importing hivepilot.config here
    """Wire up a real OTel `TracerProvider` + OTLP exporter, once per
    process, when `settings.enable_tracing` is True and the OTel SDK is
    importable. No-op otherwise (tracing disabled, or the `tracing` extra
    isn't installed) — safe to call from multiple startup paths."""
    global _initialized
    if _initialized:
        return
    if not getattr(settings, "enable_tracing", False):
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        # `tracing` extra not installed — stay dormant. Do NOT set
        # `_initialized` here so a later call (after the extra is
        # installed, e.g. in a long-lived process that reloads config)
        # could still succeed; in practice this only ever runs once at
        # process startup.
        return

    try:
        service_name = getattr(settings, "otel_service_name", None) or "hivepilot"
        resource = Resource.create({SERVICE_NAME: service_name})
        provider = TracerProvider(resource=resource)

        endpoint = getattr(settings, "otel_exporter_otlp_endpoint", None)
        # Only pass `endpoint` when HivePilot's own setting is set — otherwise
        # let OTLPSpanExporter() read the STANDARD `OTEL_EXPORTER_OTLP_ENDPOINT`
        # env var natively (the OTel SDK's own config resolution), per the
        # deliverable's requirement to honor it.
        exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    except Exception as exc:  # noqa: BLE001 — a tracing failure must never break the run
        # A synchronous construction error (malformed endpoint, bad service
        # name, exporter transport failure, ...) must not crash the calling
        # entry point (CLI/API/scheduler startup). Log the exception TYPE
        # only — never the exception message/args, which could echo a
        # secret-bearing endpoint URL or config value — and leave the
        # global no-op provider in place (do NOT set `_initialized`, so a
        # later call could still succeed once the underlying issue is
        # fixed).
        logger.warning("tracing.init_failed", error=type(exc).__name__)
        return
    _initialized = True


def get_tracer() -> Any:  # noqa: ANN401
    """Return an OTel tracer for HivePilot's own spans. Import-guarded: if
    `opentelemetry` isn't installed at all, returns a local no-op tracer so
    `get_tracer().start_as_current_span(...)` is always safe to call,
    regardless of whether OTel is installed or tracing is enabled. When
    OTel IS installed but no real provider was configured (tracing
    disabled, or the `tracing` extra's SDK/exporter isn't installed), the
    real API's own default provider produces non-recording (no-op) spans —
    same effective zero-overhead outcome."""
    try:
        from opentelemetry import trace
    except ImportError:
        return _NOOP_TRACER
    return trace.get_tracer("hivepilot")


def record_exception_on_span(span: Any, exc: BaseException) -> None:  # noqa: ANN401
    """Record `exc` on `span` and mark it as errored. No-op-safe: works
    uniformly whether `span` is a real OTel span or the local `_NoOpSpan` —
    both implement `record_exception`/`set_status`. Never raises (a
    tracing failure must never break the run it's observing).

    SECURITY: the exception's message AND stacktrace are redacted via
    `config_provenance.redact_text` before ever being handed to OTel —
    `Span.record_exception`'s default `exception.message`/
    `exception.stacktrace` attributes are built from the RAW exception, and
    a step's exception message can legitimately echo a resolved secret
    (e.g. an agent's own error text). Passing pre-redacted `attributes`
    overrides both default keys (see `Span.record_exception`'s
    `_attributes.update(attributes)`), so no unredacted secret ever reaches
    an exported span. Callers MUST also pass `record_exception=False,
    set_status_on_exception=False` to `start_as_current_span(...)` — OTel's
    own context-manager exit would otherwise auto-record the RAW exception
    a second time when it propagates out of the `with` block.
    """
    message = str(exc)
    try:
        from hivepilot.services.config_provenance import redact_text

        message = redact_text(message)
        stacktrace = redact_text("".join(traceback.format_exception(exc)))
        span.record_exception(
            exc,
            attributes={
                "exception.message": message,
                "exception.stacktrace": stacktrace,
            },
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        from opentelemetry.trace import Status, StatusCode

        span.set_status(Status(StatusCode.ERROR, message))
    except ImportError:
        # OTel not installed — `span` is `_NoOpSpan`; nothing to set.
        pass
    except Exception:  # noqa: BLE001
        pass


def current_context() -> Any:  # noqa: ANN401
    """Capture the currently-active OTel context for propagation across a
    thread-pool boundary (`concurrent.futures.ThreadPoolExecutor` worker
    threads do NOT automatically inherit the submitting thread's
    contextvars). No-op-safe: returns `None` when OTel isn't installed."""
    try:
        from opentelemetry import context as otel_context
    except ImportError:
        return None
    return otel_context.get_current()


@contextlib.contextmanager
def use_context(ctx: Any) -> Iterator[None]:  # noqa: ANN401
    """Re-attach a context captured via `current_context()` for the
    duration of the `with` block — call this at the top of a
    `ThreadPoolExecutor` worker so spans started there become children of
    whatever span was active when `current_context()` was called on the
    submitting thread. No-op-safe: does nothing when `ctx` is `None` or
    OTel isn't installed."""
    if ctx is None:
        yield
        return
    try:
        from opentelemetry import context as otel_context
    except ImportError:
        yield
        return
    token = otel_context.attach(ctx)
    try:
        yield
    finally:
        otel_context.detach(token)
