from __future__ import annotations

from dataclasses import dataclass

from hivepilot.models import PipelineConfig


@dataclass(slots=True)
class PipelineExecutionContext:
    pipeline: PipelineConfig
    project_names: list[str]


def describe_pipeline(pipeline: PipelineConfig) -> str:
    parts = [f"{idx + 1}. {stage.name} → {stage.task}" for idx, stage in enumerate(pipeline.stages)]
    return " | ".join(parts)
