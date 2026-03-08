from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.panel import Panel
from rich.table import Table

from hivepilot.console import console
from hivepilot.github import perform_git_actions
from hivepilot.models import ProjectConfig, TaskConfig, TaskStep
from hivepilot.prompts import build_prompt
from hivepilot.shell import CommandError, ensure_command_available, run_command
from hivepilot.utils import expand_path


@dataclass(slots=True)
class RunOptions:
    extra_prompt: str | None = None
    dry_run: bool = False
    auto_git: bool = False
    prompts_dir: Path = Path("prompts")
    claude_command: str = "claude"
    gh_command: str = "gh"
    git_command: str = "git"
    default_model: str | None = None


class TaskRunner:
    def __init__(self, options: RunOptions) -> None:
        self.options = options

    def run(self, *, project_name: str, project: ProjectConfig, task_name: str, task: TaskConfig) -> None:
        project.path = expand_path(project.path)
        project_path = project.path
        if not project_path.exists():
            raise FileNotFoundError(f"Project path does not exist: {project_path}")

        self._print_header(project_name, project, task_name, task)

        for index, step in enumerate(task.steps, start=1):
            self._run_step(
                project_name=project_name,
                project=project,
                task_name=task_name,
                step=step,
                index=index,
                total=len(task.steps),
            )

        if self.options.auto_git:
            perform_git_actions(
                project_name=project_name,
                task_name=task_name,
                project=project,
                git=task.git,
                git_command=self.options.git_command,
                gh_command=self.options.gh_command,
                dry_run=self.options.dry_run,
            )

    def _run_step(
        self,
        *,
        project_name: str,
        project: ProjectConfig,
        task_name: str,
        step: TaskStep,
        index: int,
        total: int,
    ) -> None:
        console.print(Panel.fit(f"Step {index}/{total}: {step.name}", style="cyan"))
        try:
            if step.runner == "claude":
                self._run_claude_step(project_name, project, task_name, step)
            else:
                self._run_shell_step(project, step)
        except Exception:
            if step.allow_failure:
                console.print(f"Step failed but allow_failure=true: {step.name}", style="yellow")
                return
            raise

    def _run_claude_step(
        self,
        project_name: str,
        project: ProjectConfig,
        task_name: str,
        step: TaskStep,
    ) -> None:
        ensure_command_available(self.options.claude_command)
        prompt_file = self.options.prompts_dir / (step.prompt_file or "")
        prompt = build_prompt(
            prompt_path=prompt_file,
            project_name=project_name,
            project=project,
            task_name=task_name,
            step=step,
            extra_prompt=self.options.extra_prompt,
        )

        command = [self.options.claude_command]
        model = step.model or self.options.default_model
        if model:
            command.extend(["--model", model])
        command.append(prompt)

        result = run_command(
            command=command,
            cwd=project.path,
            env=project.env,
            timeout_seconds=step.timeout_seconds,
            dry_run=self.options.dry_run,
        )
        if result.returncode != 0:
            raise CommandError(f"Claude step failed: {step.name}")

    def _run_shell_step(self, project: ProjectConfig, step: TaskStep) -> None:
        if not step.command:
            raise ValueError(f"Shell step missing command: {step.name}")
        result = run_command(
            command=["bash", "-lc", step.command],
            cwd=project.path,
            env=project.env,
            timeout_seconds=step.timeout_seconds,
            dry_run=self.options.dry_run,
        )
        if result.returncode != 0:
            raise CommandError(f"Shell step failed: {step.name}")

    @staticmethod
    def _print_header(project_name: str, project: ProjectConfig, task_name: str, task: TaskConfig) -> None:
        table = Table(show_header=False, box=None)
        table.add_row("Project", project_name)
        table.add_row("Path", str(project.path))
        table.add_row("Task", task_name)
        table.add_row("Description", task.description)
        if project.description:
            table.add_row("Project info", project.description)
        console.print(Panel(table, title="Run Summary", border_style="green"))
