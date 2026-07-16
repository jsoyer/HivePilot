"""Puppet runner (Phase 17b).

Output is intentionally NOT captured or returned by this runner. `puppet
apply`/`puppet agent --test` runs commonly echo node-sourced fact/report
data and manifest `notify`/`Puppet.debug` output, and the
``RunResult.detail`` path such output would otherwise flow through (CLI
stdout, the ``/v1/run`` API body, Slack/Discord/Telegram notifications) has
no way to redact node-sourced values — the Phase 10c choke point
(``redact_text``) only masks values explicitly registered via ``${secret:}``
resolution. ``run()`` always executes with ``capture_output=False`` so
output streams live to the parent's stdout instead of ever being captured,
returned, or persisted.

Destructive operations — a real ``apply`` (applies a manifest to the local
node) or ``agent`` (`puppet agent --test`, applies the catalog from the
Puppet master) run — are surfaced via ``is_destructive()``, the optional
structural contract queried by the orchestrator's step-level approval gate
(``hivepilot.orchestrator.step_requires_approval``), so they auto-gate
behind approval exactly like the Ansible/Salt/Chef runners' mutating
operations. ``noop`` (Puppet's built-in dry-run mode, `--noop`, layered on
top of either `apply` or `agent --test` via `options.agent`) never mutates
the node and is intentionally excluded.
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

_DEFAULT_PUPPET_TIMEOUT = 1800

_DESTRUCTIVE_OPS = frozenset({"apply", "agent"})
_KNOWN_OPS = frozenset({"apply", "agent", "noop"})


@dataclass
class PuppetRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings

    def run(self, payload: RunnerPayload) -> None:
        self._execute(payload)

    def is_destructive(self, payload: RunnerPayload) -> bool:
        """Optional structural contract (getattr-discovered, like `capture`):
        True when the operation this step/definition resolves to mutates
        the node (`apply`/`agent`). `noop` is always a dry-run — even with
        `options.agent` set — and is not destructive. Resolved exactly the
        same way `_execute` resolves it, so the gate always agrees with
        what would actually run."""
        operation = self._resolve_operation(payload)
        return operation in _DESTRUCTIVE_OPS

    def _resolve_operation(self, payload: RunnerPayload) -> str:
        """Single source of truth for the operation string, used by both
        `is_destructive` and `_execute`/`_build_command` so the approval
        gate can never disagree with what actually runs. Case- and
        whitespace-normalized so `"Apply"`/`"apply"` resolve identically."""
        operation = (
            payload.step.command
            or self.definition.command
            or self.definition.options.get("operation", "noop")
        )
        return str(operation).strip().lower()

    def _execute(self, payload: RunnerPayload) -> None:
        operation = self._resolve_operation(payload)
        timeout = (
            payload.step.timeout_seconds
            or self.definition.timeout_seconds
            or _DEFAULT_PUPPET_TIMEOUT
        )
        opts = self.definition.options

        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        cwd = str(payload.project.path)

        if not shutil.which("puppet"):
            raise RuntimeError(
                "puppet CLI not found on PATH. Install it before running "
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
        if operation not in _KNOWN_OPS:
            raise ValueError(f"Unknown puppet operation: {operation!r}")

        use_agent = operation == "agent" or (operation == "noop" and bool(opts.get("agent")))

        if use_agent:
            cmd = ["puppet", "agent", "--test"]
        else:
            manifest = opts.get("manifest")
            if not manifest:
                raise ValueError(f"puppet {operation} requires options.manifest")
            cmd = ["puppet", "apply", manifest]

        if operation == "noop":
            cmd.append("--noop")

        environment = opts.get("environment")
        if environment:
            cmd += ["--environment", environment]

        return cmd
