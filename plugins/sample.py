from __future__ import annotations

from typing import Any, Dict

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


def register():
    def before_step(payload, **kwargs):
        logger.info("plugin.before_step", project=payload.project_name, step=payload.step.name)

    def after_step(payload, **kwargs):
        logger.info("plugin.after_step", project=payload.project_name, step=payload.step.name, metadata=payload.metadata)

    return {
        "before_step": before_step,
        "after_step": after_step,
    }
