"""Henri — the external auditor / agent coach.

Henri is a meta-agent: he is NOT part of the delivery pipeline. He observes how
the other agents behave (via the interaction log) and proposes improvements to
their prompt files. He runs on Mistral through the ``vibe`` runner and never
applies prompt changes himself — he only proposes (a human approves).

Two modes:
- ``observe`` — light, per-cycle retrospective written to the Obsidian audit log.
- ``audit`` — deep, on-demand: proposes concrete edits to ``prompts/agents/*.md``.
"""

from __future__ import annotations

from pathlib import Path

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.roles import _resolve_prompt_path
from hivepilot.runners.base import RunnerPayload
from hivepilot.services import state_service
from hivepilot.services.obsidian_service import ObsidianService
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Bug 2 fix (run 243, live incident): "Prompt file not found:
# /opt/hivepilot/venv/lib/python3.14/site-packages/prompts/agents/auditor.md".
#
# The prompt filename Henri's own prompt is seeded/overridden under, relative
# to `prompts/agents/` -- resolved lazily by `_auditor_prompt_path()` below,
# NEVER as a module-level constant (see that function's docstring for why).
_AUDITOR_PROMPT_FILENAME = "auditor.md"


def _auditor_prompt_path() -> Path:
    """Resolve Henri's own prompt file through the EXACT SAME config chain
    every role prompt uses (`hivepilot.roles._resolve_prompt_path`): XDG
    config home -> config repo -> base_dir/cwd -> the packaged copy. This is
    the mechanism that makes `developer.md` (and every other role prompt)
    resolve correctly on a pip-installed deployment whose
    ``HIVEPILOT_CONFIG_REPO`` carries its own ``prompts/agents/`` tree.

    The auditor used to hardcode
    ``Path(__file__).resolve().parent.parent.parent / "prompts" / "agents" /
    "auditor.md"`` as a MODULE-LEVEL constant. That guess is correct only in
    a source checkout: `hivepilot/services/auditor_service.py`'s
    `.parent.parent.parent` is the repo root there, but under a pip install
    (`.../site-packages/hivepilot/services/auditor_service.py`) it lands at
    `site-packages/` -- one level ABOVE the `hivepilot` package -- so
    `site-packages/prompts/agents/auditor.md` never exists (the top-level
    `prompts/` tree is deliberately NOT shipped as package_data; see
    `pyproject.toml`'s `[tool.setuptools.package-data]` comment -- it is
    operator-editable / config-repo-seeded, exactly like every role prompt).
    Worse, that ALREADY-absolute guess was then passed straight into
    `PromptCliRunner._load_prompt`'s own `settings.resolve_config_path(...)`
    call, which is a no-op for an absolute path (joining an absolute path
    onto any base just returns the absolute path per `pathlib`'s
    `Path.__truediv__`) -- so the config-repo/XDG tiers were never actually
    consulted for the auditor at all, unlike every other role.

    Resolved as a FUNCTION (not a module-level constant) so it re-resolves
    against the CURRENT config-repo/XDG state on every call, and so it can
    raise an ACTIONABLE error (see below) instead of silently handing a
    nonexistent path to the runner, which would otherwise surface only as a
    bare, unexplained ``FileNotFoundError`` from deep inside
    ``PromptCliRunner._load_prompt``.
    """
    resolved = _resolve_prompt_path(_AUDITOR_PROMPT_FILENAME, settings)
    if not resolved.exists():
        raise FileNotFoundError(
            f"Henri's own prompt file ({_AUDITOR_PROMPT_FILENAME!r}) could not be "
            f"found in any of the usual locations: {settings.xdg_config_home}/prompts/"
            f"agents/, the config repo (HIVEPILOT_CONFIG_REPO), "
            f"{settings.base_dir}/prompts/agents/, or the packaged copy. "
            "This is expected on a pip install whose config repo doesn't carry a "
            "prompts/agents/ tree yet. "
            "fix: add prompts/agents/auditor.md to your HIVEPILOT_CONFIG_REPO (copy "
            "it from https://github.com/jsoyer/HivePilot/blob/main/prompts/agents/"
            "auditor.md if you don't have one) and run `hivepilot config sync`."
        )
    return resolved


def _run_henri(project: ProjectConfig, context: str, registry, *, label: str) -> str:
    """Invoke Henri (Mistral via the vibe runner) with the auditor prompt + context."""
    step = TaskStep(name=label, runner="vibe", prompt_file=str(_auditor_prompt_path()))
    payload = RunnerPayload(
        project_name=project.path.name,
        project=project,
        task_name=label,
        step=step,
        metadata={"prior_context": context},
        secrets={},
    )
    rdef = RunnerDefinition(name="auditor:henri", kind="vibe", command=None, model=None)
    return registry.capture_definition(rdef, payload).strip()


def _write(
    project: ProjectConfig, subpath: str, title: str, body: str, dry_run: bool, fm: dict
) -> None:
    # Per-project-vault PRD: the auditor's observations are ABOUT this
    # project, so they belong in this project's vault. `resolve_vault_path`
    # falls back to the unchanged global expression when the project declares
    # no `obsidian_vault:` override. A broken override must never take down an
    # auditor pass (best-effort, like every other write here) -- it is logged
    # and skipped; the loud failure for the same misconfiguration is raised up
    # front by `Orchestrator._run_pipeline_body`.
    from hivepilot.services.obsidian_vault_resolver import (
        VaultResolutionError,
        resolve_vault_path,
    )

    try:
        vault = resolve_vault_path(
            project,
            settings.obsidian_vault if settings.obsidian_vault.exists() else None,
        )
    except VaultResolutionError as exc:
        logger.warning("auditor.vault_unresolvable", project=project.path.name, error=str(exc))
        return
    if vault is None:
        return
    ObsidianService(vault, dry_run=dry_run).write_note(
        subpath=subpath, title=title, body=body, frontmatter_fields=fm
    )


def observe(*, project: ProjectConfig, run_id: int, registry, dry_run: bool = True) -> str:
    """Light per-cycle retrospective: Henri reviews a run's interactions and notes
    what went well/badly. Writes to the Obsidian audit log. Returns his note."""
    interactions = state_service.list_recent_interactions(limit=100, run_id=run_id)
    context = "Run interactions (oldest first):\n" + "\n".join(
        f"- {i['actor']} -> {i.get('target') or '—'}: {(i.get('summary') or '')[:200]}"
        for i in reversed(interactions)
    )
    note = _run_henri(project, context, registry, label=f"audit-observe-{run_id}")
    _write(
        project,
        f"Audit/observation-run-{run_id}.md",
        f"Audit observation — run {run_id}",
        note,
        dry_run,
        {"type": "audit", "kind": "observation", "run_id": run_id, "agent": "Henri"},
    )
    logger.info("auditor.observe", run_id=run_id, project=project.path.name)
    return note


def audit(*, project: ProjectConfig, registry, dry_run: bool = True) -> str:
    """Deep audit: Henri reviews recent interactions across runs and proposes
    concrete improvements to the agent prompt files. Proposes only — never applies."""
    interactions = state_service.list_recent_interactions(limit=200)
    context = (
        "Recent interactions across runs (oldest first):\n"
        + "\n".join(
            f"- {i['actor']} -> {i.get('target') or '—'}: {(i.get('summary') or '')[:150]}"
            for i in reversed(interactions)
        )
        + "\n\nNow run a DEEP audit: propose concrete edits to the agent prompt files "
        "(prompts/agents/*.md). Output suggestions only — do not apply them."
    )
    proposal = _run_henri(project, context, registry, label="audit-deep")
    _write(
        project,
        "Audit/proposal-latest.md",
        "Audit proposal — prompt improvements",
        proposal,
        dry_run,
        {"type": "audit", "kind": "proposal", "agent": "Henri"},
    )
    logger.info("auditor.audit", project=project.path.name)
    return proposal
