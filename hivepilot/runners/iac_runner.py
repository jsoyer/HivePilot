"""Terraform / OpenTofu / Pulumi runners.

Plan/preview output is intentionally NOT captured or returned by these
runners — it can echo secret var values (``TF_VAR_*``, Pulumi stack config),
and the ``RunResult.detail`` path it would otherwise flow through (CLI
stdout, the ``/v1/run`` API body, Slack/Discord/Telegram notifications) is
not redacted. ``run()`` always executes with ``capture_output=False`` so
output streams live to the parent's stdout instead. A safe plan-SUMMARY
capture (counts only, no diff body) is deferred to the Mirador panel sprint
(A3).
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

_DEFAULT_TF_TIMEOUT = 1800
_DEFAULT_PULUMI_TIMEOUT = 1800


@dataclass
class _TfBaseRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings
    _binary: str = "tofu"

    def run(self, payload: RunnerPayload) -> None:
        self._execute(payload)

    def _execute(self, payload: RunnerPayload) -> None:
        operation = (
            payload.step.command
            or self.definition.command
            or self.definition.options.get("operation", "plan")
        )
        timeout = (
            payload.step.timeout_seconds or self.definition.timeout_seconds or _DEFAULT_TF_TIMEOUT
        )
        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        cwd = str(payload.project.path)
        opts = self.definition.options

        if not shutil.which(self._binary):
            raise RuntimeError(
                f"{self._binary} CLI not found on PATH. Install it before running "
                f"the '{self.definition.kind}' runner."
            )

        logger.info(
            "runner.start",
            runner=self.definition.kind,
            project=payload.project_name,
            step=payload.step.name,
            operation=operation,
        )

        workspace = opts.get("workspace")
        if workspace:
            subprocess.run(
                [self._binary, "workspace", "select", workspace],
                cwd=cwd,
                env=env,
                check=True,
                text=True,
                timeout=timeout,
            )

        if operation == "cost":
            self._run_cost_estimate(cwd=cwd, env=env, timeout=timeout)
            return

        cmd = self._build_command(operation, opts)
        try:
            subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                check=True,
                text=True,
                timeout=timeout,
                capture_output=False,
            )
        except subprocess.CalledProcessError as exc:
            if operation == "drift" and exc.returncode == 2:
                raise RuntimeError(
                    "Drift detected: infrastructure state diverges from configuration"
                ) from exc
            raise

        logger.info(
            "runner.end",
            runner=self.definition.kind,
            project=payload.project_name,
            step=payload.step.name,
            operation=operation,
        )

    def _run_cost_estimate(self, *, cwd: str, env: dict, timeout: int) -> None:
        """Run infracost breakdown. Requires infracost CLI to be installed."""
        if not shutil.which("infracost"):
            raise RuntimeError(
                "infracost CLI not found. Install from https://www.infracost.io/docs/"
            )
        result = subprocess.run(
            ["infracost", "breakdown", "--path", "."],
            cwd=cwd,
            env=env,
            check=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        # Deliberately NOT logged at info: infracost breakdown output can
        # reflect resource configuration derived from secret-backed TF vars.
        # This debug-level capture is internal to infracost only — it is
        # never returned or persisted (see module docstring).
        logger.debug("iac.cost_estimate", output=result.stdout.strip())

    def _build_command(self, operation: str, opts: dict) -> list[str]:
        cmd: list[str] = [self._binary]

        extra_flags: list[str] = []
        var_file = opts.get("var_file")
        if var_file:
            extra_flags.append(f"-var-file={var_file}")
        parallelism = opts.get("parallelism")
        if parallelism is not None:
            extra_flags.append(f"-parallelism={parallelism}")

        # -backend-config is init-only: passing it to plan/apply/destroy/drift
        # is a Terraform/OpenTofu usage error (non-zero exit, nothing runs).
        # It must NOT be added to `extra_flags` above.
        backend_config = opts.get("backend_config")

        if operation == "init":
            init_flags = [f"-backend-config={backend_config}"] if backend_config else []
            cmd += ["init"] + init_flags
        elif operation == "plan":
            cmd += ["plan", "-no-color"] + extra_flags
        elif operation == "apply":
            cmd += ["apply", "-auto-approve"] + extra_flags
        elif operation == "destroy":
            cmd += ["destroy", "-auto-approve"] + extra_flags
        elif operation == "output":
            cmd += ["output", "-json"]
        elif operation == "validate":
            cmd += ["validate"]
        elif operation == "drift":
            cmd += ["plan", "--detailed-exitcode", "-no-color"] + extra_flags
        elif operation == "cost":
            # infracost breakdown delegates to a separate tool — handled in
            # _execute() before this method is reached.
            cmd = ["infracost", "breakdown", "--path", "."]
        else:
            raise ValueError(f"Unknown IaC operation: {operation!r}")

        return cmd


@dataclass
class OpenTofuRunner(_TfBaseRunner):
    definition: RunnerDefinition
    settings: Settings

    def __post_init__(self) -> None:
        self._binary = "tofu"


@dataclass
class TerraformRunner(_TfBaseRunner):
    definition: RunnerDefinition
    settings: Settings

    def __post_init__(self) -> None:
        self._binary = "terraform"


@dataclass
class PulumiRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings

    def run(self, payload: RunnerPayload) -> None:
        self._execute(payload)

    def _execute(self, payload: RunnerPayload) -> None:
        operation = (
            payload.step.command
            or self.definition.command
            or self.definition.options.get("operation", "preview")
        )
        timeout = (
            payload.step.timeout_seconds
            or self.definition.timeout_seconds
            or _DEFAULT_PULUMI_TIMEOUT
        )
        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        cwd = str(payload.project.path)
        opts = self.definition.options

        if not shutil.which("pulumi"):
            raise RuntimeError(
                "pulumi CLI not found on PATH. Install it before running the 'pulumi' runner."
            )

        logger.info(
            "runner.start",
            runner="pulumi",
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
            runner="pulumi",
            project=payload.project_name,
            step=payload.step.name,
            operation=operation,
        )

    def _build_command(self, operation: str, opts: dict) -> list[str]:
        cmd: list[str] = ["pulumi"]

        stack = opts.get("stack")
        stack_flags: list[str] = ["--stack", stack] if stack else []

        config_flags: list[str] = []
        config = opts.get("config", {})
        for key, value in config.items():
            config_flags += ["--config", f"{key}={value}"]

        if operation == "preview":
            cmd += ["preview"] + stack_flags + config_flags
        elif operation == "up":
            cmd += ["up", "--yes"] + stack_flags + config_flags
        elif operation == "destroy":
            cmd += ["destroy", "--yes"] + stack_flags + config_flags
        elif operation == "output":
            cmd += ["stack", "output", "--json"] + stack_flags
        elif operation == "refresh":
            cmd += ["refresh", "--yes"] + stack_flags + config_flags
        else:
            raise ValueError(f"Unknown Pulumi operation: {operation!r}")

        return cmd
