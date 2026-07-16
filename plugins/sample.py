from __future__ import annotations

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


def _sample_fetch():
    """Demo Mirador panel `fetch()` — one section of each closed kind, so
    Sprint 2 (TUI) / Sprint 3 (web) have something real to render against.
    """
    return {
        "sections": [
            {"kind": "stat", "label": "steps run", "value": "42", "status": "ok"},
            {
                "kind": "table",
                "columns": ["project", "status"],
                "rows": [["demo-project", "ok"], ["other-project", "warn"]],
            },
            {"kind": "text", "content": "Sample panel contributed by plugins/sample.py."},
        ]
    }


def register():
    def before_step(payload, **kwargs):
        logger.info("plugin.before_step", project=payload.project_name, step=payload.step.name)

    def after_step(payload, **kwargs):
        logger.info(
            "plugin.after_step",
            project=payload.project_name,
            step=payload.step.name,
            metadata=payload.metadata,
        )

    return {
        "before_step": before_step,
        "after_step": after_step,
        "panels": [
            {"name": "sample_stats", "title": "Sample Stats", "fetch": _sample_fetch},
        ],
    }
