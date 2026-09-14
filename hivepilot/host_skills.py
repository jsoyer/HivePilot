"""HP-112 host skills — local skill-discovery + delegate-task.

OpenSpace host_skills pattern, rewritten in Python. Discovery is HP-107
BM25 over the HP-98 catalog. Delegate is HivePilot ``run_subagent`` /
``spawn_peer`` / ``Orchestrator.run_pipeline``. This module does not
vendor OpenSpace, persist pickle, or call a remote skill host.

Contracts:

- ``discover`` ranks enabled (and optionally provisional) active
  revisions. Hits are cards; ``disclose`` returns the body after
  selection. No query argument attaches a skill to a pipeline stage
  (HP-108 / HP-103 attach stays explicit YAML).
- ``delegate`` mode is ``subagent`` | ``peer`` | ``pipeline``. Those
  map to the existing HivePilot spawn APIs. A pipeline run needs a
  registered runner or an orchestrator with ``run_pipeline``.
- Host skill specs (``skill-discovery``, ``delegate-task``) are
  capability text only. They grant no HP-95 tool tokens.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hivepilot.services.delegation import run_subagent, spawn_peer
from hivepilot.skill_catalog import SkillCatalog, SkillListing, scan_catalog
from hivepilot.skill_ranker import DEFAULT_TOP_K, SkillDisclosure, SkillHit, SkillRanker

HOST_SKILL_NAMES: tuple[str, ...] = ("skill-discovery", "delegate-task")
DELEGATE_MODES: tuple[str, ...] = ("subagent", "peer", "pipeline")

PipelineRunner = Callable[[str, str, str, str], object]

_pipeline_runner: PipelineRunner | None = None

_DISCOVERY_MD = """# Skill Discovery

Find reusable HivePilot skills on this host. Search is local BM25 over
the HP-98 catalog. HP-105 enabled / provisional filters run before
scoring. Hits are cards — name, description, score — without the skill
body. Call disclose after you select a revision.

## When to use

- The operator asks which skills exist, or whether a skill covers a task
- You want a proven procedure before inventing one
- You need to decide: follow a skill yourself, or hand the work off via
  delegate-task

## discover

Host entry: `discover(query, top_k=10, include_provisional=True)`.
Query is required. Optional `disclose_revision_id` returns the selected
body in the same report.

## After search

- Match you can follow → disclose, then follow the body.
- Match you cannot run → delegate-task (`subagent`, `peer`, or
  `pipeline`).
- No match → handle it yourself, or delegate a named HivePilot pipeline
  or role.

## Notes

- Discovery never attaches a skill to a pipeline stage. Attach stays
  explicit YAML (`PipelineStage.skills` / `hivepilot stage attach-skill`).
- Disabled and unknown catalog rows never enter the ranking corpus.
- Tell the operator what you found and what you recommend.
"""

_DELEGATE_MD = """# Delegate a Task

Hand work to a HivePilot helper when this turn cannot finish it.
Modes are local: a turn-scoped subagent, a background peer run, or a
named pipeline.

## When to use

- You lack the role, tools, or time for the work
- You already failed and a peer role or pipeline is the right owner
- The operator asks to spawn a helper, a background peer, or a pipeline

## Modes

### subagent

Turn-scoped helper. Calls `run_subagent(role, prompt)` once. No
persistent space. Returns helper text, or empty when no executor is
registered.

### peer

Background run. Calls `spawn_peer(project, task, role)` and returns the
run id. A registered peer executor may pick the run up; otherwise the
row waits for a worker.

### pipeline

Named pipeline on a project. Calls the registered HivePilot pipeline
runner, or `orchestrator.run_pipeline` when an orchestrator is passed.
Requires `project` and `pipeline`.

## After delegate

Report the mode, status, and run id or helper text to the operator.
Delegation does not write skill files or accept evolution drafts.
"""


class HostSkillsError(ValueError):
    """Invalid discover / delegate arguments."""


@dataclass(frozen=True)
class DiscoveryReport:
    """Ranked cards plus an optional disclosed body."""

    query: str
    hits: tuple[SkillHit, ...]
    disclosure: SkillDisclosure | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "hits": [hit.to_dict() for hit in self.hits],
            "disclosure": None if self.disclosure is None else self.disclosure.to_dict(),
        }


@dataclass(frozen=True)
class DelegateReport:
    """Outcome of a local subagent, peer, or pipeline hand-off."""

    mode: str
    status: str
    role: str = ""
    project: str = ""
    task: str = ""
    pipeline: str = ""
    output: str | None = None
    run_id: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "status": self.status,
            "role": self.role,
            "project": self.project,
            "task": self.task,
            "pipeline": self.pipeline,
            "output": self.output,
            "run_id": self.run_id,
        }


def register_pipeline_runner(fn: PipelineRunner | None) -> None:
    """Inject the pipeline runner used by ``delegate(mode='pipeline')``."""
    global _pipeline_runner
    _pipeline_runner = fn


def pipeline_via_orchestrator(
    orchestrator: object,
    project: str,
    pipeline: str,
    prompt: str = "",
    *,
    dry_run: bool = False,
    auto_git: bool = False,
) -> object:
    """Call ``orchestrator.run_pipeline`` with HivePilot keyword args."""
    run_pipeline = getattr(orchestrator, "run_pipeline", None)
    if not callable(run_pipeline):
        raise HostSkillsError("orchestrator is missing run_pipeline")
    return run_pipeline(
        project_names=[project],
        pipeline_name=pipeline,
        extra_prompt=prompt or None,
        auto_git=auto_git,
        dry_run=dry_run,
    )


def skill_specs() -> tuple[dict[str, Any], ...]:
    """Capability specs for the two host skills. No tool tokens."""
    return (
        {
            "name": "skill-discovery",
            "description": (
                "Search the local HivePilot skill catalog with deterministic "
                "BM25. Rank cards first; read the skill body only after you "
                "select a hit."
            ),
            "provider": "host_skills",
            "files": {
                "SKILL.md": (
                    "---\n"
                    "name: skill-discovery\n"
                    "description: Search the local HivePilot skill catalog "
                    "with deterministic BM25.\n"
                    "---\n"
                    f"{_DISCOVERY_MD}"
                )
            },
        },
        {
            "name": "delegate-task",
            "description": (
                "Hand work to a HivePilot subagent, peer run, or named "
                "pipeline. Local host APIs only."
            ),
            "provider": "host_skills",
            "files": {
                "SKILL.md": (
                    "---\n"
                    "name: delegate-task\n"
                    "description: Hand work to a HivePilot subagent, peer "
                    "run, or named pipeline.\n"
                    "---\n"
                    f"{_DELEGATE_MD}"
                )
            },
        },
    )


def _resolve_catalog(
    catalog: SkillCatalog | None,
    *,
    plugin_manager: SkillListing | None,
    plugin_skills: Iterable[Mapping[str, Any]] | None,
    base_dir: Path | None,
) -> SkillCatalog:
    if catalog is not None:
        return catalog
    return scan_catalog(
        base_dir=base_dir,
        plugin_manager=plugin_manager,
        plugin_skills=plugin_skills,
    )


def discover(
    query: str,
    *,
    catalog: SkillCatalog | None = None,
    tenant: str = "default",
    top_k: int = DEFAULT_TOP_K,
    include_provisional: bool = True,
    disclose_revision_id: str | None = None,
    plugin_manager: SkillListing | None = None,
    plugin_skills: Iterable[Mapping[str, Any]] | None = None,
    base_dir: Path | None = None,
) -> DiscoveryReport:
    """Rank local catalog skills. Never attaches a skill to a stage."""
    resolved = _resolve_catalog(
        catalog,
        plugin_manager=plugin_manager,
        plugin_skills=plugin_skills,
        base_dir=base_dir,
    )
    ranker = SkillRanker(resolved, tenant=tenant)
    hits = ranker.retrieve(
        query,
        top_k=top_k,
        include_provisional=include_provisional,
    )
    card: SkillDisclosure | None = None
    if disclose_revision_id:
        card = ranker.disclose(disclose_revision_id)
    return DiscoveryReport(query=query, hits=hits, disclosure=card)


def disclose(
    catalog: SkillCatalog,
    revision_id: str,
    *,
    tenant: str = "default",
) -> SkillDisclosure | None:
    """Return the selected revision body. Progressive disclosure."""
    return SkillRanker(catalog, tenant=tenant).disclose(revision_id)


def delegate(
    mode: str,
    *,
    prompt: str = "",
    role: str = "",
    project: str = "",
    task: str = "",
    pipeline: str = "",
    tenant: str = "default",
    orchestrator: object | None = None,
) -> DelegateReport:
    """Hand work to a HivePilot subagent, peer, or pipeline."""
    cleaned = (mode or "").strip()
    if cleaned not in DELEGATE_MODES:
        raise HostSkillsError(
            f"unknown delegate mode {mode!r}; must be one of {list(DELEGATE_MODES)}"
        )
    if cleaned == "subagent":
        return _delegate_subagent(role=role, prompt=prompt)
    if cleaned == "peer":
        return _delegate_peer(project=project, task=task, role=role, tenant=tenant)
    return _delegate_pipeline(
        project=project,
        pipeline=pipeline,
        prompt=prompt,
        tenant=tenant,
        orchestrator=orchestrator,
    )


def _require(value: str, field: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise HostSkillsError(f"{field} is required")
    return cleaned


def _delegate_subagent(*, role: str, prompt: str) -> DelegateReport:
    resolved_role = _require(role, "role")
    resolved_prompt = _require(prompt, "prompt")
    text = run_subagent(resolved_role, resolved_prompt)
    return DelegateReport(
        mode="subagent",
        status="ok" if text else "empty",
        role=resolved_role,
        output=text,
    )


def _delegate_peer(
    *,
    project: str,
    task: str,
    role: str,
    tenant: str,
) -> DelegateReport:
    resolved_project = _require(project, "project")
    resolved_task = _require(task, "task")
    resolved_role = _require(role, "role")
    run_id = spawn_peer(resolved_project, resolved_task, resolved_role, tenant=tenant)
    return DelegateReport(
        mode="peer",
        status="spawned",
        role=resolved_role,
        project=resolved_project,
        task=resolved_task,
        run_id=run_id,
    )


def _delegate_pipeline(
    *,
    project: str,
    pipeline: str,
    prompt: str,
    tenant: str,
    orchestrator: object | None,
) -> DelegateReport:
    resolved_project = _require(project, "project")
    resolved_pipeline = _require(pipeline, "pipeline")
    if orchestrator is not None:
        output = pipeline_via_orchestrator(
            orchestrator,
            resolved_project,
            resolved_pipeline,
            prompt,
        )
    else:
        runner = _pipeline_runner
        if runner is None:
            raise HostSkillsError("pipeline runner is not registered")
        output = runner(resolved_project, resolved_pipeline, prompt, tenant)
    rendered = output if isinstance(output, str) else repr(output)
    return DelegateReport(
        mode="pipeline",
        status="ran",
        project=resolved_project,
        pipeline=resolved_pipeline,
        output=rendered,
    )


def ingest_host_skills(catalog: SkillCatalog) -> tuple[Any, ...]:
    """Add the two host skill specs to *catalog* (IMPORTED)."""
    return tuple(catalog.ingest(spec) for spec in skill_specs())
