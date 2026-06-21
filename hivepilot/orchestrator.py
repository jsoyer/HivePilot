from __future__ import annotations

import concurrent.futures
import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import questionary

from hivepilot.config import settings
from hivepilot.models import PipelineStage, ProjectConfig, TaskConfig, TaskStep
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
from hivepilot.services.artifact_service import ArtifactManager
from hivepilot.services.git_service import perform_git_actions
from hivepilot.services.interaction_service import Interaction, InteractionService
from hivepilot.services.obsidian_service import ObsidianService
from hivepilot.services.pipeline_service import validate_pipeline
from hivepilot.services.project_service import load_pipelines, load_projects, load_tasks
from hivepilot.services.secrets_service import secret_resolver
from hivepilot.services.state_service import RunStatus
from hivepilot.utils.io import create_run_directory, write_summary
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


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
    ``RunnerKind`` prefix is treated as a runner, so ``"opencode-go/kimi"`` and
    other slash-style ids stay plain models.
    """
    from typing import get_args

    from hivepilot.models import RunnerKind

    if ":" in entry:
        prefix, rest = entry.split(":", 1)
        if prefix in set(get_args(RunnerKind)):
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


@dataclass
class RunResult:
    project: str
    target: str
    success: bool
    detail: str | None = None


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
    ) -> list[RunResult]:
        if pipeline_name not in self.pipelines.pipelines:
            raise ValueError(f"Unknown pipeline: {pipeline_name}")
        pipeline = self.pipelines.pipelines[pipeline_name]
        validate_pipeline(pipeline, self.tasks)

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

        results: list[RunResult] = []
        final_status = RunStatus.COMPLETE
        prior_chunks: list[str] = []  # outputs of completed stages, fed to later agents
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
                )
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
                        + f'Approve (run #{run_id}) to start "{stage.name}".'
                    ),
                    icon="⏸️",
                )
                state_service.complete_run(run_id, RunStatus.PAUSED.value)
                return results

            if group_mode:
                targets = [hub] if (stage_idx < pause_index and hub) else selected_components
            else:
                targets = project_names
            stage_results = self.run_task(
                project_names=targets,
                task_name=stage.task,
                extra_prompt=extra_prompt,
                auto_git=auto_git,
                concurrency=concurrency,
                simulate=simulate,
                dry_run=dry_run,
                prior_context="\n\n".join(prior_chunks) or None,
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

            # Documentation vault changelog note (2.6c)
            if stage.task == "company-documentation" and vault_path is not None:
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

            stage_failed = any(not r.success for r in stage_results)
            if stage_failed and not getattr(stage, "continue_on_failure", False):
                logger.warning(
                    "pipeline.fail_fast",
                    pipeline=pipeline_name,
                    stage=stage.name,
                    remaining=[s.name for s in next_stages],
                )
                final_status = RunStatus.TEST_FAILURE
                break

        state_service.complete_run(run_id, final_status.value)

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
    ) -> RunResult:
        """Resume (or deny) a pipeline parked at a plan checkpoint.

        On approve, continue ``run_pipeline`` from the saved resume point under the
        same ``run_id``; on deny, mark the run denied and run nothing further.
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
            return RunResult(pipeline_name, pipeline_name, False, "Plan denied at checkpoint")

        state_service.update_approval(run_id, "approved", approver)
        notification_service.send_notification(
            f"✅ Plan #{run_id} ({pipeline_name}) approved — starting development."
        )
        results = self.run_pipeline(
            project_names=meta["projects"],
            pipeline_name=pipeline_name,
            extra_prompt=meta.get("extra_prompt"),
            auto_git=meta.get("auto_git", False),
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
                metadata={"prior_context": prior_context or ""},
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
        outputs: list[str] = []
        for step in task.steps:
            secrets = self._resolve_secrets(step)
            payload = RunnerPayload(
                project_name=project.path.name,
                project=project,
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
                    from hivepilot.roles import resolve_host, resolve_runner

                    runner_kind, role_model = resolve_runner(task.role, policy)
                    runner_def = RunnerDefinition(
                        name=f"role:{task.role}",
                        kind=cast(RunnerKind, runner_kind),
                        command=None,
                        model=role_model,
                        host=resolve_host(task.role, policy),
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
                        project=project.path.name,
                    )
                    outputs.append(f"[simulated {runner_key}]")
                elif task.role:
                    outputs.append(self.registry.capture_definition(runner_def, payload))
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
        if auto_git:
            perform_git_actions(project_name=project.path.name, project=project, git=task.git)
        logger.info("task.end", project=project.path.name, task=task_name)
        return "\n".join(o for o in outputs if o).strip() or None

    def _capture_or_execute(self, runner_key: str, payload: RunnerPayload) -> str:
        """Run a non-role step, capturing its stdout when the runner supports it
        (so the agent's output surfaces in the interaction log / stream)."""
        runner = self.registry.get_runner(runner_key)
        capture = getattr(runner, "capture", None)
        if capture is not None:
            return capture(payload)
        runner.run(payload)
        return ""

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
