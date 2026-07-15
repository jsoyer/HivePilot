from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hivepilot.models import PipelineConfig


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
) -> dict[str, Any] | None:
    """Write a per-stage run artifact to the Obsidian vault.

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

    Returns
    -------
    dict with keys ``path``, ``content``, ``dry_run`` — or ``None`` when
    *vault_path* is ``None``.
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
    from hivepilot.services.config_provenance import redact_text

    output = redact_text(output)

    today = datetime.date.today().isoformat()
    slug = _slugify(stage_name)
    subpath = f"{_RUNS_SUBFOLDER}/{today}-run{run_id}-{slug}.md"

    svc = ObsidianService(vault_path=vault_path, dry_run=dry_run)
    return svc.write_note(
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
