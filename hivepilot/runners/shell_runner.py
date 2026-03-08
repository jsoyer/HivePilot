from __future__ import annotations

import subprocess
from dataclasses import dataclass

from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.runners.base import BaseRunner, RunnerPayload
from hivepilot.templates import render_template
from hivepilot.utils.env import merge_environments
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ShellRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings

    def run(self, payload: RunnerPayload) -> None:
        template = payload.step.command or self.definition.command
        if not template:
            raise ValueError(f"Shell runner '{self.definition.name}' missing command")

        context = {
            "project_name": payload.project_name,
            "project_path": str(payload.project.path),
            "project_description": payload.project.description or "",
            "project_default_branch": payload.project.default_branch,
            "project_owner_repo": payload.project.owner_repo or "",
            "task_name": payload.task_name,
            "step_name": payload.step.name,
            "extra_prompt": payload.metadata.get("extra_prompt", ""),
        }
        command_str = render_template(template, context)
        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)

        logger.info("shell_runner.start", project=payload.project_name, step=payload.step.name)
        subprocess.run(
            ["bash", "-lc", command_str],
            cwd=str(payload.project.path),
            env=env,
            check=True,
            text=True,
        )
        logger.info("shell_runner.end", project=payload.project_name, step=payload.step.name)
