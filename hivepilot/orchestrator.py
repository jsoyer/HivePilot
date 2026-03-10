from __future__ import annotations

import concurrent.futures
import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import questionary

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, TaskConfig, TaskStep
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
from hivepilot.services.pipeline_service import validate_pipeline
from hivepilot.services.project_service import load_pipelines, load_projects, load_tasks
from hivepilot.services.secrets_service import secret_resolver
from hivepilot.utils.io import create_run_directory, write_summary
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


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
        immediate_projects: list[ProjectConfig] = []
        for project in projects:
            policy = policy_service.enforce_policy(project.path.name, auto_git=auto_git)
            run_policies[project.path.name] = policy
            if policy.require_approval:
                run_id = state_service.record_run_start(project.path.name, task_name, status="pending")
                approval_meta = {
                    "task": task_name,
                    "project": project.path.name,
                    "extra_prompt": extra_prompt,
                    "auto_git": auto_git,
                }
                state_service.record_approval_request(run_id, project.path.name, task_name, approval_meta)
                notification_service.send_approval_keyboard(
                    run_id=run_id, project=project.path.name, task=task_name
                )
                results.append(RunResult(project.path.name, task_name, False, f"Pending approval (run {run_id})"))
            else:
                run_id = state_service.record_run_start(project.path.name, task_name, status="running")
                run_ids[project.path.name] = run_id
                immediate_projects.append(project)
                notification_service.send_notification(f"Starting {task_name} on {project.path.name}")

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
                ): project
                for project in immediate_projects
            }
            for future in concurrent.futures.as_completed(future_map):
                project = future_map[future]
                try:
                    future.result()
                    results.append(RunResult(project.path.name, task_name, True))
                    if run_ids.get(project.path.name):
                        state_service.complete_run(run_ids[project.path.name], "success")
                    notification_service.send_notification(f"✅ {project.path.name}: {task_name} completed")
                except Exception as exc:  # noqa: BLE001
                    logger.error("run.failure", project=project.path.name, task=task_name, error=str(exc))
                    results.append(RunResult(project.path.name, task_name, False, str(exc)))
                    if run_ids.get(project.path.name):
                        state_service.complete_run(run_ids[project.path.name], "failed", str(exc))
                    notification_service.send_notification(f"❌ {project.path.name}: {task_name} failed ({exc})")
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

    def run_pipeline(
        self,
        *,
        project_names: Iterable[str],
        pipeline_name: str,
        extra_prompt: str | None,
        auto_git: bool,
        concurrency: int | None = None,
    ) -> list[RunResult]:
        if pipeline_name not in self.pipelines.pipelines:
            raise ValueError(f"Unknown pipeline: {pipeline_name}")
        pipeline = self.pipelines.pipelines[pipeline_name]
        validate_pipeline(pipeline, self.tasks)
        results: list[RunResult] = []
        for stage in pipeline.stages:
            stage_results = self.run_task(
                project_names=project_names,
                task_name=stage.task,
                extra_prompt=extra_prompt,
                auto_git=auto_git,
                concurrency=concurrency,
            )
            results.extend([RunResult(r.project, f"{pipeline_name}:{stage.name}", r.success, r.detail) for r in stage_results])
            stage_failed = any(not r.success for r in stage_results)
            if stage_failed and not stage.continue_on_failure:
                logger.warning(
                    "pipeline.fail_fast",
                    pipeline=pipeline_name,
                    stage=stage.name,
                    remaining=[s.name for s in pipeline.stages[pipeline.stages.index(stage) + 1:]],
                )
                break
        return results

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

    def interactive(self) -> None:
        project = questionary.select("Select project", choices=list(self.projects.projects.keys())).ask()
        if not project:
            return
        task = questionary.select("Select task", choices=list(self.tasks.tasks.keys())).ask()
        if not task:
            return
        extra = questionary.text("Extra instructions (optional)", default="").ask()
        auto_git = questionary.confirm("Run auto-git?", default=False).ask()
        self.run_task(project_names=[project], task_name=task, extra_prompt=extra or None, auto_git=auto_git)

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
    ) -> None:
        logger.info("task.start", project=project.path.name, task=task_name)
        metadata = {"extra_prompt": extra_prompt or ""}
        if task.engine != "native":
            from hivepilot.engines import run_engine

            placeholder_step = task.steps[0] if task.steps else TaskStep(name=f"{task.engine}-engine", runner="internal")
            payload = RunnerPayload(
                project_name=project.path.name,
                project=project,
                task_name=task_name,
                step=placeholder_step,
                metadata=metadata,
                secrets=self._resolve_secrets(placeholder_step),
            )
            try:
                run_engine(task=task, project=project, payload=payload)
                if run_id:
                    state_service.record_step(run_id, placeholder_step.name, "success")
            except Exception as exc:
                if run_id:
                    state_service.record_step(run_id, placeholder_step.name, "failed", str(exc))
                raise
            logger.info("task.end", project=project.path.name, task=task_name)
            return
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
                runner_key = step.runner_ref or step.runner
                runner_def = self.registry._definition_for(runner_key)
                if runner_def.kind == "container" and policy and not policy.allow_containers:
                    raise RuntimeError(f"Containers are disabled by policy for project {project.path.name}")
                self.registry.execute(runner_key, payload)
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
