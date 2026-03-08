from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from hivepilot.config import ConfigError, load_projects, load_tasks
from hivepilot.console import console, error_console
from hivepilot.runner import RunOptions, TaskRunner
from hivepilot.settings import settings
from hivepilot.utils import expand_path

app = typer.Typer(
    help="Personal multi-project orchestrator for Claude Code workflows.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)



def _resolve(path: Path) -> Path:
    return expand_path(settings.base_dir / path)



def _load_all(projects_file: Path, tasks_file: Path):
    projects = load_projects(projects_file)
    tasks = load_tasks(tasks_file)
    return projects, tasks


@app.command("list-projects")
def list_projects(
    projects_file: Path = typer.Option(settings.projects_file, help="Path to projects.yaml"),
) -> None:
    """List configured projects."""
    try:
        projects = load_projects(_resolve(projects_file))
    except ConfigError as exc:
        error_console.print(str(exc), style="bold red")
        raise typer.Exit(code=1) from exc

    table = Table(title="Projects")
    table.add_column("Name")
    table.add_column("Path")
    table.add_column("Default branch")
    table.add_column("Description")
    for name, project in projects.projects.items():
        table.add_row(name, str(project.path), project.default_branch, project.description or "")
    console.print(table)


@app.command("list-tasks")
def list_tasks(tasks_file: Path = typer.Option(settings.tasks_file, help="Path to tasks.yaml")) -> None:
    """List configured tasks."""
    try:
        tasks = load_tasks(_resolve(tasks_file))
    except ConfigError as exc:
        error_console.print(str(exc), style="bold red")
        raise typer.Exit(code=1) from exc

    table = Table(title="Tasks")
    table.add_column("Name")
    table.add_column("Description")
    table.add_column("Steps")
    for name, task in tasks.tasks.items():
        step_names = ", ".join(step.name for step in task.steps)
        table.add_row(name, task.description, step_names)
    console.print(table)


@app.command()
def run(
    project: str = typer.Argument(..., help="Project key from projects.yaml"),
    task: str = typer.Argument(..., help="Task key from tasks.yaml"),
    extra_prompt: Optional[str] = typer.Option(None, "--extra-prompt", "-e", help="Extra instructions for this run"),
    auto_git: bool = typer.Option(False, help="Run post-task git/gh actions configured in tasks.yaml"),
    dry_run: bool = typer.Option(False, help="Print commands without executing them"),
    projects_file: Path = typer.Option(settings.projects_file, help="Path to projects.yaml"),
    tasks_file: Path = typer.Option(settings.tasks_file, help="Path to tasks.yaml"),
    prompts_dir: Path = typer.Option(settings.prompts_dir, help="Directory containing prompt templates"),
) -> None:
    """Run a task on a configured project."""
    try:
        projects, tasks = _load_all(_resolve(projects_file), _resolve(tasks_file))
    except ConfigError as exc:
        error_console.print(str(exc), style="bold red")
        raise typer.Exit(code=1) from exc

    if project not in projects.projects:
        error_console.print(f"Unknown project: {project}", style="bold red")
        raise typer.Exit(code=1)
    if task not in tasks.tasks:
        error_console.print(f"Unknown task: {task}", style="bold red")
        raise typer.Exit(code=1)

    runner = TaskRunner(
        RunOptions(
            extra_prompt=extra_prompt,
            dry_run=dry_run,
            auto_git=auto_git,
            prompts_dir=_resolve(prompts_dir),
            claude_command=settings.claude_command,
            gh_command=settings.gh_command,
            git_command=settings.git_command,
            default_model=settings.default_model,
        )
    )

    try:
        runner.run(
            project_name=project,
            project=projects.projects[project],
            task_name=task,
            task=tasks.tasks[task],
        )
    except Exception as exc:
        error_console.print(f"Run failed: {exc}", style="bold red")
        raise typer.Exit(code=1) from exc


@app.command("doctor")
def doctor() -> None:
    """Print the active configuration paths."""
    table = Table(title="Configuration")
    table.add_column("Setting")
    table.add_column("Value")
    table.add_row("base_dir", str(settings.base_dir))
    table.add_row("projects_file", str(_resolve(settings.projects_file)))
    table.add_row("tasks_file", str(_resolve(settings.tasks_file)))
    table.add_row("prompts_dir", str(_resolve(settings.prompts_dir)))
    table.add_row("claude_command", settings.claude_command)
    table.add_row("gh_command", settings.gh_command)
    table.add_row("git_command", settings.git_command)
    table.add_row("default_model", settings.default_model or "<unset>")
    console.print(table)


if __name__ == "__main__":
    app()
