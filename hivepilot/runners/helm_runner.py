"""Helm runner (Phase 17c).

Output is intentionally NOT captured or returned by this runner. Commands
such as ``helm template``/``helm get values`` can render or dump chart
values (including secret-derived values baked into a release), and the
``RunResult.detail`` path such output would otherwise flow through (CLI
stdout, the ``/v1/run`` API body, Slack/Discord/Telegram notifications) has
no way to redact chart-sourced values — the Phase 10c choke point
(``redact_text``) only masks values explicitly registered via ``${secret:}``
resolution. ``run()`` always executes with ``capture_output=False`` so
output streams live to the parent's stdout instead of ever being captured,
returned, or persisted.

Destructive operations (``install``, ``upgrade``, ``rollback``,
``uninstall`` — all of which mutate the cluster) are surfaced via
``is_destructive()``, the optional structural contract queried by the
orchestrator's step-level approval gate
(``hivepilot.orchestrator.step_requires_approval``), so they auto-gate
behind approval exactly like the kubectl/Terraform/OpenTofu/Pulumi runners'
mutating operations. ``template``/``lint``/``list``/``status`` are
read-only and intentionally excluded.
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

_DEFAULT_HELM_TIMEOUT = 600

# Operations that mutate a live cluster/release — gated behind the
# step-level approval flow (see hivepilot.orchestrator.step_requires_approval).
# `template`/`lint`/`list`/`status` are read-only and intentionally excluded.
_DESTRUCTIVE_OPS = frozenset({"install", "upgrade", "rollback", "uninstall"})

# Operations that accept chart-level `-f`/`--version` flags.
_CHART_VALUE_OPS = frozenset({"install", "upgrade", "template"})


@dataclass
class HelmRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings

    def run(self, payload: RunnerPayload) -> None:
        self._execute(payload)

    def is_destructive(self, payload: RunnerPayload) -> bool:
        """Optional structural contract (getattr-discovered, like `capture`):
        True when the operation this step/definition resolves to mutates the
        cluster (`install`/`upgrade`/`rollback`/`uninstall`). Resolved
        exactly the same way `_execute` resolves it, so the gate always
        agrees with what would actually run."""
        operation = self._resolve_operation(payload)
        return operation in _DESTRUCTIVE_OPS

    def _resolve_operation(self, payload: RunnerPayload) -> str:
        """Single source of truth for the operation string, used by both
        `is_destructive` and `_execute`/`_build_command` so the approval
        gate can never disagree with what actually runs. Case- and
        whitespace-normalized so `"Uninstall"`/`"uninstall"` resolve
        identically."""
        operation = (
            payload.step.command
            or self.definition.command
            or self.definition.options.get("operation", "list")
        )
        return str(operation).strip().lower()

    def _execute(self, payload: RunnerPayload) -> None:
        operation = self._resolve_operation(payload)
        timeout = (
            payload.step.timeout_seconds or self.definition.timeout_seconds or _DEFAULT_HELM_TIMEOUT
        )
        opts = self.definition.options

        env_layers: list[dict[str, str]] = []
        kubeconfig = opts.get("kubeconfig")
        if kubeconfig:
            env_layers.append({"KUBECONFIG": kubeconfig})
        env = merge_environments(
            payload.project.env, self.definition.env, *env_layers, payload.secrets
        )
        cwd = str(payload.project.path)

        if not shutil.which("helm"):
            raise RuntimeError(
                "helm CLI not found on PATH. Install it before running "
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
        if operation == "install":
            release, chart = self._require_release_and_chart(operation, opts)
            cmd = ["helm", "install", release, chart]
        elif operation == "upgrade":
            release, chart = self._require_release_and_chart(operation, opts)
            cmd = ["helm", "upgrade", release, chart]
        elif operation == "rollback":
            release = self._require_release(operation, opts)
            revision = opts.get("revision")
            cmd = ["helm", "rollback", release] + ([str(revision)] if revision else [])
        elif operation == "uninstall":
            release = self._require_release(operation, opts)
            cmd = ["helm", "uninstall", release]
        elif operation == "template":
            release, chart = self._require_release_and_chart(operation, opts)
            cmd = ["helm", "template", release, chart]
        elif operation == "lint":
            chart = opts.get("chart")
            if not chart:
                raise ValueError("helm lint requires options.chart")
            cmd = ["helm", "lint", chart]
        elif operation == "list":
            cmd = ["helm", "list"]
        elif operation == "status":
            release = self._require_release(operation, opts)
            cmd = ["helm", "status", release]
        else:
            raise ValueError(f"Unknown helm operation: {operation!r}")

        if operation in _CHART_VALUE_OPS:
            values = opts.get("values")
            if values:
                values_list = [values] if isinstance(values, str) else values
                for value_file in values_list:
                    cmd += ["-f", value_file]
            version = opts.get("version")
            if version:
                cmd += ["--version", version]

        namespace = opts.get("namespace")
        if namespace:
            cmd += ["-n", namespace]
        context = opts.get("context")
        if context:
            cmd += ["--kube-context", context]

        return cmd

    @staticmethod
    def _require_release(operation: str, opts: dict) -> str:
        release = opts.get("release")
        if not release:
            raise ValueError(f"helm {operation} requires options.release")
        return release

    @staticmethod
    def _require_release_and_chart(operation: str, opts: dict) -> tuple[str, str]:
        release = opts.get("release")
        if not release:
            raise ValueError(f"helm {operation} requires options.release")
        chart = opts.get("chart")
        if not chart:
            raise ValueError(f"helm {operation} requires options.chart")
        return release, chart
