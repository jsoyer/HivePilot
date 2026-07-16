"""Kubernetes (kubectl) runner (Phase 28b).

Output is intentionally NOT captured or returned by this runner. Read
operations such as ``kubectl get secret -o yaml``/``kubectl describe`` can
base64-dump or echo secret material sourced from the cluster itself (e.g.
Kubernetes ``Secret`` objects), and the ``RunResult.detail`` path such output
would otherwise flow through (CLI stdout, the ``/v1/run`` API body,
Slack/Discord/Telegram notifications) has no way to redact cluster-sourced
values — the Phase 10c choke point (``redact_text``) only masks values
explicitly registered via ``${secret:}`` resolution. ``run()`` always
executes with ``capture_output=False`` so output streams live to the
parent's stdout instead of ever being captured, returned, or persisted.

Destructive operations (``apply``, ``delete``, and mutating ``rollout``
sub-commands: ``restart``/``undo``) are surfaced via ``is_destructive()``,
the optional structural contract queried by the orchestrator's step-level
approval gate (``hivepilot.orchestrator.step_requires_approval``), so they
auto-gate behind approval exactly like the Terraform/OpenTofu/Pulumi
runners' ``apply``/``destroy``.
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

_DEFAULT_KUBECTL_TIMEOUT = 300

# Rollout sub-commands that mutate a live workload; `status`/`history` are
# read-only and intentionally excluded.
_ROLLOUT_DESTRUCTIVE_SUBS = frozenset({"restart", "undo"})
_TOP_LEVEL_DESTRUCTIVE_OPS = frozenset({"apply", "delete"})


@dataclass
class KubectlRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings

    def run(self, payload: RunnerPayload) -> None:
        self._execute(payload)

    def is_destructive(self, payload: RunnerPayload) -> bool:
        """Optional structural contract (getattr-discovered, like `capture`):
        True when the operation this step/definition resolves to mutates
        the cluster (`apply`, `delete`, or a `rollout restart`/`rollout
        undo`). Resolved exactly the same way `_execute` resolves it, so the
        gate always agrees with what would actually run."""
        operation = self._resolve_operation(payload)
        if operation in _TOP_LEVEL_DESTRUCTIVE_OPS:
            return True
        if operation == "rollout":
            sub = self.definition.options.get("rollout", "status")
            return sub in _ROLLOUT_DESTRUCTIVE_SUBS
        return False

    def _resolve_operation(self, payload: RunnerPayload) -> str:
        return (
            payload.step.command
            or self.definition.command
            or self.definition.options.get("operation", "get")
        )

    def _execute(self, payload: RunnerPayload) -> None:
        operation = self._resolve_operation(payload)
        timeout = (
            payload.step.timeout_seconds
            or self.definition.timeout_seconds
            or _DEFAULT_KUBECTL_TIMEOUT
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

        if not shutil.which("kubectl"):
            raise RuntimeError(
                "kubectl CLI not found on PATH. Install it before running "
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
        cmd: list[str] = ["kubectl"]

        if operation == "apply":
            kustomize = opts.get("kustomize")
            if kustomize:
                cmd += ["apply", "-k", kustomize]
            else:
                cmd += ["apply", "-f", opts.get("manifest", "")]
        elif operation == "delete":
            manifest = opts.get("manifest")
            if manifest:
                cmd += ["delete", "-f", manifest]
            else:
                resource = opts.get("resource", "")
                name = opts.get("name")
                cmd += ["delete", resource] + ([name] if name else [])
        elif operation == "get":
            resource = opts.get("resource", "")
            name = opts.get("name")
            output = opts.get("output", "wide")
            cmd += ["get", resource] + ([name] if name else []) + ["-o", output]
        elif operation == "diff":
            cmd += ["diff", "-f", opts.get("manifest", "")]
        elif operation == "rollout":
            sub = opts.get("rollout", "status")
            resource = opts.get("resource", "")
            cmd += ["rollout", sub, resource]
        elif operation == "describe":
            resource = opts.get("resource", "")
            name = opts.get("name")
            cmd += ["describe", resource] + ([name] if name else [])
        else:
            raise ValueError(f"Unknown kubectl operation: {operation!r}")

        namespace = opts.get("namespace")
        if namespace:
            cmd += ["-n", namespace]
        context = opts.get("context")
        if context:
            cmd += ["--context", context]

        return cmd
