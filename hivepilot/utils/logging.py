from __future__ import annotations

import logging
from pathlib import Path

import structlog

from hivepilot.config import settings


_configured = False


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
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    _configured = True


def get_logger(name: str):
    configure_logging()
    return structlog.get_logger(name)
