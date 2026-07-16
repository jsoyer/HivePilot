"""Salt runner (Phase 17b).

Output is intentionally NOT captured or returned by this runner. State runs
and ``pillar.items``/``grains.items`` lookups commonly echo minion-sourced
values (pillar/grain data, `debug`-equivalent state output), and the
``RunResult.detail`` path such output would otherwise flow through (CLI
stdout, the ``/v1/run`` API body, Slack/Discord/Telegram notifications) has
no way to redact minion-sourced values — the Phase 10c choke point
(``redact_text``) only masks values explicitly registered via ``${secret:}``
resolution. ``run()`` always executes with ``capture_output=False`` so
output streams live to the parent's stdout instead of ever being captured,
returned, or persisted.

Destructive operations — a real (non-test-mode) ``apply`` or ``highstate``
run — are surfaced via ``is_destructive()``, the optional structural
contract queried by the orchestrator's step-level approval gate
(``hivepilot.orchestrator.step_requires_approval``), so they auto-gate
behind approval exactly like the Ansible/Terraform/kubectl runners' mutating
operations. The dedicated ``test`` operation (always run with ``test=True``)
and an ``apply``/``highstate`` run with ``options.test=True`` are dry-runs
and are intentionally excluded, as are the read-only ``pillar``/``grains``
lookups.
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

_DEFAULT_SALT_TIMEOUT = 1800

# Operations that resolve to a state run (state.apply / state.highstate) —
# the only ops that can mutate minions. `pillar`/`grains` are read-only
# lookups.
_STATE_RUN_OPS = frozenset({"apply", "highstate", "test"})


@dataclass
class SaltRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings

    def run(self, payload: RunnerPayload) -> None:
        self._execute(payload)

    def is_destructive(self, payload: RunnerPayload) -> bool:
        """Optional structural contract (getattr-discovered, like `capture`):
        True when the operation this step/definition resolves to mutates
        live minions (a real `apply` or `highstate` run). A `test` run, or
        an `apply`/`highstate` run with `options.test=True`, is a dry-run
        and is not destructive; `pillar`/`grains` are read-only. Resolved
        exactly the same way `_execute` resolves it, so the gate always
        agrees with what would actually run."""
        operation = self._resolve_operation(payload)
        if operation in ("apply", "highstate"):
            return not self._is_test_mode(operation, self.definition.options)
        return False

    def _resolve_operation(self, payload: RunnerPayload) -> str:
        """Single source of truth for the operation string, used by both
        `is_destructive` and `_execute`/`_build_command` so the approval
        gate can never disagree with what actually runs. Case- and
        whitespace-normalized so `"Apply"`/`"apply"` resolve identically."""
        operation = (
            payload.step.command
            or self.definition.command
            or self.definition.options.get("operation", "test")
        )
        return str(operation).strip().lower()

    @staticmethod
    def _is_test_mode(operation: str, opts: dict) -> bool:
        """Single source of truth for test (dry-run) mode, used by both
        `is_destructive` and `_build_command`: either the dedicated `test`
        operation, or an `apply`/`highstate` run with `options.test`
        truthy."""
        return operation == "test" or bool(opts.get("test"))

    def _execute(self, payload: RunnerPayload) -> None:
        operation = self._resolve_operation(payload)
        timeout = (
            payload.step.timeout_seconds or self.definition.timeout_seconds or _DEFAULT_SALT_TIMEOUT
        )
        opts = self.definition.options

        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        cwd = str(payload.project.path)

        local = bool(opts.get("local"))
        binary = "salt-call" if local else "salt"

        if not shutil.which(binary):
            raise RuntimeError(
                f"{binary} CLI not found on PATH. Install it before running "
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
        local = bool(opts.get("local"))

        if operation in _STATE_RUN_OPS:
            if operation == "highstate":
                func = "state.highstate"
            elif operation == "apply":
                func = "state.apply"
            else:
                # "test": mirror apply when a state is given, else highstate.
                func = "state.apply" if opts.get("state") else "state.highstate"

            cmd = self._base_command(func, local, opts)
            if func == "state.apply":
                state = opts.get("state")
                if not state:
                    raise ValueError(f"salt {operation} requires options.state")
                cmd.append(state)
            if self._is_test_mode(operation, opts):
                cmd.append("test=True")
            return cmd
        elif operation == "pillar":
            return self._base_command("pillar.items", local, opts)
        elif operation == "grains":
            return self._base_command("grains.items", local, opts)
        else:
            raise ValueError(f"Unknown salt operation: {operation!r}")

    @staticmethod
    def _base_command(func: str, local: bool, opts: dict) -> list[str]:
        if local:
            return ["salt-call", "--local", func]
        target = opts.get("target", "*")
        return ["salt", target, func]
