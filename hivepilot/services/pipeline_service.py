from __future__ import annotations

from hivepilot.models import PipelineConfig, TasksFile


def validate_pipeline(pipeline: PipelineConfig, tasks: TasksFile) -> None:
    for stage in pipeline.stages:
        if stage.task not in tasks.tasks:
            raise ValueError(f"Pipeline stage '{stage.name}' references missing task '{stage.task}'")
