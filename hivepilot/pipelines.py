from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hivepilot.models import PipelineConfig
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class PipelineExecutionContext:
    pipeline: PipelineConfig
    project_names: list[str]


def describe_pipeline(pipeline: PipelineConfig) -> str:
    parts = [f"{idx + 1}. {stage.name} → {stage.task}" for idx, stage in enumerate(pipeline.stages)]
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Vault artifact helper
# ---------------------------------------------------------------------------

_RUNS_SUBFOLDER = "Runs"

# The developer role's deliverable is a code diff destined for the project's
# own git repo, not the vault (mirrors the `task.role == "developer"`
# special-casing hivepilot.orchestrator already uses for the same
# distinction, e.g. dev-fallback-runner selection). Excluded from the
# canonical `02 - Artifacts/<role>/` copy below.
_DEVELOPER_ROLE = "developer"


def _slugify(text: str) -> str:
    """Convert *text* to a lowercase-kebab filename slug."""
    slug = text.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug.strip("-")


def write_stage_artifact(
    vault_path: Path | str | None,
    run_id: int,
    stage_name: str,
    output: str,
    dry_run: bool = True,
    role: str | None = None,
) -> dict[str, Any] | None:
    """Write a per-stage run artifact to the Obsidian vault.

    In addition to the internal ``12 - HivePilot/Runs/`` run-log copy this
    has always written, also writes the stage's DELIVERABLE to the canonical
    ``02 - Artifacts/<role>/<date>-<slug>.md`` location when *role* is given
    (vault-canonical-artifacts PRD). This is the mechanism that lets planning
    agents (CEO/PM/CTO/designers) skip writing their own deliverable via the
    agent's Write tool — which is blocked in headless ``--print`` mode and
    the vault dir isn't in the agent's accessible dirs. HivePilot persists it
    itself, using its OWN file access against `settings.obsidian_vault`.

    Parameters
    ----------
    vault_path:
        Absolute path to the Obsidian vault root.  When ``None`` the function
        returns ``None`` silently — callers with no vault configured are safe.
    run_id:
        Numeric run identifier (used in the filename).
    stage_name:
        Human-readable stage name (e.g. ``"CEO Intake"``).
    output:
        Markdown body content produced by the stage.
    dry_run:
        When ``True`` (default) no file is written; the planned path and
        content are returned in the result dict.
    role:
        The stage's producing role (e.g. ``"cto"``). ``None`` (the default —
        e.g. a stage whose task has no `role`) skips the canonical artifact
        copy entirely; only the run-copy is written, unchanged from before
        this parameter existed. The ``"developer"`` role is also skipped —
        its deliverable is a code diff that belongs in the project's repo,
        not the vault. Whitespace-only `output` is skipped too (no empty
        artifact files).

    Returns
    -------
    dict with keys ``path``, ``content``, ``dry_run`` — or ``None`` when
    *vault_path* is ``None``. Reflects the run-copy write only; the
    canonical-artifact copy (when written) is best-effort and never affects
    this return value.
    """
    if vault_path is None:
        return None

    try:
        from hivepilot.services.obsidian_service import ObsidianService
    except ImportError:
        return None

    # Choke point: `output` is a stage's aggregated agent output and can echo
    # a resolved ${secret:NAME} value. Redact before it reaches the vault note
    # body — including the dry_run preview dict, which callers may surface.
    # (ObsidianService._emit redacts again on every write — harmless, idempotent
    # double coverage — but redacting here also protects the dry_run preview
    # dict returned below, before it ever reaches `_emit`.)
    from hivepilot.services.config_provenance import redact_text

    output = redact_text(output)

    today = datetime.date.today().isoformat()
    slug = _slugify(stage_name)
    subpath = f"{_RUNS_SUBFOLDER}/{today}-run{run_id}-{slug}.md"

    svc = ObsidianService(vault_path=vault_path, dry_run=dry_run)
    result = svc.write_note(
        subpath=subpath,
        title=f"Run {run_id} — {stage_name}",
        body=output,
        frontmatter_fields={
            "type": "run-artifact",
            "status": "complete",
            "created": today,
            "agent": "hivepilot",
            "run_id": run_id,
            "stage": stage_name,
            "language": "en",
        },
    )

    # Canonical stage-deliverable artifact (02 - Artifacts/<role>/...).
    # Scope: only stages with a resolvable role, excluding the developer/code
    # stage (its diff goes to the repo, not the vault) and blank output (no
    # empty artifact files). Best-effort — a vault-write error here must
    # NEVER fail or pause the run, same posture as the run-copy above.
    if role and role != _DEVELOPER_ROLE and output.strip():
        try:
            svc.write_artifact(
                role=role,
                slug=f"run{run_id}-{slug}",
                title=f"{stage_name} — Run {run_id}",
                body=output,
                frontmatter_fields={
                    "type": "artifact",
                    "status": "complete",
                    "created": today,
                    "agent": "hivepilot",
                    "run_id": run_id,
                    "stage": stage_name,
                    "role": role,
                    "language": "en",
                },
            )
        except Exception as exc:  # noqa: BLE001 — best-effort: never fail/pause the run
            logger.warning(
                "obsidian.artifact_write_failed",
                run_id=run_id,
                stage=stage_name,
                role=role,
                error=str(exc),
            )

    return result
