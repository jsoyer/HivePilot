from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.runners.base import BaseRunner, RunnerPayload
from hivepilot.utils.env import gather_overrides
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Host paths that must never be bind-mounted into a container (escape / disclosure).
_BLOCKED_VOLUME_PREFIXES = ("/etc", "/root", "/proc", "/sys", "/dev", "/boot", "/var/run")

# Supported container runtimes — docker and podman share the same run CLI surface.
_SUPPORTED_RUNTIMES = ("docker", "podman")


def _validate_volume(volume: str) -> None:
    """Reject volume mounts exposing sensitive host paths or using traversal."""
    host = volume.split(":", 1)[0]
    if not host:
        raise ValueError(f"Invalid volume spec: {volume!r}")
    if ".." in host.split("/"):
        raise ValueError(f"Unsafe volume (path traversal): {volume!r}")
    resolved = os.path.realpath(host)
    for blocked in _BLOCKED_VOLUME_PREFIXES:
        if resolved == blocked or resolved.startswith(blocked + os.sep):
            raise ValueError(f"Blocked volume mount to sensitive host path: {volume!r}")


@dataclass
class ContainerRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings

    def run(self, payload: RunnerPayload) -> None:
        image = self.definition.options.get("image")
        command = self.definition.options.get("command")
        if not image or not command:
            raise ValueError("Container runner requires image and command options.")

        runtime = self.definition.options.get("runtime") or self.settings.container_runtime
        if runtime not in _SUPPORTED_RUNTIMES:
            raise ValueError(
                f"Unsupported container runtime {runtime!r}; expected one of {_SUPPORTED_RUNTIMES}."
            )

        volumes = self.definition.options.get("volumes", [])
        env_vars = gather_overrides(payload.project.env, self.definition.env, payload.secrets)

        run_command = [
            runtime,
            "run",
            "--rm",
            "-w",
            str(payload.project.path),
        ]
        for volume in volumes:
            _validate_volume(volume)
            run_command.extend(["-v", volume])
        for key, value in env_vars.items():
            run_command.extend(["-e", f"{key}={value}"])
        run_command.extend([image, "bash", "-lc", command])

        logger.info(
            "container_runner.start",
            runtime=runtime,
            image=image,
            command=command,
            project=payload.project_name,
        )
        subprocess.run(run_command, check=True)
