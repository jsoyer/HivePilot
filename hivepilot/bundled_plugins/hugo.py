"""hugo runner plugin — wraps the Hugo static-site-generator CLI
(`new`/`build`/`serve`) as a first-class `kind: "hugo"` runner.

Opt-in-by-default (`settings.hugo_enabled`, default True — same opt-OUT
pattern as `plugins/rtk.py`), PATH-gated at run time via `shutil.which`.

Operation resolution mirrors the IaC/Helm runners
(`hivepilot.runners.iac_runner`/`helm_runner`): `payload.step.command` wins,
falling back to `self.definition.command`, falling back to
`self.definition.options["operation"]`, defaulting to `"build"`. A single
`_resolve_operation` is the source of truth so nothing else can disagree
about which operation is about to run.

Non-destructive: `hugo build`/`new`/`serve` only ever touch local files
(rendered site output, new content scaffolding) or start a local dev
server — none of them mutate a remote/live system, so (unlike the
IaC/Helm/kubectl runners) this plugin deliberately does NOT implement
`is_destructive()`. Deployment of the generated site is out of scope here
and stays with whatever `GitActions`/CI step already handles it.

`HugoRunner` satisfies `hivepilot.runners.base.BaseRunner` structurally (same
`__init__(definition, settings)` / `run(payload)` shape) WITHOUT subclassing
the `Protocol` — see `plugins/rtk.py`'s docstring for why.

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
from hivepilot.utils.env import merge_environments
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


class HugoRunner:
    """Wraps the `hugo` CLI: `build` (default) / `new` / `serve`."""

    def __init__(self, definition: RunnerDefinition, settings: Settings) -> None:
        self.definition = definition
        self.settings = settings

    def _resolve_operation(self, payload: RunnerPayload) -> str:
        """Single source of truth for the operation string. Case- and
        whitespace-normalized so `"Serve"`/`"serve"` resolve identically."""
        operation = (
            payload.step.command
            or self.definition.command
            or self.definition.options.get("operation", "build")
        )
        return str(operation).strip().lower()

    def _build_command(self, operation: str, options: dict[str, Any]) -> list[str]:
        if operation == "build":
            cmd = ["hugo"]
            if options.get("minify") is not False:
                cmd.append("--minify")
            destination = options.get("destination")
            if destination:
                cmd += ["--destination", destination]
            base_url = options.get("base_url")
            if base_url:
                cmd += ["--baseURL", base_url]
            environment = options.get("environment")
            if environment:
                cmd += ["--environment", environment]
            return cmd

        if operation == "new":
            path = options.get("path")
            if not path:
                raise ValueError(
                    "hugo 'new' requires an options.path (content path, e.g. posts/my-post.md)"
                )
            cmd = ["hugo", "new", path]
            archetype = options.get("archetype")
            if archetype:
                cmd += ["--kind", archetype]
            return cmd

        if operation == "serve":
            # NOTE: `hugo serve` starts a long-running local dev server and
            # BLOCKS until terminated — intended for local/dev use, not
            # one-shot automation.
            cmd = ["hugo", "serve"]
            bind = options.get("bind")
            if bind:
                cmd += ["--bind", bind]
            port = options.get("port")
            if port is not None:
                cmd += ["--port", str(port)]
            return cmd

        raise ValueError(f"Unsupported hugo operation: {operation!r}. Supported: build, new, serve")

    def run(self, payload: RunnerPayload) -> None:
        operation = self._resolve_operation(payload)
        binary = shutil.which("hugo")
        if not binary:
            raise RuntimeError("hugo CLI not found on PATH. Install Hugo to use the 'hugo' runner.")

        argv = self._build_command(operation, self.definition.options)
        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)

        logger.info(
            "hugo_runner.start",
            project=payload.project_name,
            step=payload.step.name,
            operation=operation,
        )

        subprocess.run(
            argv,
            cwd=str(payload.project.path),
            env=env,
            check=True,
            text=True,
        )

        logger.info(
            "hugo_runner.end",
            project=payload.project_name,
            step=payload.step.name,
            operation=operation,
        )


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `hugo` is on PATH; `error` when it isn't — unlike
    `plugins/rtk.py`'s degraded/fallback story, this runner has no raw-command
    fallback: without the `hugo` binary the runner cannot execute at all."""
    if shutil.which("hugo"):
        return HealthStatus("ok", "hugo on PATH")
    return HealthStatus("error", "hugo not on PATH — install Hugo to use this runner")


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.hugo_enabled:
        return {}
    return {"runners": {"hugo": HugoRunner}, "health": {"hugo": health}}
