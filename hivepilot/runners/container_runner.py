from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.runners.base import BaseRunner, RunnerPayload
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ContainerRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings

    def run(self, payload: RunnerPayload) -> None:
        image = self.definition.options.get("image")
        command = self.definition.options.get("command")
        if not image or not command:
            raise ValueError("Container runner requires image and command options.")

        volumes = self.definition.options.get("volumes", [])
        env_vars = payload.project.env.copy()
        env_vars.update(self.definition.env)

        docker_command = [
            "docker",
            "run",
            "--rm",
            "-w",
            str(payload.project.path),
        ]
        for volume in volumes:
            docker_command.extend(["-v", volume])
        for key, value in env_vars.items():
            docker_command.extend(["-e", f"{key}={value}"])
        docker_command.extend([image, "bash", "-lc", command])

        logger.info("container_runner.start", image=image, command=command, project=payload.project_name)
        subprocess.run(docker_command, check=True)
