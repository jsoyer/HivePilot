"""`gh` runner plugin — a plain, PATH-gated, opt-in COMMAND runner that
executes the GitHub CLI (``gh <args>``) for the operator-specified subcommand
in a step's ``command`` (e.g. ``pr create --title X``).

Deliberately NOT an agent kind: unlike antigravity/kimi/qwen (LLM agents in
``hivepilot.services.agent_checks.AGENT_RUNNER_KINDS`` /
``hivepilot.registry._OPTIONAL_AGENT_PLUGIN_KINDS`` /
``hivepilot.runners.prompt_cli_runner``), ``gh`` never sends a prompt to a
model — it shells out to a CLI tool exactly like the IaC runners
(``hivepilot.runners.iac_runner``) or ``plugins/rtk.py``. It is intentionally
absent from both agent-kind registries.

Modeled directly on ``plugins/rtk.py`` (the self-contained plugin-runner
pattern: plain class, same env-merge, same PATH-gated ``register()``) with
the destructive-operation gate modeled on
``hivepilot.runners.iac_runner._TfBaseRunner.is_destructive`` (resolve the
operation the SAME way ``run`` resolves it, so the gate always agrees with
what would actually execute).

``GhRunner`` satisfies ``hivepilot.runners.base.BaseRunner`` structurally
(same ``__init__(definition, settings)`` / ``run(payload)`` shape) WITHOUT
subclassing the ``Protocol`` — subclassing a ``Protocol`` class makes mypy
treat it as abstract, which is unnecessary friction for a plugin that isn't
part of the type-checked ``hivepilot`` package.

Deliberately NOT a ``@dataclass``: local-file plugins are loaded via
``importlib.util.spec_from_file_location()`` / ``exec_module()``
(``hivepilot.plugins._scan_local_plugins``), which never registers the
module in ``sys.modules``. Combined with ``from __future__ import
annotations``, that trips a real CPython 3.14 ``dataclasses`` bug
(``_is_type`` does ``sys.modules[cls.__module__].__dict__``, which is
``None`` for an unregistered module) — a plain class with an explicit
``__init__`` sidesteps it entirely and keeps this plugin robust across
Python versions and loading mechanisms.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from typing import Any

from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.plugins import HealthStatus
from hivepilot.runners.base import RunnerPayload
from hivepilot.templates import render_template
from hivepilot.utils.env import merge_environments
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Destructive/outward-irreversible gh operations, gated behind the
# step-level approval flow (same `getattr(runner, "is_destructive")`
# mechanism the IaC runners use — see `hivepilot.orchestrator.
# step_requires_approval`). Read-only/idempotent-create commands (`pr
# create`, `issue list`, `pr view`, `repo clone`, …) are intentionally
# excluded. `secret set` is included alongside `secret delete` because it
# overwrites a secret value irreversibly in the target repo/org.
_DESTRUCTIVE_OPS: frozenset[tuple[str, str]] = frozenset(
    {
        ("pr", "merge"),
        ("repo", "delete"),
        ("release", "delete"),
        ("secret", "delete"),
        ("secret", "set"),
        ("gist", "delete"),
        ("ssh-key", "delete"),
        ("cache", "delete"),
    }
)


class GhRunner:
    """Plain command runner that executes `gh <args>` from step config."""

    def __init__(self, definition: RunnerDefinition, settings: Settings) -> None:
        self.definition = definition
        self.settings = settings

    def run(self, payload: RunnerPayload) -> None:
        binary = shutil.which("gh")
        if binary is None:
            raise RuntimeError("gh CLI not found on PATH. Install it: see https://cli.github.com/")

        args = self._resolve_args(payload)
        argv = ["gh", *args]
        env = merge_environments(payload.project.env, self.definition.env, payload.secrets)

        logger.info(
            "gh_runner.start",
            project=payload.project_name,
            step=payload.step.name,
            args=args,
        )
        subprocess.run(
            argv,
            cwd=str(payload.project.path),
            env=env,
            check=True,
            text=True,
        )
        logger.info("gh_runner.end", project=payload.project_name, step=payload.step.name)

    def is_destructive(self, payload: RunnerPayload) -> bool:
        """Optional structural contract (getattr-discovered, like `capture` on
        other runners): True when the resolved `gh` subcommand pair is a
        known destructive/outward-irreversible operation. Resolved exactly
        the same way `run` resolves the command, so the gate always agrees
        with what would actually execute.

        Fail-safe default: an empty or unparseable command resolves to
        `False` (non-destructive). This gate is advisory over an
        operator-authored command — it is not a security boundary — so
        treating "can't tell" as non-destructive (rather than blocking every
        malformed command behind approval) is an intentional, documented
        tradeoff.
        """
        try:
            args = self._resolve_args(payload)
        except ValueError as exc:
            logger.debug("gh_runner.is_destructive_unresolvable", error=str(exc))
            return False
        if len(args) < 2:
            return False
        group, subcommand = args[0], args[1]
        return (group, subcommand) in _DESTRUCTIVE_OPS

    def _resolve_args(self, payload: RunnerPayload) -> list[str]:
        template = payload.step.command or self.definition.command
        if not template:
            raise ValueError(
                f"gh runner '{self.definition.name}' missing command "
                "(the gh subcommand, e.g. 'pr create --title X')"
            )

        context: dict[str, Any] = {
            "project_name": payload.project_name,
            "project_path": str(payload.project.path),
            "project_description": payload.project.description or "",
            "project_default_branch": payload.project.default_branch,
            "project_owner_repo": payload.project.owner_repo or "",
            "task_name": payload.task_name,
            "step_name": payload.step.name,
            "extra_prompt": payload.metadata.get("extra_prompt", ""),
        }
        rendered = render_template(template, context)
        try:
            return shlex.split(rendered)
        except ValueError:
            # Unbalanced quotes etc. — surfaced as a normal ValueError;
            # `is_destructive` catches this above and treats it as
            # non-destructive (see its docstring).
            raise


def health(**kwargs: Any) -> HealthStatus:
    """`ok` when `gh` is on PATH; `error` when it isn't — unlike `rtk`, there
    is no fallback execution path for this runner, so a missing binary is a
    hard error, not a degradation."""
    if shutil.which("gh"):
        return HealthStatus("ok", "gh on PATH")
    return HealthStatus("error", "gh not on PATH — install from https://cli.github.com/")


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.gh_enabled:
        return {}
    if shutil.which("gh") is None:
        return {}
    return {"runners": {"gh": GhRunner}, "health": {"gh": health}}
