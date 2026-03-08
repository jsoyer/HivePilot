from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from typing import Iterable, Optional

import questionary

from hivepilot.config import settings
from hivepilot.models import PipelineConfig, ProjectConfig, TaskConfig, TaskStep
from hivepilot.registry import RunnerRegistry
from hivepilot.runners.base import RunnerPayload
from hivepilot.services.git_service import perform_git_actions
from hivepilot.services.pipeline_service import validate_pipeline
from hivepilot.services.project_service import load_pipelines, load_projects, load_tasks
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
        self.projects = load_projects()
        self.tasks = load_tasks()
        self.pipelines = load_pipelines()
        self.registry = RunnerRegistry(self.tasks.runners)

    def refresh(self) -> None:
        self.__init__()

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
        with concurrent.futures.ThreadPoolExecutor(max_workers=limit) as executor:
            future_map = {
                executor.submit(
                    self._execute_task,
                    project=project,
                    task_name=task_name,
                    task=task,
                    extra_prompt=extra_prompt,
                    auto_git=auto_git,
                ): project
                for project in projects
            }
            for future in concurrent.futures.as_completed(future_map):
                project = future_map[future]
                try:
                    future.result()
                    results.append(RunResult(project.path.name, task_name, True))
                except Exception as exc:  # noqa: BLE001
                    logger.error("run.failure", project=project.path.name, task=task_name, error=str(exc))
                    results.append(RunResult(project.path.name, task_name, False, str(exc)))
        write_summary(
            run_dir,
            {
                "task": task_name,
                "projects": [p.path.name for p in projects],
                "results": [result.__dict__ for result in results],
            },
        )
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
        return results

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
            )
            run_engine(task=task, project=project, payload=payload)
            logger.info("task.end", project=project.path.name, task=task_name)
            return
        for step in task.steps:
            payload = RunnerPayload(
                project_name=project.path.name,
                project=project,
                task_name=task_name,
                step=step,
                metadata=metadata,
            )
            try:
                runner_key = step.runner_ref or step.runner
                self.registry.execute(runner_key, payload)
            except Exception as exc:
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
