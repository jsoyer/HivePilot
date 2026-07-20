"""Atlantis IaC runner (Phase 17c — the last missing IaC runner kind).

This runner shells out to the **one-shot `atlantis` CLI subcommands**
(`atlantis plan` / `atlantis apply`) for CI/local use. It deliberately does
NOT spawn the long-running `atlantis server` daemon: a daemon process would
hang the blocking `subprocess.run` call this runner (like every other IaC
runner) relies on. Real Atlantis is normally PR/webhook-driven; operators who
want that flow run `atlantis server` outside HivePilot and use this runner
only for direct plan/apply invocations.

Plan/apply output is intentionally NOT captured or returned by this runner —
Atlantis wraps Terraform/OpenTofu under the hood, so its output can echo
secret var values (`TF_VAR_*` and similar), and the `RunResult.detail` path
it would otherwise flow through (CLI stdout, the `/v1/run` API body,
Slack/Discord/Telegram notifications) is not reliably redacted for
unregistered TF_VAR_*-style values: the Phase 10c choke point
(`redact_text`) only masks values that were explicitly registered via
`${secret:}` resolution, and TF_VAR_*-style values never go through that
registration path. `run()` always executes with `capture_output=False` so
output streams live to the parent's stdout instead — mirroring
`hivepilot.runners.iac_runner`.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.runners.base import BaseRunner, RunnerPayload
from hivepilot.utils.env import merge_environments
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_ATLANTIS_TIMEOUT = 1800

# `apply` mutates real infrastructure — gated behind the step-level approval
# flow (see hivepilot.orchestrator.step_requires_approval). `plan` is
# read-only and intentionally excluded.
_ATLANTIS_DESTRUCTIVE_OPS = frozenset({"apply"})


@dataclass
class AtlantisRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings
    # cli-only: IaC runners only ever shell out. A resolved mode:api fails
    # closed at orchestrator validation (BaseRunner.supported_modes).
    supported_modes = frozenset({"cli"})

    def run(self, payload: RunnerPayload) -> None:
        self._execute(payload)

    def is_destructive(self, payload: RunnerPayload) -> bool:
        """Optional structural contract (getattr-discovered, like `capture`):
        True when the operation this step/definition resolves to mutates
        real infrastructure (`apply`). Resolved exactly the same way
        `_execute` resolves it, so the gate always agrees with what would
        actually run."""
        operation = self._resolve_operation(payload)
        return operation in _ATLANTIS_DESTRUCTIVE_OPS

    def _resolve_operation(self, payload: RunnerPayload) -> str:
        """Single source of truth for the operation string, used by both
        `is_destructive` and `_execute`/`_build_command` so the approval
        gate can never disagree with what actually runs."""
        return (
            payload.step.command
            or self.definition.command
            or self.definition.options.get("operation", "plan")
        )

    def _execute(self, payload: RunnerPayload) -> None:
        operation = self._resolve_operation(payload)
        timeout = (
            payload.step.timeout_seconds
            or self.definition.timeout_seconds
            or _DEFAULT_ATLANTIS_TIMEOUT
        )
        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        cwd = str(payload.project.path)
        opts = self.definition.options

        if not shutil.which("atlantis"):
            raise RuntimeError(
                "atlantis CLI not found on PATH. Install it before running "
                f"the '{self.definition.kind}' runner."
            )

        logger.info(
            "runner.start",
            runner=self.definition.kind,
            project=payload.project_name,
            step=payload.step.name,
            operation=operation,
        )

        cmd = self._build_command(operation, opts)
        subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            check=True,
            text=True,
            timeout=timeout,
            capture_output=False,
        )

        logger.info(
            "runner.end",
            runner=self.definition.kind,
            project=payload.project_name,
            step=payload.step.name,
            operation=operation,
        )

    def _build_command(self, operation: str, opts: dict) -> list[str]:
        if operation == "plan":
            cmd = ["atlantis", "plan"]
        elif operation == "apply":
            cmd = ["atlantis", "apply"]
        else:
            raise ValueError(f"Unknown atlantis operation: {operation!r}")

        project = opts.get("project")
        if project:
            cmd += ["-p", project]
        directory = opts.get("dir")
        if directory:
            cmd += ["--dir", directory]
        extra_args = opts.get("args")
        if extra_args is not None and not isinstance(extra_args, list):
            raise ValueError("atlantis 'args' option must be a list of strings")
        if extra_args:
            cmd += list(extra_args)

        return cmd
