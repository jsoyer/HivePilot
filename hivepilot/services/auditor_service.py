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
from hivepilot.runners.base import RunnerPayload
from hivepilot.services import state_service
from hivepilot.services.obsidian_service import ObsidianService
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

AUDITOR_PROMPT = Path(__file__).resolve().parent.parent.parent / "prompts" / "agents" / "auditor.md"


def _run_henri(project: ProjectConfig, context: str, registry, *, label: str) -> str:
    """Invoke Henri (Mistral via the vibe runner) with the auditor prompt + context."""
    step = TaskStep(name=label, runner="vibe", prompt_file=str(AUDITOR_PROMPT))
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
    vault = settings.obsidian_vault if settings.obsidian_vault.exists() else None
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
