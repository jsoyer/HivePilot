"""rtk runner plugin — wraps a shell-generic step's command with `rtk proxy`
to cut token usage on command output.

Modeled directly on `hivepilot.runners.shell_runner.ShellRunner` (the closest
built-in: a generic shell-command runner) — same template rendering, same
environment merge, same `bash -lc` invocation. The only difference is the
command is wrapped through `rtk proxy <cmd>` when the `rtk` binary is on
PATH.

Graceful degradation: if `rtk` is not installed, this runner logs a clear
warning and falls back to executing the raw command WITHOUT rtk — it never
crashes a run just because the token-saving proxy is unavailable.

`RtkRunner` satisfies `hivepilot.runners.base.BaseRunner` structurally (same
`__init__(definition, settings)` / `run(payload)` shape) WITHOUT subclassing
the `Protocol` — subclassing a `Protocol` class makes mypy treat it as
abstract, which is unnecessary friction for a plugin that isn't part of the
type-checked `hivepilot` package.

Deliberately NOT a `@dataclass`: local-file plugins are loaded via
`importlib.util.spec_from_file_location()` / `exec_module()`
(`hivepilot.plugins._scan_local_plugins`), which never registers the module
in `sys.modules`. Combined with `from __future__ import annotations`, that
trips a real CPython 3.14 `dataclasses` bug (`_is_type` does
`sys.modules[cls.__module__].__dict__`, which is `None` for an unregistered
module) — a plain class with an explicit `__init__` sidesteps it entirely
and keeps this plugin robust across Python versions and loading mechanisms.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.plugins import HealthStatus
from hivepilot.runners.base import RunnerPayload
from hivepilot.templates import render_template
from hivepilot.utils.env import merge_environments
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


class RtkRunner:
    """Shell-generic runner that proxies its command through `rtk proxy`."""

    def __init__(self, definition: RunnerDefinition, settings: Settings) -> None:
        self.definition = definition
        self.settings = settings

    def run(self, payload: RunnerPayload) -> None:
        command_str = self._render_command(payload)
        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        rtk_path = shutil.which("rtk")

        if rtk_path:
            argv = ["rtk", "proxy", "bash", "-lc", command_str]
            logger.info(
                "rtk_runner.start",
                project=payload.project_name,
                step=payload.step.name,
                wrapped=True,
                rtk_path=rtk_path,
            )
        else:
            argv = ["bash", "-lc", command_str]
            logger.info(
                "rtk_runner.rtk_not_found",
                project=payload.project_name,
                step=payload.step.name,
                detail="rtk not found on PATH — falling back to raw command execution",
            )

        subprocess.run(
            argv,
            cwd=str(payload.project.path),
            env=env,
            check=True,
            text=True,
        )
        logger.info("rtk_runner.end", project=payload.project_name, step=payload.step.name)

    def _render_command(self, payload: RunnerPayload) -> str:
        template = payload.step.command or self.definition.command
        if not template:
            raise ValueError(f"rtk runner '{self.definition.name}' missing command")

        context: dict[str, Any] = {
            "project_name": payload.project_name,
            "project_path": str(payload.project.path),
            "project_description": payload.project.description or "",
            "project_default_branch": payload.project.default_branch,
            "project_owner_repo": payload.project.owner_repo or "",
            "task_name": payload.task_name,
            "step_name": payload.step.name,
            "extra_prompt": payload.metadata.get("extra_prompt", ""),
        }
        return render_template(template, context)


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `rtk` is on PATH; `degraded` when it isn't — `RtkRunner.run`
    already falls back to raw (unwrapped) command execution in that case, so
    a missing `rtk` binary degrades token savings rather than breaking runs.
    """
    if shutil.which("rtk"):
        return HealthStatus("ok", "rtk on PATH")
    return HealthStatus("degraded", "rtk not on PATH — falls back to raw execution")


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.rtk_enabled:
        return {}
    return {"runners": {"rtk": RtkRunner}, "health": {"rtk": health}}
