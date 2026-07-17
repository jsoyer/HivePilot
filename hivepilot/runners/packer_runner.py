"""Packer runner (Phase 17c Sprint 2).

Output is intentionally NOT captured or returned by this runner. `packer
build`/`packer inspect` output can echo variable values sourced from
`-var`/`-var-file` (including cloud credentials passed as build vars), and
the ``RunResult.detail`` path such output would otherwise flow through (CLI
stdout, the ``/v1/run`` API body, Slack/Discord/Telegram notifications) has
no way to redact template-sourced values — the Phase 10c choke point
(``redact_text``) only masks values explicitly registered via
``${secret:}`` resolution. ``run()`` always executes with
``capture_output=False`` so output streams live to the parent's stdout
instead of ever being captured, returned, or persisted.

Side-effecting operations are surfaced via ``is_destructive()``, the
optional structural contract queried by the orchestrator's step-level
approval gate (``hivepilot.orchestrator.step_requires_approval``), so they
auto-gate behind approval exactly like the helm/kubectl/ansible/kustomize
runners' mutating operations:

- ``build`` creates a real machine image and/or cloud resources (costly,
  side-effecting, and not easily reversible) -> destructive.
- ``fmt`` rewrites the template file(s) on disk -> destructive.
- ``validate``/``init``/``inspect`` are read-only and intentionally
  excluded.
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

_DEFAULT_PACKER_TIMEOUT = 3600

# Operations that have real, non-trivial side effects — gated behind the
# step-level approval flow (see hivepilot.orchestrator.step_requires_approval).
# `validate`/`init`/`inspect` are read-only and intentionally excluded.
_DESTRUCTIVE_OPS = frozenset({"build", "fmt"})

# Operations that accept `-var`/`-var-file`/`-only`/`-except` flags.
_VAR_OPS = frozenset({"validate", "build"})


@dataclass
class PackerRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings
    # cli-only: non-agent runner; a resolved mode:api fails closed at
    # orchestrator validation (BaseRunner.supported_modes).
    supported_modes = frozenset({"cli"})

    def run(self, payload: RunnerPayload) -> None:
        self._execute(payload)

    def is_destructive(self, payload: RunnerPayload) -> bool:
        """Optional structural contract (getattr-discovered, like `capture`):
        True when the operation this step/definition resolves to has real
        side effects (`build` creates images/cloud resources; `fmt` rewrites
        source files). Resolved exactly the same way `_execute` resolves
        it, so the gate always agrees with what would actually run."""
        operation = self._resolve_operation(payload)
        return operation in _DESTRUCTIVE_OPS

    def _resolve_operation(self, payload: RunnerPayload) -> str:
        """Single source of truth for the operation string, used by both
        `is_destructive` and `_execute`/`_build_command` so the approval
        gate can never disagree with what actually runs. Case- and
        whitespace-normalized so `"Build"`/`"build"` resolve identically."""
        operation = (
            payload.step.command
            or self.definition.command
            or self.definition.options.get("operation", "validate")
        )
        return str(operation).strip().lower()

    def _execute(self, payload: RunnerPayload) -> None:
        operation = self._resolve_operation(payload)
        timeout = (
            payload.step.timeout_seconds
            or self.definition.timeout_seconds
            or _DEFAULT_PACKER_TIMEOUT
        )
        opts = self.definition.options

        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)
        cwd = str(payload.project.path)

        if not shutil.which("packer"):
            raise RuntimeError(
                "packer CLI not found on PATH. Install it before running "
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
        template = opts.get("template")

        if operation == "fmt":
            if not template:
                raise ValueError("packer fmt requires options.template")
            return ["packer", "fmt", template]
        elif operation == "init":
            if not template:
                raise ValueError("packer init requires options.template")
            return ["packer", "init", template]
        elif operation == "inspect":
            if not template:
                raise ValueError("packer inspect requires options.template")
            return ["packer", "inspect", template]
        elif operation == "validate":
            if not template:
                raise ValueError("packer validate requires options.template")
            cmd = ["packer", "validate"]
        elif operation == "build":
            if not template:
                raise ValueError("packer build requires options.template")
            cmd = ["packer", "build"]
        else:
            raise ValueError(f"Unknown packer operation: {operation!r}")

        if operation in _VAR_OPS:
            cmd += self._var_flags(opts)
            if operation == "build" and opts.get("force"):
                cmd.append("-force")

        cmd.append(template)
        return cmd

    @staticmethod
    def _var_flags(opts: dict) -> list[str]:
        flags: list[str] = []
        var = opts.get("var")
        if var:
            if isinstance(var, dict):
                for key, value in var.items():
                    flags += ["-var", f"{key}={value}"]
            else:
                var_list = [var] if isinstance(var, str) else var
                for entry in var_list:
                    flags += ["-var", entry]
        var_file = opts.get("var_file")
        if var_file:
            var_file_list = [var_file] if isinstance(var_file, str) else var_file
            for value_file in var_file_list:
                flags += ["-var-file", value_file]
        only = opts.get("only")
        if only:
            flags.append(f"-only={only}")
        except_ = opts.get("except")
        if except_:
            flags.append(f"-except={except_}")
        return flags
