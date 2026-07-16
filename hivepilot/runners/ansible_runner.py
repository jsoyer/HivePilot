"""Ansible runner (Phase 17b).

Output is intentionally NOT captured or returned by this runner. Playbook
and ad-hoc runs commonly echo host facts, `debug` module output, or values
gathered from the target hosts themselves, and the ``RunResult.detail`` path
such output would otherwise flow through (CLI stdout, the ``/v1/run`` API
body, Slack/Discord/Telegram notifications) has no way to redact
host-sourced values — the Phase 10c choke point (``redact_text``) only
masks values explicitly registered via ``${secret:}`` resolution. ``run()``
always executes with ``capture_output=False`` so output streams live to the
parent's stdout instead of ever being captured, returned, or persisted.

Destructive operations — a real (non-check-mode) ``playbook`` run and
``adhoc`` (arbitrary module execution against live hosts) — are surfaced via
``is_destructive()``, the optional structural contract queried by the
orchestrator's step-level approval gate
(``hivepilot.orchestrator.step_requires_approval``), so they auto-gate
behind approval exactly like the Terraform/OpenTofu/Pulumi/kubectl runners'
mutating operations. A ``playbook`` run with ``options.check=True`` (or the
dedicated ``check`` operation) is a dry-run and is intentionally excluded.
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

_DEFAULT_ANSIBLE_TIMEOUT = 1800

# Closed set of operations this runner supports, mapped to the CLI binary
# each one invokes. Anything else fails closed via `_binary_for`, matching
# the top-level operation dispatch in `_build_command`.
_BINARY_FOR_OP = {
    "playbook": "ansible-playbook",
    "check": "ansible-playbook",
    "adhoc": "ansible",
    "lint": "ansible-lint",
    "galaxy-install": "ansible-galaxy",
}


@dataclass
class AnsibleRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings

    def run(self, payload: RunnerPayload) -> None:
        self._execute(payload)

    def is_destructive(self, payload: RunnerPayload) -> bool:
        """Optional structural contract (getattr-discovered, like `capture`):
        True when the operation this step/definition resolves to mutates
        live hosts (a real `playbook` run, or `adhoc`). A `playbook` run in
        check-mode (`check` operation, or `options.check=True`) is a dry-run
        and is not destructive. Resolved exactly the same way `_execute`
        resolves it, so the gate always agrees with what would actually
        run."""
        operation = self._resolve_operation(payload)
        if operation == "adhoc":
            return True
        if operation == "playbook":
            return not self._is_check_mode(operation, self.definition.options)
        return False

    def _resolve_operation(self, payload: RunnerPayload) -> str:
        """Single source of truth for the operation string, used by both
        `is_destructive` and `_execute`/`_build_command` so the approval
        gate can never disagree with what actually runs. Case- and
        whitespace-normalized so `"Check"`/`"check"` resolve identically."""
        operation = (
            payload.step.command
            or self.definition.command
            or self.definition.options.get("operation", "check")
        )
        return str(operation).strip().lower()

    @staticmethod
    def _is_check_mode(operation: str, opts: dict) -> bool:
        """Single source of truth for check (dry-run) mode, used by both
        `is_destructive` and `_build_command`: either the dedicated `check`
        operation, or a `playbook` run with `options.check` truthy."""
        return operation == "check" or (operation == "playbook" and bool(opts.get("check")))

    def _execute(self, payload: RunnerPayload) -> None:
        operation = self._resolve_operation(payload)
        timeout = (
            payload.step.timeout_seconds
            or self.definition.timeout_seconds
            or _DEFAULT_ANSIBLE_TIMEOUT
        )
        opts = self.definition.options

        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        cwd = str(payload.project.path)

        binary = _BINARY_FOR_OP.get(operation)
        if binary is None:
            raise ValueError(f"Unknown ansible operation: {operation!r}")

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
        if operation in ("playbook", "check"):
            inventory = opts.get("inventory")
            if not inventory:
                raise ValueError(f"ansible {operation} requires options.inventory")
            playbook = opts.get("playbook")
            if not playbook:
                raise ValueError(f"ansible {operation} requires options.playbook")
            cmd = ["ansible-playbook", "-i", inventory, playbook]
            extra_vars = opts.get("extra_vars")
            if extra_vars:
                cmd += ["--extra-vars", extra_vars]
            limit = opts.get("limit")
            if limit:
                cmd += ["--limit", limit]
            tags = opts.get("tags")
            if tags:
                cmd += ["--tags", tags]
            if self._is_check_mode(operation, opts):
                cmd += ["--check"]
            return cmd
        elif operation == "adhoc":
            pattern = opts.get("pattern")
            if not pattern:
                raise ValueError("ansible adhoc requires options.pattern")
            module = opts.get("module")
            if not module:
                raise ValueError("ansible adhoc requires options.module")
            cmd = ["ansible", pattern, "-m", module]
            args = opts.get("args")
            if args:
                cmd += ["-a", args]
            inventory = opts.get("inventory")
            if inventory:
                cmd += ["-i", inventory]
            limit = opts.get("limit")
            if limit:
                cmd += ["--limit", limit]
            return cmd
        elif operation == "lint":
            playbook = opts.get("playbook")
            if not playbook:
                raise ValueError("ansible lint requires options.playbook")
            return ["ansible-lint", playbook]
        elif operation == "galaxy-install":
            requirements = opts.get("requirements")
            if not requirements:
                raise ValueError("ansible galaxy-install requires options.requirements")
            return ["ansible-galaxy", "install", "-r", requirements]
        else:
            raise ValueError(f"Unknown ansible operation: {operation!r}")
