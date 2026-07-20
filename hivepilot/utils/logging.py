from __future__ import annotations

import logging

import structlog
from structlog.typing import EventDict, WrappedLogger

from hivepilot.config import settings

_configured = False


def _redact_secret_values(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor: redact any registered secret value from every
    field of the event before it is rendered, RECURSIVELY (a secret nested
    inside a list/dict/tuple kwarg is redacted too, not just top-level
    strings).

    Ensures resolved ``${secret:NAME}`` values (and direct-form secrets) never
    appear verbatim in log output. Imported lazily to avoid an import cycle at
    module-load time (config_provenance imports settings, which imports logging
    indirectly in some paths).
    """
    from hivepilot.services.config_provenance import redact_value

    for key, value in list(event_dict.items()):
        event_dict[key] = redact_value(value)
    return event_dict


def _bind_trace_context(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor: bind ``trace_id``/``span_id`` (formatted the same
    way OTel itself does — 32-hex / 16-hex) onto the event dict when an OTel
    span is currently active AND recording, so a log line can be pivoted to
    its trace in Jaeger/Grafana (roadmap item 4: trace-aware error
    correlation).

    Lazy, guarded import of `opentelemetry.trace` — never a hard dependency
    of the core install. No-op (returns `event_dict` unchanged) when OTel
    isn't installed, tracing is off, or there's no active/recording span —
    log output stays byte-identical to before this processor existed.
    Never raises.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return event_dict
    try:
        span = trace.get_current_span()
        if not span.is_recording():
            return event_dict
        span_context = span.get_span_context()
        if not span_context.is_valid:
            return event_dict
        event_dict["trace_id"] = format(span_context.trace_id, "032x")
        event_dict["span_id"] = format(span_context.span_id, "016x")
    except Exception:  # noqa: BLE001 — tracing must never break logging
        return event_dict
    return event_dict


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(settings.logs_dir / "hivepilot.log", encoding="utf-8"),
        ],
    )
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="ISO"),
            _redact_secret_values,
            _bind_trace_context,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    _configured = True


def get_logger(name: str):
    configure_logging()
    return structlog.get_logger(name)
