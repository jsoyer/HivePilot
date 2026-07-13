from __future__ import annotations

import concurrent.futures
import json
import subprocess
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import questionary

from hivepilot.config import settings
from hivepilot.models import Group, PipelineStage, ProjectConfig, TaskConfig, TaskStep

try:
    from hivepilot.services import metrics as _metrics  # noqa: F401

    _METRICS_AVAILABLE = True
except ImportError:
    _metrics = None  # type: ignore[assignment]
    _METRICS_AVAILABLE = False
from hivepilot.pipelines import write_stage_artifact
from hivepilot.plugins import PluginManager
from hivepilot.registry import RunnerRegistry
from hivepilot.runners.base import RunnerPayload
from hivepilot.services import (
    knowledge_service,
    notification_service,
    policy_service,
    state_service,
)
from hivepilot.services.agent_report import parse_agent_report, parse_agent_requests
from hivepilot.services.artifact_service import ArtifactManager
from hivepilot.services.git_service import isolated_worktree, perform_git_actions
from hivepilot.services.interaction_service import (
    Interaction,
    InteractionService,
    log_challenge_interaction,
    log_request_interaction,
)
from hivepilot.services.obsidian_service import ObsidianService
from hivepilot.services.pipeline_service import validate_pipeline
from hivepilot.services.project_service import load_pipelines, load_projects, load_tasks
from hivepilot.services.secrets_service import secret_resolver
from hivepilot.services.state_service import RunStatus
from hivepilot.utils.io import create_run_directory, write_summary
from hivepilot.utils.logging import get_logger

if TYPE_CHECKING:
    from hivepilot.roles import Role

logger = get_logger(__name__)

_DETAILS_MAX_BULLETS = 6
_DETAILS_FALLBACK_CHARS = 600


def _resolve_role_from_display(display_name: str) -> str | None:
    """Resolve a role key from a display string like "Aliénor (CEO)".

    Searches the ROLES registry for a role whose ``display_name`` or ``title``
    matches *display_name* (case-insensitive, partial/substring match).
    Returns the role key (e.g. ``"ceo"``) or ``None`` if nothing matches.
    """
    from hivepilot.roles import ROLES

    needle = display_name.strip().lower()
    for role_key, role in ROLES.items():
        candidates = []
        if role.display_name:
            candidates.append(role.display_name.lower())
        if role.title:
            candidates.append(role.title.lower())
        # Also check the formatted "display_name (title)" pattern
        if role.display_name and role.title:
            candidates.append(f"{role.display_name.lower()} ({role.title.lower()})")
        for candidate in candidates:
            if candidate in needle or needle in candidate:
                return role_key
    return None


def _build_checkpoint_details(
    prior_chunks: list[str],
    completed: list[str],
    next_stage: str,
    components: list[str],
    group_mode: bool,
) -> str:
    """Build a human-readable details string for the Telegram approval DM.

    Pure function — no I/O, no side-effects; safe to unit-test in isolation.

    The resulting string is composed of (in order):
    - Components line (group mode only)
    - Completed / next stage lines
    - Plan summary extracted from the last prior_chunk via parse_agent_report;
      falls back to a plain text excerpt when structured parsing yields nothing.
    - A footer pointing to the Obsidian vault.
    """
    lines: list[str] = []

    if group_mode and components:
        lines.append(f"🎯 *Components:* {', '.join(components)}")

    if completed:
        lines.append(f"✅ *Completed:* {', '.join(completed)}")
    lines.append(f"▶️ *Next:* {next_stage}")

    last_chunk = prior_chunks[-1].strip() if prior_chunks else ""
    if last_chunk:
        report = parse_agent_report(last_chunk)
        bullets = report.summary[:_DETAILS_MAX_BULLETS]
        if bullets:
            lines.append("\n📋 *Plan summary:*")
            for bullet in bullets:
                lines.append(f"  • {bullet}")
        else:
            # Fallback: plain excerpt of the last chunk
            excerpt = last_chunk[:_DETAILS_FALLBACK_CHARS]
            if len(last_chunk) > _DETAILS_FALLBACK_CHARS:
                excerpt += "…"
            lines.append(f"\n📋 *Plan excerpt:*\n{excerpt}")

    lines.append("\n📂 _Full plan: in the Obsidian vault._")

    return "\n".join(lines)


def _runner_for_stage(stage: PipelineStage) -> str:
    """Return the runner name for a pipeline stage.

    Currently always returns ``"claude"`` (Claude-first seam).  Future sprints
    may inspect *stage* fields (e.g. a ``runner`` override) to route to other
    runners.
    """
    return "claude"


def _parse_brain(entry: str, default_runner: str) -> tuple[str, str]:
    """Split a debate brain spec into ``(runner, model)``.

    ``"runner:model"`` (e.g. ``"claude:claude-sonnet-4-6"``) pins a runner for that
    brain; a bare model uses the role's default runner. Only a recognised
    runner-kind prefix is treated as a runner, so ``"opencode-go/kimi"`` and
    other slash-style ids stay plain models.
    """
    from hivepilot.models import KNOWN_RUNNER_KINDS
    from hivepilot.registry import RUNNER_MAP

    if ":" in entry:
        prefix, rest = entry.split(":", 1)
        if prefix in (frozenset(RUNNER_MAP) | frozenset(KNOWN_RUNNER_KINDS)):
            return prefix, rest
    return default_runner, entry


def _parse_components(text: str, valid: list[str]) -> list[str]:
    """Extract the component subset an agent selected via a ``COMPONENTS:`` line,
    intersected with the *valid* component set. Returns ``[]`` if none matched
    (callers fall back to all components)."""
    import re

    valid_set = set(valid)
    found: list[str] = []
    for line in text.splitlines():
        m = re.match(r"\s*COMPONENTS\s*:\s*(.+)", line, re.IGNORECASE)
        if not m:
            continue
        for tok in re.split(r"[,\s]+", m.group(1).strip()):
            tok = tok.strip().strip(".`*")
            if tok in valid_set and tok not in found:
                found.append(tok)
    return found


def _parse_output_sections(text: str, keys: list[str]) -> dict[str, str]:
    """Extract per-key ``## <HEADER>`` sections from a stage's output text.

    A *key* (e.g. ``"design_spec"``) matches a header (e.g. ``"## DESIGN_SPEC"``,
    ``"## Design Spec"``, or ``"## design-spec"``) when, after upper-casing both
    and collapsing runs of ``_``, ``-``, and whitespace to a single ``_``, they
    are equal. A section's body is every line following its header up to the
    next ``## `` header (matched or not) or the end of *text*, with
    leading/trailing blank lines stripped.

    Returns ``{key: section_body}`` only for keys whose section was found —
    mirrors ``_parse_components``'s "empty when none found" style so callers
    can fall back (here: the whole-blob coarse fallback in the stage loop,
    see ``_stage_outputs_by_key``)."""
    import re

    def _normalize(header: str) -> str:
        return re.sub(r"[\s_-]+", "_", header.strip()).upper()

    key_by_norm = {_normalize(k): k for k in keys}
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        m = re.match(r"^##(?!#)\s+(.+?)\s*$", line)
        if m:
            current = key_by_norm.get(_normalize(m.group(1)))
            if current is not None:
                sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in sections.items()}


def _stage_outputs_by_key(stage_output: str, keys: list[str]) -> dict[str, str]:
    """Map a producing stage's declared output *keys* to content for the
    run-scoped keyed store (PRD A2): section-extracted where a ``## <KEY>``
    header is present in *stage_output*, else the whole *stage_output* blob
    (coarse fallback) so every declared key always resolves to something.

    Built but not consumed this sprint — see PRD A2 Sprint 1. Callers merging
    this into a run-scoped dict across stages should let later stages
    overwrite same-key entries from earlier stages (last producer wins)."""
    sections = _parse_output_sections(stage_output, keys)
    return {key: sections.get(key, stage_output) for key in keys}


def _resolve_stage_target_components(
    stage: PipelineStage, group_tags: dict[str, list[str]]
) -> set[str]:
    """Union of a stage's ``only_components`` and the components tagged by any
    of its ``only_tags`` (resolved against *group_tags*, tag -> component names).

    Returns an empty set when the stage declares neither selector.
    """
    target = set(stage.only_components or [])
    for tag in stage.only_tags or []:
        target |= set(group_tags.get(tag, []))
    return target


def _validate_stage_tags(stages: list[PipelineStage], group_tags: dict[str, list[str]]) -> None:
    """Fail-closed guard: every ``only_tags`` value referenced by any stage must
    be defined in the run's ``Group.tags``.

    Raises ``ValueError`` naming the offending tag on the first mismatch found.
    Called once, up front (before any stage executes), so a mistyped or
    undefined tag on e.g. a review/security stage can never be silently
    bypassed by that stage instead running unscoped or being skipped.
    """
    for stage in stages:
        for tag in stage.only_tags or []:
            if tag not in group_tags:
                raise ValueError(
                    f"Pipeline stage '{stage.name}' references undefined tag "
                    f"'{tag}' (not present in this run's Group.tags)"
                )


def _stage_should_skip(
    stage: PipelineStage,
    group_tags: dict[str, list[str]],
    selected_components: list[str],
) -> bool:
    """A stage is skipped iff its scoping target (``only_components`` union
    ``only_tags``-resolved components) is non-empty AND disjoint from the
    components selected for this run. A stage with neither selector set
    always runs (target is empty -> never skipped)."""
    target = _resolve_stage_target_components(stage, group_tags)
    if not target:
        return False
    return target.isdisjoint(selected_components)


def build_prior_context(
    prior_chunks: list[str],
    mode: str,
    max_chars: int,
) -> str | None:
    """Build the prior_context string to pass to the next stage.

    Modes:
    - full: join all chunks unchanged.
    - synthesis: keep only the chunk whose header contains "Plan Synthesis"
      (case-insensitive) if found, plus the most recent chunk. If no synthesis
      chunk exists, keep only the most recent chunk.
    - cap: join all, truncate to max_chars keeping the TAIL (most recent content),
      prepend '…[earlier context truncated]…' if truncation occurred.

    Returns None if prior_chunks is empty.
    """
    if not prior_chunks:
        return None
    if mode == "synthesis":
        synthesis = next((c for c in prior_chunks if "plan synthesis" in c.lower()), None)
        last = prior_chunks[-1]
        parts = []
        if synthesis and synthesis is not last:
            parts.append(synthesis)
        parts.append(last)
        return "\n\n".join(parts)
    joined = "\n\n".join(prior_chunks)
    if mode == "cap":
        if len(joined) > max_chars:
            truncated = joined[-max_chars:]
            return "\u2026[earlier context truncated]\u2026\n\n" + truncated
        return joined
    # mode == "full"
    return joined


def _route_prior_context(
    *,
    role: "Role | None",
    prior_chunks: list[str],
    outputs_by_key: dict[str, str],
    routing_mode: str,
    prior_context_mode: str,
    max_chars: int,
    stage_name: str,
) -> str | None:
    """Compute the prior_context string for the stage about to run (PRD A2 Sprint 2).

    Routing is gated ONLY on ``routing_mode == "keyed"`` — NEVER on whether
    *role* declares ``inputs``. ``roles.yaml`` already declares ``inputs`` on
    EVERY role (cosmetically), so gating on presence-of-inputs instead of the
    explicit flag would silently regress every existing pipeline to a keyed
    subset. In ``full`` mode (default) this always falls through to
    ``build_prior_context(prior_chunks, ...)`` — byte-identical to
    pre-Sprint-2 behaviour for every role, regardless of its declared inputs.

    In ``keyed`` mode, a role with non-empty ``inputs`` gets its context
    assembled from ONLY the declared input keys present in *outputs_by_key*
    (Sprint 1's run-scoped store), joined as ``## <KEY>`` blocks and capped
    with the same tail-truncation rule as ``build_prior_context``'s "cap" mode.

    Conservative fallback rule:
    - ALL declared input keys missing from the store -> the keyed slice would
      be empty, which is worse than no routing at all, so fall back to the
      full ``build_prior_context(prior_chunks, ...)`` and log a warning naming
      the missing keys.
    - SOME (not all) declared input keys present -> use exactly what's
      present; a non-empty keyed subset is still more precise than the full
      context, so no fallback in that case.
    - Role has no declared ``inputs`` (empty list) -> not routable at all;
      falls through to the full context, same as ``full`` mode.
    """
    if routing_mode == "keyed" and role is not None and role.inputs:
        present = {k: outputs_by_key[k] for k in role.inputs if k in outputs_by_key}
        if present:
            joined = "\n\n".join(f"## {k.upper()}\n{v}" for k, v in present.items())
            if len(joined) > max_chars:
                return "\u2026[earlier context truncated]\u2026\n\n" + joined[-max_chars:]
            return joined
        missing = [k for k in role.inputs if k not in outputs_by_key]
        logger.warning(
            "pipeline.keyed_context_fallback",
            stage=stage_name,
            missing_keys=missing,
        )
    return build_prior_context(prior_chunks, mode=prior_context_mode, max_chars=max_chars)


@dataclass
class RunResult:
    project: str
    target: str
    success: bool
    detail: str | None = None
    # True for a stage skipped via only_components/only_tags scoping — distinct
    # from both success and failure in the run record (PRD A1 §6).
    skipped: bool = False


class Orchestrator:
    def __init__(self) -> None:
        self._load()

    def _load(self) -> None:
        self.projects = load_projects()
        self.tasks = load_tasks()
        self.pipelines = load_pipelines()
        self.registry = RunnerRegistry(self.tasks.runners)
        self.plugins = PluginManager()

    def refresh(self) -> None:
        self._load()

    def run_task(
        self,
        *,
        project_names: Iterable[str],
        task_name: str,
        extra_prompt: str | None,
        auto_git: bool,
        concurrency: int | None = None,
        simulate: bool = False,
        dry_run: bool = True,
        prior_context: str | None = None,
    ) -> list[RunResult]:
        if task_name not in self.tasks.tasks:
            raise ValueError(f"Unknown task: {task_name}")
        task = self.tasks.tasks[task_name]
        projects = [self._project(name) for name in project_names]
        run_dir = create_run_directory()
        limit = concurrency or settings.concurrency_limit
        results: list[RunResult] = []
        run_policies: dict[str, policy_service.Policy] = {}
        run_ids: dict[str, int] = {}
        notion_page_ids: dict[str, str | None] = {}
        immediate_projects: list[ProjectConfig] = []
        for project in projects:
            policy = policy_service.enforce_policy(project.path.name, auto_git=auto_git)
            run_policies[project.path.name] = policy
            if policy.require_approval and not simulate:
                run_id = state_service.record_run_start(
                    project.path.name, task_name, status="pending"
                )
                approval_meta = {
                    "task": task_name,
                    "project": project.path.name,
                    "extra_prompt": extra_prompt,
                    "auto_git": auto_git,
                }
                state_service.record_approval_request(
                    run_id, project.path.name, task_name, approval_meta
                )
                notification_service.send_approval_keyboard(
                    run_id=run_id, project=project.path.name, task=task_name
                )
                results.append(
                    RunResult(
                        project.path.name, task_name, False, f"Pending approval (run {run_id})"
                    )
                )
            else:
                run_id = state_service.record_run_start(
                    project.path.name, task_name, status="running"
                )
                run_ids[project.path.name] = run_id
                immediate_projects.append(project)
                try:
                    from hivepilot.services.notion_service import on_run_start

                    notion_page_ids[project.path.name] = on_run_start(
                        run_id=run_id, project=project.path.name, task=task_name
                    )
                except Exception:  # noqa: BLE001
                    pass
                notification_service.send_notification(
                    f"Starting {task_name} on {project.path.name}"
                )

        # Batch limiting: if dev_batch_size > 0, only dispatch the first N projects
        # and defer the rest as quota-deferred entries (picked up by daemon).
        if settings.dev_batch_size > 0 and len(immediate_projects) > settings.dev_batch_size:
            from datetime import datetime as _dt
            from datetime import timedelta
            from datetime import timezone as _tz

            from hivepilot.services.retry_service import enqueue_deferred as _enqueue_deferred

            _batch = immediate_projects[: settings.dev_batch_size]
            _remainder = immediate_projects[settings.dev_batch_size :]
            immediate_projects = _batch
            _defer_at = _dt.now(_tz.utc) + timedelta(minutes=1)
            for _dp in _remainder:
                _enqueue_deferred(
                    task=task_name,
                    projects=[_dp.path.name],
                    error="batch limit: deferred to next window",
                    next_retry_at=_defer_at,
                    context={
                        "task": task_name,
                        "extra_prompt": extra_prompt,
                        "auto_git": auto_git,
                    },
                )
                results.append(
                    RunResult(
                        _dp.path.name,
                        task_name,
                        False,
                        f"batch-deferred until {_defer_at}",
                    )
                )
                logger.info(
                    "run.batch_deferred",
                    project=_dp.path.name,
                    task=task_name,
                    batch_size=settings.dev_batch_size,
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=limit) as executor:
            future_map = {
                executor.submit(
                    self._execute_task,
                    project=project,
                    task_name=task_name,
                    task=task,
                    extra_prompt=extra_prompt,
                    auto_git=auto_git,
                    run_id=run_ids.get(project.path.name),
                    policy=run_policies.get(project.path.name),
                    simulate=simulate,
                    dry_run=dry_run,
                    prior_context=prior_context,
                ): project
                for project in immediate_projects
            }
            from hivepilot.services.quota import QuotaDeferredError

            for future in concurrent.futures.as_completed(future_map):
                project = future_map[future]
                try:
                    detail = future.result()
                    results.append(RunResult(project.path.name, task_name, True, detail))
                    if run_ids.get(project.path.name):
                        state_service.complete_run(run_ids[project.path.name], "success")
                    try:
                        from hivepilot.services.notion_service import on_run_complete

                        on_run_complete(notion_page_ids.get(project.path.name), status="success")
                    except Exception:  # noqa: BLE001
                        pass
                    notification_service.send_notification(
                        f"✅ {project.path.name}: {task_name} completed"
                    )
                except QuotaDeferredError as exc:  # noqa: BLE001
                    from datetime import datetime as _datetime
                    from datetime import timedelta, timezone

                    from hivepilot.services.retry_service import enqueue_deferred

                    _reset_at = exc.reset_at or (
                        _datetime.now(timezone.utc) + timedelta(minutes=30)
                    )
                    enqueue_deferred(
                        task=task_name,
                        projects=[project.path.name],
                        error=str(exc),
                        next_retry_at=_reset_at,
                        context={
                            "task": task_name,
                            "extra_prompt": extra_prompt,
                            "auto_git": auto_git,
                        },
                    )
                    logger.warning(
                        "run.quota_deferred",
                        project=project.path.name,
                        task=task_name,
                        reset_at=str(_reset_at),
                    )
                    results.append(
                        RunResult(
                            project.path.name,
                            task_name,
                            False,
                            f"quota-deferred until {_reset_at}",
                        )
                    )
                    if run_ids.get(project.path.name):
                        state_service.complete_run(run_ids[project.path.name], "deferred")
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "run.failure", project=project.path.name, task=task_name, error=str(exc)
                    )
                    results.append(RunResult(project.path.name, task_name, False, str(exc)))
                    if run_ids.get(project.path.name):
                        state_service.complete_run(run_ids[project.path.name], "failed", str(exc))
                    notification_service.send_notification(
                        f"❌ {project.path.name}: {task_name} failed ({exc})"
                    )
                    try:
                        from hivepilot.services.notion_service import on_run_complete

                        on_run_complete(
                            notion_page_ids.get(project.path.name), status="failed", detail=str(exc)
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        from hivepilot.services.linear_service import on_run_failure

                        on_run_failure(project=project.path.name, task=task_name, error=str(exc))
                    except Exception:  # noqa: BLE001
                        pass
        summary = {
            "task": task_name,
            "projects": [p.path.name for p in projects],
            "results": [result.__dict__ for result in results],
            "extra_prompt": extra_prompt,
        }
        write_summary(run_dir, summary)
        artifact_manager = ArtifactManager(run_dir)
        artifact_manager.write_json("results.json", summary)
        self._collect_artifacts(
            manager=artifact_manager,
            task=task,
            projects=projects,
        )
        exporters = task.artifacts.get("exporters", [])
        artifact_manager.export(exporters)
        project_lookup = {project.path.name: project for project in projects}
        for result in results:
            project_cfg = project_lookup.get(result.project)
            if not project_cfg:
                continue
            summary_text = f"{result.target} -> {'success' if result.success else 'failed'} ({result.detail or 'no detail'})"
            knowledge_service.append_feedback(project_cfg.path, result.target, summary_text)
        return results

    def _agent_name(self, stage: PipelineStage) -> str:
        """Human-facing agent name for a stage (FR theme), falling back to stage name."""
        from hivepilot.roles import ROLES

        task = self.tasks.tasks.get(stage.task)
        if task and task.role:
            role = ROLES.get(task.role)
            if role and role.display_name:
                return f"{role.display_name} ({role.title})"
        return stage.name

    def _handle_agent_requests(
        self,
        *,
        stage_output: str,
        actor: str,
        stage: "PipelineStage",
        project_names: list[str],
        policy: "policy_service.Policy | None",
        budget: dict[str, int],
        depth: int = 0,
    ) -> str:
        """Process ``REQUEST: <agent> — <question>`` lines from *stage_output*.

        Guardrails (all enforced, highest-priority first):
        - ``depth >= settings.max_request_depth`` → skip (depth cap)
        - ``budget["remaining"] <= 0`` → skip (global budget exhausted)
        - per-turn cap: honour at most ``settings.max_agent_requests`` requests
        - anti-cycle guard: skip ``(requester, target)`` pairs seen in this turn
        - unresolvable target → skip with a warning log
        - on budget exhaustion or unresolved requests → append NEEDS_HUMAN note

        Each honoured request:
        1. Streams ❓ (requester → target — question)
        2. Re-invokes target role via ``capture_definition`` (same pattern as rebuttal)
        3. Streams ↩️ (target → requester — answer excerpt)
        4. Logs request + answer interactions
        5. Appends ``[ANSWER from <target>]: <answer>`` to the returned output

        Never raises — errors are caught and logged.
        """
        from typing import cast

        from hivepilot.models import RunnerDefinition, RunnerKind, TaskStep
        from hivepilot.roles import get_role, resolve_host, resolve_runner
        from hivepilot.runners.base import RunnerPayload

        if depth >= settings.max_request_depth:
            return stage_output
        if budget["remaining"] <= 0:
            return stage_output + "\n[NEEDS_HUMAN] Agent request budget exhausted for this run."

        requests_found = parse_agent_requests(stage_output)
        if not requests_found:
            return stage_output

        # Per-turn cap
        requests_to_handle = requests_found[: settings.max_agent_requests]
        unhandled_count = len(requests_found) - len(requests_to_handle)

        # Anti-cycle guard: track (requester, target) pairs seen this turn
        seen_pairs: set[tuple[str, str]] = set()

        extra_lines: list[str] = []
        for target_display, question in requests_to_handle:
            if budget["remaining"] <= 0:
                extra_lines.append("[NEEDS_HUMAN] Agent request budget exhausted mid-turn.")
                break

            # Anti-cycle check
            pair = (actor, target_display)
            if pair in seen_pairs:
                logger.info(
                    "agent_request.cycle_skipped",
                    requester=actor,
                    target=target_display,
                )
                continue
            seen_pairs.add(pair)

            # Resolve target role key
            target_role_key = _resolve_role_from_display(target_display)
            if target_role_key is None:
                logger.info(
                    "agent_request.target_unresolvable",
                    requester=actor,
                    target=target_display,
                )
                extra_lines.append(
                    f"[NEEDS_HUMAN] Could not resolve agent '{target_display}' for request."
                )
                continue

            # Stream the request
            try:
                notification_service.stream_agent_request(
                    requester=actor,
                    target=target_display,
                    question=question,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent_request.stream_failed", error=str(exc))

            # Log the request interaction
            try:
                log_request_interaction(actor=actor, target=target_display, question=question)
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent_request.log_failed", error=str(exc))

            # Re-invoke target role via capture_definition
            answer = ""
            try:
                target_role = get_role(target_role_key)
                runner_kind, role_model = resolve_runner(target_role_key, policy)
                role_perm = target_role.permission_mode
                role_options: dict[str, str] = {}
                if role_perm:
                    role_options["permission_mode"] = role_perm
                req_runner_def = RunnerDefinition(
                    name=f"request:{target_role_key}",
                    kind=cast(RunnerKind, runner_kind),
                    command=None,
                    model=role_model,
                    host=resolve_host(target_role_key, policy),
                    options=role_options,
                )
                req_prompt_file = (
                    str(target_role.prompt_file)
                    if target_role.prompt_file and target_role.prompt_file.exists()
                    else ""
                )
                req_step = TaskStep(
                    name=f"request:{target_role_key}",
                    runner=runner_kind,
                    prompt_file=req_prompt_file,
                )
                request_prompt = (
                    f"You are {target_display}. A colleague ({actor}) has a specific question.\n\n"
                    f"QUESTION: {question}\n\n"
                    "Give a concise, factual answer (under 200 words). "
                    "Focus only on the question asked."
                )
                req_payload = RunnerPayload(
                    project_name=project_names[0] if project_names else "unknown",
                    project=None,
                    task_name=f"request:{target_role_key}",
                    step=req_step,
                    metadata={"extra_prompt": request_prompt, "prior_context": ""},
                    secrets={},
                )
                answer = self.registry.capture_definition(req_runner_def, req_payload)
                budget["remaining"] -= 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "agent_request.invoke_failed",
                    target_role=target_role_key,
                    error=str(exc),
                )
                extra_lines.append(f"[NEEDS_HUMAN] Request to '{target_display}' failed: {exc}")
                continue

            # Stream the answer
            try:
                notification_service.stream_agent_answer(
                    target=target_display,
                    requester=actor,
                    answer_excerpt=answer[:500],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent_answer.stream_failed", error=str(exc))

            # Log the answer interaction
            try:
                log_request_interaction(
                    actor=target_display, target=actor, question=f"[ANSWER] {answer[:500]}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent_answer.log_failed", error=str(exc))

            extra_lines.append(f"[ANSWER from {target_display}]: {answer}")

        if unhandled_count > 0:
            extra_lines.append(
                f"[NEEDS_HUMAN] {unhandled_count} request(s) skipped due to per-turn cap "
                f"(max_agent_requests={settings.max_agent_requests})."
            )

        if extra_lines:
            return stage_output + "\n" + "\n".join(extra_lines)
        return stage_output

    def _run_rebuttal_round(
        self,
        *,
        challenger_name: str,
        challenge_target: str,
        challenge_point: str,
        challenger_stage: PipelineStage,
        completed_stages: list[PipelineStage],
        prior_chunks: list[str],
        policy: policy_service.Policy | None,
        project_name: str,
        simulate: bool,
    ) -> None:
        """Execute one bounded rebuttal round after a ⚔️ challenge.

        Flow:
          1. Resolve target role from *challenge_target* display name.
          2. Confirm target is an upstream (already completed) stage.
          3. Build rebuttal prompt and invoke target via capture_definition.
          4. Stream 🛡️ rebuttal turn; log the interaction.
          5. Re-invoke challenger with rebuttal → "ACCEPT or MAINTAIN?".
          6. Stream ⚖️ (resolved) or 🙋 (escalated).
          7. Append resolution note to *prior_chunks* for downstream stages.

        Never raises — errors are logged and the pipeline continues with A-only
        ⚔️ visibility.
        """
        from typing import cast

        from hivepilot.models import RunnerDefinition, RunnerKind, TaskStep
        from hivepilot.roles import get_role, resolve_host, resolve_runner
        from hivepilot.runners.base import RunnerPayload

        # 1. Resolve target role key
        target_role_key = _resolve_role_from_display(challenge_target)
        if target_role_key is None:
            logger.info(
                "rebuttal.target_unresolvable",
                target=challenge_target,
                challenger=challenger_name,
            )
            return

        # 2. Confirm the target is an upstream completed stage
        target_role = get_role(target_role_key)
        # Find the completed stage whose task maps to this role
        target_stage: PipelineStage | None = None
        for s in completed_stages:
            task_cfg = self.tasks.tasks.get(s.task)
            if task_cfg and task_cfg.role == target_role_key:
                target_stage = s
                break
        if target_stage is None:
            logger.info(
                "rebuttal.target_not_upstream",
                target_role=target_role_key,
                target_display=challenge_target,
                completed=[s.task for s in completed_stages],
            )
            return

        # 3. Build rebuttal prompt for the target
        # Find the target's prior output from prior_chunks (search by agent display name)
        target_agent_name = self._agent_name(target_stage)
        prior_output = ""
        for chunk in reversed(prior_chunks):
            if target_agent_name in chunk or target_stage.name in chunk:
                prior_output = chunk
                break

        rebuttal_prompt = (
            f"You are {target_agent_name}. A colleague has challenged your previous output.\n\n"
            f"YOUR PREVIOUS OUTPUT:\n{prior_output[:2000] if prior_output else '(not available)'}\n\n"
            f"CHALLENGE from {challenger_name}:\n{challenge_point}\n\n"
            "Respond with one of:\n"
            "- ACCEPT: <brief acknowledgement and what you would change>\n"
            "- DEFEND: <clear rationale for why your original position stands>\n"
            "- ESCALATE: <brief statement of why this needs human review>\n\n"
            "Keep your response concise (under 200 words)."
        )

        # 4. Invoke target role's runner for rebuttal
        runner_kind, role_model = resolve_runner(target_role_key, policy)
        role_perm = target_role.permission_mode
        role_options: dict[str, str] = {}
        if role_perm:
            role_options["permission_mode"] = role_perm
        runner_def = RunnerDefinition(
            name=f"rebuttal:{target_role_key}",
            kind=cast(RunnerKind, runner_kind),
            command=None,
            model=role_model,
            host=resolve_host(target_role_key, policy),
            options=role_options,
        )
        prompt_file = (
            str(target_role.prompt_file)
            if target_role.prompt_file and target_role.prompt_file.exists()
            else ""
        )
        step = TaskStep(
            name=f"rebuttal:{target_role_key}",
            runner=runner_kind,
            prompt_file=prompt_file,
        )
        rebuttal_project = self._project(project_name)
        payload = RunnerPayload(
            project_name=project_name,
            project=rebuttal_project,
            task_name=f"rebuttal:{target_role_key}",
            step=step,
            metadata={"extra_prompt": rebuttal_prompt, "prior_context": ""},
            secrets={},
        )

        if simulate:
            rebuttal_output = (
                f"[simulated rebuttal from {target_agent_name}] DEFEND: My analysis stands."
            )
        else:
            rebuttal_output = self.registry.capture_definition(runner_def, payload)

        # Stream 🛡️ rebuttal turn
        notification_service.stream_rebuttal(
            actor=target_agent_name,
            target=challenger_name,
            point=rebuttal_output,
        )
        log_challenge_interaction(
            actor=target_agent_name,
            target=challenger_name,
            point=f"[REBUTTAL] {rebuttal_output}",
        )

        # 5. Re-invoke challenger with the rebuttal → resolution check
        challenger_role_key: str | None = None
        challenger_task_cfg2 = self.tasks.tasks.get(challenger_stage.task)
        if challenger_task_cfg2 and challenger_task_cfg2.role:
            challenger_role_key = challenger_task_cfg2.role

        resolution_prompt = (
            f"You are {challenger_name}. You challenged {target_agent_name} and they responded.\n\n"
            f"YOUR ORIGINAL CHALLENGE:\n{challenge_point}\n\n"
            f"THEIR REBUTTAL:\n{rebuttal_output[:2000]}\n\n"
            "Respond with exactly one of:\n"
            "- ACCEPT: <brief acknowledgement — you are satisfied with their defence>\n"
            "- MAINTAIN: <brief statement of why you still disagree — this will be escalated for human review>\n\n"
            "Keep your response concise (under 100 words)."
        )

        if challenger_role_key is None:
            # No role for challenger — default to ACCEPT to avoid blocking
            resolution_output = "ACCEPT: Unable to determine challenger role for resolution check."
        elif simulate:
            resolution_output = f"[simulated resolution from {challenger_name}] ACCEPT: Satisfied."
        else:
            ch_runner_kind, ch_role_model = resolve_runner(challenger_role_key, policy)
            ch_role = get_role(challenger_role_key)
            ch_role_perm = ch_role.permission_mode
            ch_role_options: dict[str, str] = {}
            if ch_role_perm:
                ch_role_options["permission_mode"] = ch_role_perm
            ch_runner_def = RunnerDefinition(
                name=f"resolution:{challenger_role_key}",
                kind=cast(RunnerKind, ch_runner_kind),
                command=None,
                model=ch_role_model,
                host=resolve_host(challenger_role_key, policy),
                options=ch_role_options,
            )
            ch_step = TaskStep(
                name=f"resolution:{challenger_role_key}",
                runner=ch_runner_kind,
                prompt_file=(
                    str(ch_role.prompt_file)
                    if ch_role.prompt_file and ch_role.prompt_file.exists()
                    else ""
                ),
            )
            ch_payload = RunnerPayload(
                project_name=project_name,
                project=rebuttal_project,
                task_name=f"resolution:{challenger_role_key}",
                step=ch_step,
                metadata={"extra_prompt": resolution_prompt, "prior_context": ""},
                secrets={},
            )
            resolution_output = self.registry.capture_definition(ch_runner_def, ch_payload)

        # 6. Determine outcome and stream final icon
        is_escalated = resolution_output.strip().upper().startswith("MAINTAIN")
        if is_escalated:
            notification_service.stream_needs_human(
                actor=challenger_name,
                target=target_agent_name,
                point=resolution_output,
            )
            resolution_note = (
                f"[NEEDS_HUMAN] Challenge between {challenger_name} → {target_agent_name} "
                f"unresolved. Point: {challenge_point[:200]} | "
                f"Rebuttal: {rebuttal_output[:200]} | "
                f"Challenger maintained: {resolution_output[:200]}"
            )
        else:
            notification_service.stream_resolved(
                actor=challenger_name,
                target=target_agent_name,
                resolution=resolution_output,
            )
            resolution_note = (
                f"[RESOLVED] Challenge between {challenger_name} → {target_agent_name} closed. "
                f"Point: {challenge_point[:200]} | Resolution: {resolution_output[:200]}"
            )

        log_challenge_interaction(
            actor=challenger_name,
            target=target_agent_name,
            point=f"[RESOLUTION] {resolution_output}",
        )

        # 7. Append resolution note to prior_chunks for downstream context
        prior_chunks.append(f"## Challenge Resolution\n{resolution_note}")

    def run_pipeline(
        self,
        *,
        project_names: Iterable[str],
        pipeline_name: str,
        extra_prompt: str | None,
        auto_git: bool,
        concurrency: int | None = None,
        dry_run: bool = True,
        simulate: bool = False,
        start_index: int = 0,
        run_id: int | None = None,
        hub: str | None = None,
        components: list[str] | None = None,
        seed_context: str | None = None,
        group: Group | None = None,
    ) -> list[RunResult]:
        if pipeline_name not in self.pipelines.pipelines:
            raise ValueError(f"Unknown pipeline: {pipeline_name}")
        pipeline = self.pipelines.pipelines[pipeline_name]
        validate_pipeline(pipeline, self.tasks)

        # Stage scoping (PRD A1): resolve the run's tag -> component map and
        # fail closed, up front, before any stage executes, if a stage
        # references a tag that isn't defined for this run's group. Applies
        # even when no `group` was passed (group_tags == {}): a stage that
        # declares `only_tags` without a group is an undefined-tag error too.
        group_tags: dict[str, list[str]] = group.tags if group is not None else {}
        _validate_stage_tags(pipeline.stages, group_tags)

        project_names = list(project_names)

        # Group mode: planning stages (before the checkpoint) run once in the hub;
        # execution stages (the pause_before stage onward) fan out over components.
        group_mode = bool(components)
        pause_index = next(
            (i for i, s in enumerate(pipeline.stages) if s.pause_before), len(pipeline.stages)
        )

        notification_service.stream_agent_turn(
            actor="HivePilot",
            stage=f"pipeline {pipeline_name}",
            summary=f"started on {', '.join(project_names)}",
            icon="🚀",
        )

        # Resolve vault path — None means artifact writes are silent no-ops
        vault_path = settings.obsidian_vault if settings.obsidian_vault.exists() else None

        # Open (or resume) a state-service run record (RUNNING)
        if run_id is None:
            run_id = state_service.record_run_start(
                pipeline_name,
                pipeline_name,
                status=RunStatus.RUNNING.value,
            )

        interactions_svc = InteractionService(vault_path, dry_run=dry_run)
        notification_service.emit_event(
            "pipeline_start", run_id=run_id, pipeline=pipeline_name, projects=project_names
        )

        results: list[RunResult] = []
        final_status = RunStatus.COMPLETE
        prior_chunks: list[str] = []  # outputs of completed stages, fed to later agents
        # PRD A2 Sprint 1: run-scoped keyed store (output-key -> content), populated
        # alongside prior_chunks below via section-extraction with whole-blob coarse
        # fallback. Built but NOT consumed anywhere yet — inert this sprint.
        outputs_by_key: dict[str, str] = {}
        _request_budget: dict[str, int] = {"remaining": settings.max_requests_per_run}
        if seed_context:
            prior_chunks.append(seed_context)
        elif group_mode:
            prior_chunks.append(
                "Components of this product (decide which ones this change should touch):\n"
                + "\n".join(f"- {c}" for c in (components or []))
                + "\n\nWhen you synthesize the plan, end with a line listing the impacted "
                "components exactly as: `COMPONENTS: name1, name2` (using the names above)."
            )
        selected_components = list(components or [])  # narrowed by the agents' COMPONENTS line
        for stage_idx, stage in enumerate(pipeline.stages):
            if stage_idx < start_index:
                continue  # already executed before the checkpoint pause

            # Group mode: the agents pick which components the change touches.
            if group_mode and stage_idx == pause_index:
                picked = _parse_components("\n\n".join(prior_chunks), components or [])
                if picked:
                    selected_components = picked

            # Stage scoping (PRD A1): skip this stage entirely when it declares
            # only_components/only_tags and that target is disjoint from the
            # components selected for this run. The stage's task is never
            # invoked, `prior_chunks`/artifacts/interaction-log are left
            # untouched (early continue), and the run carries on. A single
            # RunResult with skipped=True is appended so the skip is
            # distinguishable in the run record from both success and
            # failure (success=True, skipped=True — never counted as a
            # failure, never confused with a real completed stage).
            if _stage_should_skip(stage, group_tags, selected_components):
                target_components = sorted(_resolve_stage_target_components(stage, group_tags))
                logger.info(
                    "pipeline.stage_skipped",
                    pipeline=pipeline_name,
                    stage=stage.name,
                    only_components=stage.only_components,
                    only_tags=stage.only_tags,
                    selected_components=selected_components,
                )
                results.append(
                    RunResult(
                        project=", ".join(selected_components)
                        or (project_names[0] if project_names else pipeline_name),
                        target=f"{pipeline_name}:{stage.name}",
                        success=True,
                        detail=(
                            f"skipped: scoped to {target_components}, disjoint from "
                            f"selected components {selected_components}"
                        ),
                        skipped=True,
                    )
                )
                continue

            # Plan checkpoint: pause for human approval before this stage.
            # Skipped under --simulate, consistent with simulate bypassing approvals.
            if stage.pause_before and stage_idx != start_index and not simulate:
                completed = [s.name for s in pipeline.stages[:stage_idx]]
                checkpoint_meta = {
                    "kind": "pipeline_checkpoint",
                    "pipeline": pipeline_name,
                    "projects": project_names,
                    "resume_from_index": stage_idx,
                    "extra_prompt": extra_prompt,
                    "auto_git": auto_git,
                    "dry_run": dry_run,
                    "simulate": simulate,
                    "next_stage": stage.name,
                    "completed_stages": completed,
                    "hub": hub,
                    "components": selected_components,
                    "planning_context": "\n\n".join(prior_chunks) or None,
                }
                state_service.record_approval_request(
                    run_id,
                    project_names[0] if project_names else pipeline_name,
                    pipeline_name,
                    checkpoint_meta,
                )
                notification_service.send_approval_keyboard(
                    run_id=run_id,
                    project=", ".join(project_names) or pipeline_name,
                    task=f"plan → {stage.name}",
                    details=_build_checkpoint_details(
                        prior_chunks=prior_chunks,
                        completed=completed,
                        next_stage=stage.name,
                        components=selected_components,
                        group_mode=group_mode,
                    ),
                )
                proposal_excerpt = (prior_chunks[-1] if prior_chunks else "").strip()
                notification_service.stream_agent_turn(
                    actor="HivePilot",
                    stage="checkpoint",
                    summary=(
                        f"Plan ready ({', '.join(completed)}). "
                        + (
                            f"Target components: {', '.join(selected_components)}. "
                            if group_mode
                            else ""
                        )
                        + f'Approve (run #{run_id}) to start "{stage.name}". '
                        + "Full plan in the Obsidian vault."
                        + (f"\n\n{proposal_excerpt}" if proposal_excerpt else "")
                    ),
                    icon="⏸️",
                )
                notification_service.emit_event(
                    "checkpoint",
                    run_id=run_id,
                    pipeline=pipeline_name,
                    next_stage=stage.name,
                    components=selected_components if group_mode else None,
                    status="awaiting_approval",
                )
                state_service.complete_run(run_id, RunStatus.PAUSED.value)
                return results

            if group_mode:
                is_hub_stage = bool(stage_idx < pause_index and hub)
                targets = [hub] if is_hub_stage else selected_components
                # Planning stages run on the hub, which is the product/parent dir
                # (not a code git repo). Code git actions only make sense on the
                # component repos in the post-checkpoint fan-out; the hub's planning
                # artifacts are persisted via the vault auto-commit instead.
                stage_auto_git = auto_git and not is_hub_stage
            else:
                targets = project_names
                stage_auto_git = auto_git
            # PRD A2 Sprint 2: resolve the CONSUMING stage's role (the stage
            # about to run) to decide whether prior_context is routed from the
            # keyed store or built the classic way. Gated on
            # settings.context_routing_mode only — see _route_prior_context.
            from hivepilot.roles import ROLES

            consuming_task = self.tasks.tasks.get(stage.task)
            consuming_role = (
                ROLES.get(consuming_task.role) if consuming_task and consuming_task.role else None
            )
            stage_results = self.run_task(
                project_names=targets,
                task_name=stage.task,
                extra_prompt=extra_prompt,
                auto_git=stage_auto_git,
                concurrency=concurrency,
                simulate=simulate,
                dry_run=dry_run,
                prior_context=_route_prior_context(
                    role=consuming_role,
                    prior_chunks=prior_chunks,
                    outputs_by_key=outputs_by_key,
                    routing_mode=settings.context_routing_mode,
                    prior_context_mode=settings.prior_context_mode,
                    max_chars=settings.max_prior_context_chars,
                    stage_name=stage.name,
                ),
            )
            results.extend(
                [
                    RunResult(r.project, f"{pipeline_name}:{stage.name}", r.success, r.detail)
                    for r in stage_results
                ]
            )

            # Aggregate stage output for the vault artifact
            stage_output = "\n".join(
                r.detail or f"{r.project}: {'ok' if r.success else 'failed'}" for r in stage_results
            )

            # PRD A2 Sprint 1/2: populate the run-scoped keyed store alongside
            # prior_chunks. Same-key entries from a later stage overwrite earlier
            # ones (last producer wins). Consumed by _route_prior_context above
            # for the NEXT stage's turn, but ONLY in context_routing_mode="keyed"
            # (Sprint 2) — in "full" mode (default) this dict is populated but
            # never read, so the prior_chunks/build_prior_context path remains
            # byte-identical to pre-Sprint-2 behaviour.
            from hivepilot.roles import ROLES

            producing_task = self.tasks.tasks.get(stage.task)
            producing_role = (
                ROLES.get(producing_task.role) if producing_task and producing_task.role else None
            )
            if producing_role and producing_role.outputs:
                outputs_by_key.update(_stage_outputs_by_key(stage_output, producing_role.outputs))

            prior_chunks.append(f"## {self._agent_name(stage)} ({stage.name})\n{stage_output}")
            write_stage_artifact(
                vault_path=vault_path,
                run_id=run_id,
                stage_name=stage.name,
                output=stage_output,
                dry_run=dry_run,
            )

            # Per-stage interaction log (2.6a)
            next_stages = pipeline.stages[stage_idx + 1 :]
            next_target = self._agent_name(next_stages[0]) if next_stages else None
            from datetime import datetime, timezone

            interactions_svc.log_interaction(
                Interaction(
                    actor=self._agent_name(stage),
                    action="completed stage",
                    target=next_target,
                    summary=stage_output,
                    timestamp=datetime.now(tz=timezone.utc).isoformat(),
                    run_id=run_id,
                    metadata={"pipeline": pipeline_name, "stage_index": stage_idx},
                )
            )
            notification_service.stream_agent_turn(
                actor=self._agent_name(stage),
                stage=stage.name,
                target=next_target,
                summary=stage_output,
            )

            # Surface inter-agent challenges (⚔️) and bounded rebuttal (🛡️/⚖️/🙋)
            _report = parse_agent_report(stage_output)
            if _report.challenge:
                _challenger_name = self._agent_name(stage)
                _challenge_target = _report.challenge.target
                _challenge_point = _report.challenge.point
                notification_service.stream_challenge(
                    actor=_challenger_name,
                    target=_challenge_target,
                    point=_challenge_point,
                )
                if _METRICS_AVAILABLE and _metrics is not None:
                    try:
                        _metrics.challenges_total.inc()
                    except Exception:  # noqa: BLE001
                        pass
                log_challenge_interaction(
                    actor=_challenger_name,
                    target=_challenge_target,
                    point=_challenge_point,
                )

                # Bounded rebuttal round: target defends → challenger accepts/maintains
                if settings.enable_challenge_rounds and settings.max_challenge_rounds >= 1:
                    try:
                        _rebuttal_policy = policy_service.get_policy(
                            project_names[0] if project_names else pipeline_name
                        )
                        _rebuttal_project_name = (
                            project_names[0] if project_names else pipeline_name
                        )
                        self._run_rebuttal_round(
                            challenger_name=_challenger_name,
                            challenge_target=_challenge_target,
                            challenge_point=_challenge_point,
                            challenger_stage=stage,
                            completed_stages=pipeline.stages[:stage_idx],
                            prior_chunks=prior_chunks,
                            policy=_rebuttal_policy,
                            project_name=_rebuttal_project_name,
                            simulate=simulate,
                        )
                    except Exception as _rebuttal_exc:  # noqa: BLE001
                        logger.warning(
                            "rebuttal.error",
                            challenger=_challenger_name,
                            target=_challenge_target,
                            error=str(_rebuttal_exc),
                        )

            # Tier-2: on-demand agent-to-agent requests (❓/↩️)
            if settings.enable_agent_requests:
                _req_policy = policy_service.get_policy(
                    project_names[0] if project_names else pipeline_name
                )
                stage_output = self._handle_agent_requests(
                    stage_output=stage_output,
                    actor=self._agent_name(stage),
                    stage=stage,
                    project_names=list(project_names),
                    policy=_req_policy,
                    budget=_request_budget,
                )

            # Documentation vault changelog note (2.6c)
            if stage.commits_vault and vault_path is not None:
                doc_svc = ObsidianService(vault_path, dry_run=dry_run)
                doc_svc.write_note(
                    subpath=f"Docs/changelog-run-{run_id}.md",
                    title=f"Documentation update — pipeline run {run_id}",
                    body=stage_output,
                    frontmatter_fields={
                        "type": "documentation",
                        "run_id": run_id,
                        "pipeline": pipeline_name,
                        "agent": "gemini-cli",
                        "stage": stage.name,
                    },
                )

            # Commit+push the vault per stage so notes land in Obsidian as they're
            # written (not just at run end). Opt-in, best-effort.
            if (
                settings.auto_commit_vault
                and not simulate
                and not dry_run
                and vault_path is not None
            ):
                try:
                    from hivepilot.services.git_service import commit_vault

                    commit_vault(
                        vault_path, f"HivePilot: {pipeline_name} run {run_id} — {stage.name}"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "vault.commit_failed", run_id=run_id, stage=stage.name, error=str(exc)
                    )

            stage_failed = any(not r.success for r in stage_results)
            if stage_failed and not stage.continue_on_failure:
                logger.warning(
                    "pipeline.fail_fast",
                    pipeline=pipeline_name,
                    stage=stage.name,
                    remaining=[s.name for s in next_stages],
                )
                final_status = RunStatus.TEST_FAILURE
                break

        state_service.complete_run(run_id, final_status.value)
        notification_service.emit_event(
            "complete", run_id=run_id, pipeline=pipeline_name, status=final_status.value
        )

        # Version the plan/ADR notes: commit+push the Obsidian vault (best-effort,
        # opt-in, only on a real write run).
        if settings.auto_commit_vault and not simulate and not dry_run and vault_path is not None:
            try:
                from hivepilot.services.git_service import commit_vault

                commit_vault(vault_path, f"HivePilot: {pipeline_name} run {run_id}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("vault.commit_failed", run_id=run_id, error=str(exc))

        # Henri (external auditor) observes the completed cycle — best-effort, never
        # breaks a run; skipped under --simulate and when disabled.
        if settings.auditor_auto and not simulate and project_names:
            try:
                from hivepilot.services import auditor_service

                auditor_service.observe(
                    project=self._project(project_names[0]),
                    run_id=run_id,
                    registry=self.registry,
                    dry_run=dry_run,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("auditor.observe_failed", run_id=run_id, error=str(exc))
        return results

    def resume_pipeline(
        self,
        *,
        run_id: int,
        approve: bool,
        approver: str,
        auto_git: bool | None = None,
    ) -> RunResult:
        """Resume (or deny) a pipeline parked at a plan checkpoint.

        On approve, continue ``run_pipeline`` from the saved resume point under the
        same ``run_id``; on deny, mark the run denied and run nothing further.
        ``auto_git`` (when not None) overrides the run's stored auto_git — e.g. to
        enable push/PR at approval time on a run launched without ``--auto-git``.
        """
        approval = state_service.get_approval(run_id)
        if not approval or approval.get("status") != "pending":
            raise ValueError(f"Run {run_id} is not a pending checkpoint.")
        meta = json.loads(approval.get("metadata") or "{}")
        if meta.get("kind") != "pipeline_checkpoint":
            raise ValueError(f"Run {run_id} is not a pipeline checkpoint.")
        pipeline_name = meta["pipeline"]

        if not approve:
            state_service.update_approval(run_id, "denied", approver)
            state_service.complete_run(run_id, "denied", "Plan denied at checkpoint")
            notification_service.send_notification(
                f"❌ Plan #{run_id} ({pipeline_name}) denied — pipeline stopped."
            )
            notification_service.emit_event(
                "denied", run_id=run_id, pipeline=pipeline_name, approver=approver
            )
            return RunResult(pipeline_name, pipeline_name, False, "Plan denied at checkpoint")

        state_service.update_approval(run_id, "approved", approver)
        notification_service.send_notification(
            f"✅ Plan #{run_id} ({pipeline_name}) approved — starting development."
        )
        notification_service.emit_event(
            "approved", run_id=run_id, pipeline=pipeline_name, approver=approver
        )
        results = self.run_pipeline(
            project_names=meta["projects"],
            pipeline_name=pipeline_name,
            extra_prompt=meta.get("extra_prompt"),
            auto_git=auto_git if auto_git is not None else meta.get("auto_git", False),
            dry_run=meta.get("dry_run", True),
            simulate=meta.get("simulate", False),
            start_index=meta["resume_from_index"],
            run_id=run_id,
            hub=meta.get("hub"),
            components=meta.get("components"),
            seed_context=meta.get("planning_context"),
        )
        ok = all(r.success for r in results)
        return RunResult(pipeline_name, pipeline_name, ok)

    def human_challenge(self, run_id: int, challenge_text: str, approver: str) -> str:
        """Process a human challenge/question at a plan checkpoint.

        The run stays paused — caller must not call resume_pipeline.
        Returns the CoS response string.
        """
        import json as _json
        from typing import cast

        from hivepilot.models import RunnerDefinition, RunnerKind, TaskStep
        from hivepilot.roles import get_role, resolve_host, resolve_runner
        from hivepilot.runners.base import RunnerPayload

        row = state_service.get_approval(run_id)
        if not row:
            raise ValueError(f"No approval row found for run_id={run_id}")

        raw_meta = row.get("metadata") or "{}"
        meta: dict = _json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
        planning_context = meta.get("planning_context", "")
        project_name = row.get("project", "unknown")

        # Resolve CoS role (Jules)
        cos_role_key = "cos"
        try:
            cos_role = get_role(cos_role_key)
        except Exception:
            cos_role = None

        # Get policy for this project
        try:
            policy = policy_service.get_policy(project_name)
        except Exception:
            policy = None

        if cos_role is not None:
            runner_kind, role_model = resolve_runner(cos_role_key, policy)
            role_perm = cos_role.permission_mode
            role_options: dict[str, str] = {}
            if role_perm:
                role_options["permission_mode"] = role_perm
            runner_def = RunnerDefinition(
                name="human_challenge:cos",
                kind=cast(RunnerKind, runner_kind),
                command=None,
                model=role_model,
                host=resolve_host(cos_role_key, policy),
                options=role_options,
            )
            prompt_file = (
                str(cos_role.prompt_file)
                if cos_role.prompt_file and cos_role.prompt_file.exists()
                else ""
            )
            step = TaskStep(
                name="human_challenge:cos",
                runner=runner_kind,
                prompt_file=prompt_file,
            )
        else:
            # Fallback: use default claude runner
            runner_def = RunnerDefinition(
                name="human_challenge:cos",
                kind=cast(RunnerKind, "claude"),
                command=None,
                model=None,
                host=None,
                options={},
            )
            step = TaskStep(
                name="human_challenge:cos",
                runner="claude",
                prompt_file="",
            )

        challenge_prompt = (
            "You are Jules, the Chief of Staff. A human is challenging/asking about a paused"
            " pipeline plan.\n\n"
            f"CURRENT PLAN CONTEXT:\n{planning_context}\n\n"
            f"HUMAN CHALLENGE/QUESTION: {challenge_text}\n\n"
            "Respond with your analysis and any plan revisions needed."
            " Keep your response concise (under 400 words)."
        )
        payload = RunnerPayload(
            project_name=project_name,
            project=None,
            task_name="human_challenge:cos",
            step=step,
            metadata={"extra_prompt": challenge_prompt, "prior_context": ""},
            secrets={},
        )

        answer = self.registry.capture_definition(runner_def, payload)

        # Log interactions
        try:
            log_challenge_interaction(actor=approver, target="Jules (CoS)", point=challenge_text)
            log_challenge_interaction(actor="Jules (CoS)", target=approver, point=answer)
        except Exception as exc:  # noqa: BLE001
            logger.warning("human_challenge.log_failed", error=str(exc))

        # Persist challenge + response into planning_context
        meta["planning_context"] = (
            planning_context
            + f"\n\n[HUMAN CHALLENGE by {approver}]: {challenge_text}\n[JULES RESPONSE]: {answer}"
        )
        state_service.update_approval_metadata(run_id, meta)

        return answer

    def run_approved(
        self,
        *,
        run_id: int,
        approve: bool,
        approver: str,
        reason: str | None = None,
    ) -> RunResult:
        approval = state_service.get_approval(run_id)
        if not approval or approval["status"] != "pending":
            raise ValueError(f"Run {run_id} is not pending approval.")
        project_name = approval["project"]
        task_name = approval["task"]
        metadata = json.loads(approval.get("metadata") or "{}")

        if not approve:
            state_service.update_approval(run_id, "denied", approver)
            state_service.complete_run(run_id, "denied", reason or "Denied by approval workflow")
            notification_service.send_notification(
                f"❌ Run {run_id} for {project_name}:{task_name} denied by {approver}"
            )
            return RunResult(project_name, task_name, False, "Denied")

        policy = policy_service.get_policy(project_name)
        project = self._project(project_name)
        task = self.tasks.tasks[task_name]
        state_service.update_approval(run_id, "approved", approver)
        notification_service.send_notification(
            f"✅ Run {run_id} for {project_name}:{task_name} approved by {approver}."
        )
        try:
            self._execute_task(
                project=project,
                task_name=task_name,
                task=task,
                extra_prompt=metadata.get("extra_prompt"),
                auto_git=metadata.get("auto_git", False),
                run_id=run_id,
                policy=policy,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("run_approved.failure", run_id=run_id, error=str(exc))
            state_service.complete_run(run_id, "failed", str(exc))
            notification_service.send_notification(
                f"❌ Run {run_id} for {project_name}:{task_name} failed after approval ({exc})"
            )
            return RunResult(project_name, task_name, False, str(exc))
        state_service.complete_run(run_id, "success")
        return RunResult(project_name, task_name, True)

    def run_debate(
        self,
        *,
        project_name: str,
        role_name: str,
        topic: str,
        dry_run: bool = True,
        simulate: bool = False,
        prior_context: str | None = None,
    ) -> dict | None:
        """Run a dual-model debate for *role_name*: capture each model's position,
        synthesize via DebateService, and write an ADR. Returns the ADR emit dict."""
        from typing import cast

        from hivepilot.models import RunnerDefinition, RunnerKind, TaskStep
        from hivepilot.roles import get_role, resolve_host, resolve_runner
        from hivepilot.services.debate_service import DebateService, Position

        role = get_role(role_name)
        models = list(role.models or ([role.model] if role.model else []))
        if len(models) < 2:
            raise ValueError(
                f"Role '{role_name}' is not a dual-model debate role (models={models})."
            )
        project = self._project(project_name)
        policy = policy_service.get_policy(project_name)
        runner_kind, _ = resolve_runner(role_name, policy)  # also enforces allowed_runners
        debate_host = resolve_host(role_name, policy)
        vault_path = settings.obsidian_vault if settings.obsidian_vault.exists() else None

        positions: list[Position] = []
        for entry in models:
            # A brain may pin its own runner via "runner:model"
            # (e.g. "claude:claude-sonnet-4-6"); a bare model uses the role's runner.
            brain_runner, brain_model = _parse_brain(entry, runner_kind)
            step = TaskStep(
                name=f"{role_name}-{brain_model}",
                runner=brain_runner,
                prompt_file=str(role.prompt_file),
            )
            payload = RunnerPayload(
                project_name=project.path.name,
                project=project,
                task_name=f"debate:{role_name}",
                step=step,
                # The debate topic IS the brief / hand-off context for this role;
                # inject it as extra_prompt so each brain actually sees it (otherwise
                # e.g. the CEO gets no brief and correctly returns NEEDS_HUMAN).
                metadata={"extra_prompt": topic, "prior_context": prior_context or ""},
                secrets=self._resolve_secrets(step),
            )
            if simulate:
                output = f"[simulated {brain_model} position on: {topic}]"
            else:
                rdef = RunnerDefinition(
                    name=f"debate:{role_name}:{brain_model}",
                    kind=cast(RunnerKind, brain_runner),
                    command=None,
                    model=brain_model,
                    host=debate_host,
                )
                output = self.registry.capture_definition(rdef, payload)
            positions.append(
                Position(
                    role=f"{role_name}:{brain_model}",
                    stance="proposal",
                    rationale=output.strip()[:1000],
                )
            )
            notification_service.stream_agent_turn(
                actor=f"{role.display_name or role_name} ({role.title}) · {brain_model}",
                stage="debate",
                summary=output,
                icon="💬",
            )

        decision = (
            f"Synthesis of {len(models)} model proposals ({', '.join(models)}) for: {topic}. "
            f"Each model's proposal is recorded; final arbitration by {role_name} / human review."
        )
        adr = DebateService(vault_path, dry_run=dry_run).run(
            topic=topic, positions=positions, decision=decision
        )
        notification_service.stream_agent_turn(
            actor=f"{role.display_name or role_name} ({role.title})",
            stage="synthesis",
            summary=decision,
            icon="⚖️",
        )
        state_service.record_interaction(
            actor=role_name,
            action="debate",
            target=None,
            summary=topic,
            metadata={"models": models},
        )
        logger.info("debate.complete", role=role_name, models=models, project=project.path.name)
        return adr

    def interactive(self) -> None:
        project = questionary.select(
            "Select project", choices=list(self.projects.projects.keys())
        ).ask()
        if not project:
            return
        task = questionary.select("Select task", choices=list(self.tasks.tasks.keys())).ask()
        if not task:
            return
        extra = questionary.text("Extra instructions (optional)", default="").ask()
        auto_git = questionary.confirm("Run auto-git?", default=False).ask()
        self.run_task(
            project_names=[project], task_name=task, extra_prompt=extra or None, auto_git=auto_git
        )

    def _execute_task(
        self,
        *,
        project: ProjectConfig,
        task_name: str,
        task: TaskConfig,
        extra_prompt: str | None,
        auto_git: bool,
        run_id: int | None = None,
        policy: policy_service.Policy | None = None,
        simulate: bool = False,
        dry_run: bool = True,
        prior_context: str | None = None,
    ) -> str | None:
        logger.info("task.start", project=project.path.name, task=task_name)
        metadata = {"extra_prompt": extra_prompt or "", "prior_context": prior_context or ""}
        if task.engine != "native":
            from hivepilot.engines import run_engine

            placeholder_step = (
                task.steps[0]
                if task.steps
                else TaskStep(name=f"{task.engine}-engine", runner="internal")
            )
            payload = RunnerPayload(
                project_name=project.path.name,
                project=project,
                task_name=task_name,
                step=placeholder_step,
                metadata=metadata,
                secrets=self._resolve_secrets(placeholder_step),
            )
            try:
                if simulate:
                    logger.info(
                        "task.simulate.engine", project=project.path.name, engine=task.engine
                    )
                else:
                    run_engine(task=task, project=project, payload=payload)
                if run_id:
                    state_service.record_step(run_id, placeholder_step.name, "success")
            except Exception as exc:
                if run_id:
                    state_service.record_step(run_id, placeholder_step.name, "failed", str(exc))
                raise
            logger.info("task.end", project=project.path.name, task=task_name)
            return None
        if task.role:
            from hivepilot.roles import get_role as _get_role

            _role = _get_role(task.role)
            if _role.models and len(_role.models) > 1:
                topic = extra_prompt or task.description or task_name
                adr = self.run_debate(
                    project_name=project.path.name,
                    role_name=task.role,
                    topic=topic,
                    dry_run=dry_run,
                    simulate=simulate,
                    prior_context=prior_context,
                )
                if run_id:
                    state_service.record_step(run_id, f"{task.role}-debate", "success")
                logger.info("task.end", project=project.path.name, task=task_name)
                adr_path = adr.get("path") if adr else None
                return "dual-model debate → synthesis" + (f" ({adr_path})" if adr_path else "")
        # Stage cache (L3) — skip runner on cache hit
        _stage_cache = None
        _stage_cache_key_val: str | None = None
        if settings.stage_cache_enabled and not simulate and not auto_git:
            from hivepilot.services.stage_cache import get_stage_cache, stage_cache_key

            _stage_cache = get_stage_cache(settings)
            _stage_cache_key_val = stage_cache_key(
                task_name=task_name,
                model=None,  # model resolved per-runner; use None for key stability
                extra_prompt=extra_prompt,
                prior_context=prior_context,
                repo_head=self._get_repo_head(project.path),
            )
            _cached_result = _stage_cache.get(_stage_cache_key_val)
            if _cached_result is not None:
                logger.info("stage.cache_hit", task=task_name, project=project.path.name)
                return _cached_result or None

        # Determine if we should isolate this run in a git worktree
        _use_worktree = (
            settings.worktree_isolation
            and not simulate
            and auto_git
            and (task.git.commit or task.git.push)
            and self._is_git_repo(project.path)
        )

        _wt_ctx = isolated_worktree(project.path) if _use_worktree else nullcontext(project.path)

        with _wt_ctx as _exec_path:
            # Build a shallow copy of the project with the worktree path so both
            # the runner CWD and git actions operate there (branches/commits live
            # in the shared .git; the real working tree is never touched).
            _exec_project = project.model_copy(update={"path": _exec_path})

            outputs: list[str] = []
            for step in task.steps:
                secrets = self._resolve_secrets(step)
                payload = RunnerPayload(
                    project_name=_exec_project.path.name,
                    project=_exec_project,
                    task_name=task_name,
                    step=step,
                    metadata=metadata,
                    secrets=secrets,
                )
                try:
                    self.plugins.run_hook("before_step", payload=payload)
                    if task.role:
                        from typing import cast

                        from hivepilot.models import RunnerDefinition, RunnerKind
                        from hivepilot.roles import get_role, resolve_host, resolve_runner

                        runner_kind, role_model = resolve_runner(task.role, policy)
                        role_options: dict[str, str] = {}
                        role_perm = get_role(task.role).permission_mode
                        if role_perm:
                            role_options["permission_mode"] = role_perm
                        runner_def = RunnerDefinition(
                            name=f"role:{task.role}",
                            kind=cast(RunnerKind, runner_kind),
                            command=None,
                            model=role_model,
                            host=resolve_host(task.role, policy),
                            options=role_options,
                        )
                        runner_key = task.role
                    else:
                        runner_key = step.runner_ref or step.runner
                        runner_def = self.registry._definition_for(runner_key)
                    if runner_def.kind == "container" and policy and not policy.allow_containers:
                        raise RuntimeError(
                            f"Containers are disabled by policy for project {project.path.name}"
                        )
                    if simulate:
                        logger.info(
                            "step.simulate",
                            step=step.name,
                            runner=runner_key,
                            project=_exec_project.path.name,
                        )
                        outputs.append(f"[simulated {runner_key}]")
                    elif task.role:
                        from typing import cast

                        from hivepilot.models import RunnerDefinition, RunnerKind
                        from hivepilot.services.quota import parse_quota_error
                        from hivepilot.services.runner_throttle import semaphore_for_kind

                        _runner_def_to_try = runner_def
                        _fallback_runners = (
                            list(settings.dev_fallback_runners) if task.role == "developer" else []
                        )
                        _last_exc: BaseException | None = None

                        while True:
                            _sem = semaphore_for_kind(_runner_def_to_try.kind)
                            _sem.acquire()
                            try:
                                outputs.append(
                                    self.registry.capture_definition(_runner_def_to_try, payload)
                                )
                                _last_exc = None
                                break  # success
                            except Exception as _exc:
                                _last_exc = _exc
                                _quota_err = parse_quota_error(str(_exc))
                                if _quota_err is None:
                                    raise  # non-quota error → surface immediately
                                # Quota error — try fallback runners
                                if not _fallback_runners:
                                    if task.role == "developer":
                                        from hivepilot.services.quota import QuotaDeferredError

                                        raise QuotaDeferredError(
                                            str(_quota_err.raw), reset_at=_quota_err.reset_at
                                        ) from _exc
                                    raise  # non-developer roles: surface the original exception
                                _next_kind = _fallback_runners.pop(0)
                                logger.info(
                                    "dev.fallback",
                                    from_runner=_runner_def_to_try.kind,
                                    to_runner=_next_kind,
                                    reset_at=str(_quota_err.reset_at),
                                )
                                if _METRICS_AVAILABLE and _metrics is not None:
                                    try:
                                        _metrics.quota_fallbacks_total.labels(
                                            to_runner=_next_kind
                                        ).inc()
                                    except Exception:  # noqa: BLE001
                                        pass
                                _runner_def_to_try = RunnerDefinition(
                                    name=f"role:{task.role}:{_next_kind}",
                                    kind=cast(RunnerKind, _next_kind),
                                    command=None,
                                    model=role_model,
                                    host=resolve_host(task.role, policy),
                                    options=role_options,
                                )
                            finally:
                                _sem.release()

                        if _last_exc is not None:
                            raise _last_exc
                    else:
                        outputs.append(self._capture_or_execute(runner_key, payload))
                    if run_id:
                        state_service.record_step(run_id, step.name, "success")
                    self.plugins.run_hook("after_step", payload=payload)
                except Exception as exc:
                    if run_id:
                        state_service.record_step(run_id, step.name, "failed", str(exc))
                    if step.allow_failure:
                        logger.warning("step.failure_allowed", step=step.name, error=str(exc))
                        continue
                    raise

            task_result = "\n".join(o for o in outputs if o).strip() or None

            # Stage cache (L3) — store result on success
            if _stage_cache is not None and _stage_cache_key_val is not None and task_result:
                _stage_cache.put(_stage_cache_key_val, task_result)

            if auto_git:
                perform_git_actions(
                    project_name=project.path.name,  # real component name → correct branch name
                    project=_exec_project,  # worktree path → git ops run in the worktree
                    git=task.git,
                )

        logger.info("task.end", project=project.path.name, task=task_name)
        return task_result

    def _capture_or_execute(self, runner_key: str, payload: RunnerPayload) -> str:
        """Run a non-role step, capturing its stdout when the runner supports it
        (so the agent's output surfaces in the interaction log / stream)."""
        runner = self.registry.get_runner(runner_key)
        capture = getattr(runner, "capture", None)
        if capture is not None:
            return capture(payload)
        runner.run(payload)
        return ""

    @staticmethod
    def _get_repo_head(path: Path) -> str | None:
        """Return the current git HEAD sha for a repo path, or None on error."""
        try:
            result = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:  # noqa: BLE001
            pass
        return None

    @staticmethod
    def _is_git_repo(path: Path) -> bool:
        """Return True if *path* is inside a git repository."""
        try:
            result = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--git-dir"],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def _project(self, name: str) -> ProjectConfig:
        if name not in self.projects.projects:
            raise ValueError(f"Unknown project: {name}")
        return self.projects.projects[name]

    def _resolve_secrets(self, step: TaskStep) -> dict[str, str]:
        if not step.secrets:
            return {}
        return secret_resolver.resolve(step.secrets)

    def _collect_artifacts(
        self,
        *,
        manager: ArtifactManager,
        task: TaskConfig,
        projects: list[ProjectConfig],
    ) -> None:
        capture = task.artifacts.get("capture", ["diff"])
        if "diff" in capture:
            for project in projects:
                diff = self._git_diff(project.path)
                if diff:
                    manager.write_file(f"diffs/{project.path.name}.patch", diff)

    @staticmethod
    def _git_diff(path: Path) -> str | None:
        result = subprocess.run(
            ["git", "diff"],
            cwd=str(path),
            text=True,
            capture_output=True,
            check=False,
        )
        return result.stdout if result.stdout.strip() else None
