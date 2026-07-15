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
    string field of the event before it is rendered.

    Ensures resolved ``${secret:NAME}`` values (and direct-form secrets) never
    appear verbatim in log output. Imported lazily to avoid an import cycle at
    module-load time (config_provenance imports settings, which imports logging
    indirectly in some paths).
    """
    from hivepilot.services.config_provenance import redact_text

    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            event_dict[key] = redact_text(value)
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
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    _configured = True


def get_logger(name: str):
    configure_logging()
    return structlog.get_logger(name)
