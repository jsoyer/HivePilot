"""Chef runner (Phase 17b).

Output is intentionally NOT captured or returned by this runner. `chef-client`
runs commonly echo node-sourced attribute/ohai data and recipe `log`/`Chef::Log`
output, and the ``RunResult.detail`` path such output would otherwise flow
through (CLI stdout, the ``/v1/run`` API body, Slack/Discord/Telegram
notifications) has no way to redact node-sourced values — the Phase 10c
choke point (``redact_text``) only masks values explicitly registered via
``${secret:}`` resolution. ``run()`` always executes with
``capture_output=False`` so output streams live to the parent's stdout
instead of ever being captured, returned, or persisted.

The destructive operation — a real ``converge`` run (applies the run-list
to the node) — is surfaced via ``is_destructive()``, the optional structural
contract queried by the orchestrator's step-level approval gate
(``hivepilot.orchestrator.step_requires_approval``), so it auto-gates behind
approval exactly like the Ansible/Salt/Puppet runners' mutating operations.
``why-run`` (Chef's built-in dry-run mode, ``chef-client --why-run``) never
converges the node and is intentionally excluded.
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

_DEFAULT_CHEF_TIMEOUT = 1800

_KNOWN_OPS = frozenset({"converge", "why-run"})


@dataclass
class ChefRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings

    def run(self, payload: RunnerPayload) -> None:
        self._execute(payload)

    def is_destructive(self, payload: RunnerPayload) -> bool:
        """Optional structural contract (getattr-discovered, like `capture`):
        True when the operation this step/definition resolves to converges
        the node (`converge`). `why-run` is a dry-run and is not
        destructive. Resolved exactly the same way `_execute` resolves it,
        so the gate always agrees with what would actually run."""
        operation = self._resolve_operation(payload)
        return operation == "converge"

    def _resolve_operation(self, payload: RunnerPayload) -> str:
        """Single source of truth for the operation string, used by both
        `is_destructive` and `_execute`/`_build_command` so the approval
        gate can never disagree with what actually runs. Case- and
        whitespace-normalized so `"Converge"`/`"converge"` resolve
        identically."""
        operation = (
            payload.step.command
            or self.definition.command
            or self.definition.options.get("operation", "why-run")
        )
        return str(operation).strip().lower()

    def _execute(self, payload: RunnerPayload) -> None:
        operation = self._resolve_operation(payload)
        timeout = (
            payload.step.timeout_seconds or self.definition.timeout_seconds or _DEFAULT_CHEF_TIMEOUT
        )
        opts = self.definition.options

        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        cwd = str(payload.project.path)

        if not shutil.which("chef-client"):
            raise RuntimeError(
                "chef-client CLI not found on PATH. Install it before "
                f"running the '{self.definition.kind}' runner."
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
        if operation not in _KNOWN_OPS:
            raise ValueError(f"Unknown chef operation: {operation!r}")

        cmd = ["chef-client"]
        if operation == "why-run":
            cmd.append("--why-run")

        config = opts.get("config")
        if config:
            cmd += ["-c", config]
        runlist = opts.get("runlist")
        if runlist:
            cmd += ["--override-runlist", runlist]
        if opts.get("local_mode"):
            cmd.append("-z")

        return cmd
