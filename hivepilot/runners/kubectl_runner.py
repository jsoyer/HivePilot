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
sub-commands: ``restart``/``undo``/``pause``/``resume``) are surfaced via
``is_destructive()``, the optional structural contract queried by the
orchestrator's step-level approval gate
(``hivepilot.orchestrator.step_requires_approval``), so they auto-gate
behind approval exactly like the Terraform/OpenTofu/Pulumi runners'
``apply``/``destroy``.
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

# Rollout sub-commands that mutate a live workload (`pause`/`resume` flip
# `.spec.paused` on the Deployment); `status`/`history` are read-only and
# intentionally excluded.
_ROLLOUT_DESTRUCTIVE_SUBS = frozenset({"restart", "undo", "pause", "resume"})
_TOP_LEVEL_DESTRUCTIVE_OPS = frozenset({"apply", "delete"})

# Closed set of rollout sub-commands kubectl actually supports. Anything else
# fails closed, matching the top-level operation dispatch in `_build_command`.
_KNOWN_ROLLOUT_SUBS = frozenset({"status", "history", "pause", "resume", "restart", "undo"})


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
        undo`/`rollout pause`/`rollout resume`). Resolved exactly the same
        way `_execute` resolves it, so the gate always agrees with what
        would actually run."""
        operation = self._resolve_operation(payload)
        if operation in _TOP_LEVEL_DESTRUCTIVE_OPS:
            return True
        if operation == "rollout":
            sub = self._resolve_rollout_sub(self.definition.options)
            return sub in _ROLLOUT_DESTRUCTIVE_SUBS
        return False

    def _resolve_operation(self, payload: RunnerPayload) -> str:
        """Single source of truth for the top-level operation string, used
        by both `is_destructive` and `_execute`/`_build_command` so the
        approval gate can never disagree with what actually runs. Case- and
        whitespace-normalized so `"Delete"`/`"delete"` resolve identically."""
        operation = (
            payload.step.command
            or self.definition.command
            or self.definition.options.get("operation", "get")
        )
        return str(operation).strip().lower()

    @staticmethod
    def _resolve_rollout_sub(opts: dict) -> str:
        """Single source of truth for the `rollout` sub-command, used by
        both `is_destructive` and `_build_command` so the approval gate can
        never disagree with what actually runs. Case- and
        whitespace-normalized so `"Restart"`/`"restart"` resolve
        identically."""
        return str(opts.get("rollout", "status")).strip().lower()

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
                if not resource:
                    raise ValueError("kubectl delete requires options.resource")
                name = opts.get("name")
                cmd += ["delete", resource] + ([name] if name else [])
        elif operation == "get":
            resource = opts.get("resource", "")
            if not resource:
                raise ValueError("kubectl get requires options.resource")
            name = opts.get("name")
            output = opts.get("output", "wide")
            cmd += ["get", resource] + ([name] if name else []) + ["-o", output]
        elif operation == "diff":
            cmd += ["diff", "-f", opts.get("manifest", "")]
        elif operation == "rollout":
            sub = self._resolve_rollout_sub(opts)
            if sub not in _KNOWN_ROLLOUT_SUBS:
                raise ValueError(f"Unknown kubectl rollout subcommand: {sub!r}")
            resource = opts.get("resource", "")
            if not resource:
                raise ValueError("kubectl rollout requires options.resource")
            cmd += ["rollout", sub, resource]
        elif operation == "describe":
            resource = opts.get("resource", "")
            if not resource:
                raise ValueError("kubectl describe requires options.resource")
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
