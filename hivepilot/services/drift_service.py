"""Read-only IaC drift detection (Phase 20 Sprint D1).

Runs the same drift operation `hivepilot.runners.iac_runner` runs (`tofu`/
`terraform plan --detailed-exitcode -no-color`), but with
`capture_output=True` so the plan summary counts can be read, and returns a
structured `DriftResult` — never the raw plan stdout/stderr.

This is a **service**, not a runner: it is invoked directly (e.g. by a future
`hivepilot drift check` CLI command or the Mirador panel), never through
`Orchestrator`/`RunResult`.

Anti-leak guarantee
--------------------
The captured plan stdout can echo resolved `${secret:}` values (TF_VAR_*
echoes in resource diffs) — this is exactly why `iac_runner.run()` always
executes with `capture_output=False` (see that module's docstring). Here we
DO capture output (there's no other way to read a plan summary), so the
captured stdout is treated as a local, short-lived buffer: `detect_drift`
extracts ONLY the "Plan: N to add, N to change, N to destroy" counts line via
a strict regex, and the raw buffer is discarded — it is never stored on the
returned `DriftResult`, never logged, and never included in a raised
exception message. A non-zero/non-two exit code raises `RuntimeError` with
the tool name and exit code ONLY (see `_run_iac_operation`'s `RunResult`
discipline for the equivalent non-drift-op case).

To guarantee the drift argv can never diverge from `iac_runner`'s, this
module builds a `RunnerPayload` and calls the resolved Terraform/OpenTofu
runner's own private `_build_command`/`_binary` rather than re-implementing
argv assembly.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import cast

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.registry import resolve_runner_class
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.iac_runner import PulumiRunner, _TfBaseRunner
from hivepilot.utils.env import merge_environments
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_DRIFT_TIMEOUT = 300

# Matches Terraform/OpenTofu's plan summary line, e.g.:
#   "Plan: 1 to add, 2 to change, 3 to destroy."
# Deliberately the ONLY thing ever extracted from captured plan stdout — see
# module docstring's anti-leak guarantee.
_DRIFT_SUMMARY_RE = re.compile(r"Plan:\s+(\d+)\s+to add,\s+(\d+)\s+to change,\s+(\d+)\s+to destroy")


@dataclass(frozen=True)
class DriftSummary:
    """Parsed Terraform/OpenTofu plan summary counts. Nothing else from the
    plan is ever extracted or retained."""

    to_add: int
    to_change: int
    to_destroy: int


@dataclass(frozen=True)
class DriftResult:
    """Structured, secret-safe drift-check outcome.

    `summary` is populated whenever drift is detected (`drifted=True`) AND
    the plan summary line was successfully parsed; it is `None` when drift
    was detected but the summary line could not be parsed (NEVER a raw
    stdout fallback). `error` is reserved for future non-raising callers
    (mirrors `scan_service.ScanResult.error`) — the public `detect_drift`
    entry point in this module always raises `RuntimeError`/`ValueError`
    instead of populating it.
    """

    project: str
    runner: str
    drifted: bool
    summary: DriftSummary | None = None
    error: str | None = None


def _require_tool(tool: str, *, purpose: str) -> None:
    if not shutil.which(tool):
        raise RuntimeError(f"{tool} not found on PATH. Install it before {purpose}.")


def detect_drift(
    project: ProjectConfig,
    *,
    runner_kind: str = "opentofu",
    timeout: int = _DEFAULT_DRIFT_TIMEOUT,
    secrets: dict[str, str] | None = None,
) -> DriftResult:
    """Run an IaC drift check against *project* and return a structured
    `DriftResult`.

    `runner_kind` selects the IaC runner: `"opentofu"` (default) or
    `"terraform"`. `secrets` mirrors `RunnerPayload.secrets` (already-resolved
    `${secret:NAME}` values, e.g. `TF_VAR_*`/cloud credentials) and is merged
    into the child process environment exactly the way `iac_runner` merges
    it.

    Raises `ValueError` if `runner_kind` is `"pulumi"` (Pulumi has no drift
    operation) or an unknown kind, `RuntimeError` if the runner's CLI binary
    isn't on `PATH`, the drift check times out, or the tool exits with an
    unexpected code (never `0`/no-drift or `2`/drift-detected).
    """
    runner_cls = resolve_runner_class(runner_kind)
    if runner_cls is PulumiRunner:
        raise ValueError(f"drift is not supported for the {runner_kind!r} runner")

    project_name = project.path.name

    definition = RunnerDefinition(name=runner_kind, kind=runner_kind)
    step = TaskStep(name="drift", runner=runner_kind, command="drift")
    payload = RunnerPayload(
        project_name=project_name,
        project=project,
        task_name="drift",
        step=step,
        metadata={},
        secrets=secrets or {},
    )

    runner = cast(_TfBaseRunner, runner_cls(definition=definition, settings=settings))
    binary = runner._binary
    _require_tool(binary, purpose="detecting infrastructure drift")

    # Mirrors _TfBaseRunner._execute's operation resolution exactly, so this
    # stays in lockstep even if a future caller starts passing a definition
    # with its own `command`/`options["operation"]` override.
    operation = (
        payload.step.command or definition.command or definition.options.get("operation", "plan")
    )
    env = merge_environments(payload.project.env, definition.env, payload.secrets)
    cwd = str(payload.project.path)
    argv = runner._build_command(operation, definition.options)

    logger.info("drift.start", runner=runner_kind, project=project_name)

    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{binary} drift check timed out after {timeout}s") from exc

    if proc.returncode == 0:
        logger.info("drift.end", runner=runner_kind, project=project_name, drifted=False)
        return DriftResult(
            project=project_name,
            runner=runner_kind,
            drifted=False,
            summary=DriftSummary(to_add=0, to_change=0, to_destroy=0),
        )

    if proc.returncode == 2:
        # proc.stdout is a local buffer only: extract ONLY the counts line
        # below, then let the raw text go out of scope untouched by any
        # returned/logged/raised value — see module docstring.
        match = _DRIFT_SUMMARY_RE.search(proc.stdout)
        summary = (
            DriftSummary(
                to_add=int(match.group(1)),
                to_change=int(match.group(2)),
                to_destroy=int(match.group(3)),
            )
            if match
            else None
        )
        logger.info("drift.end", runner=runner_kind, project=project_name, drifted=True)
        return DriftResult(project=project_name, runner=runner_kind, drifted=True, summary=summary)

    # Any other exit code: tool name + exit code ONLY — never proc.stdout/
    # proc.stderr, which can echo resolved secret values.
    raise RuntimeError(f"{binary} drift check failed with exit code {proc.returncode}")
