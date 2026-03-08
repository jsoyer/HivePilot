from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import typer

from hivepilot.config import settings
from hivepilot.orchestrator import Orchestrator
from hivepilot.services.github_service import create_issue, create_release, ensure_repository
from hivepilot.services.project_service import load_projects, load_tasks
from hivepilot.utils.logging import get_logger

app = typer.Typer(help="HivePilot advanced orchestrator")
gh_app = typer.Typer(help="GitHub helpers")
app.add_typer(gh_app, name="gh")
logger = get_logger(__name__)


def _resolve_projects(project: str, extras: List[str], run_all: bool) -> list[str]:
    projects = load_projects()
    if run_all:
        return list(projects.projects.keys())
    names = [project, *extras]
    seen = set()
    ordered = []
    for name in names:
        if name not in projects.projects:
            raise typer.BadParameter(f"Unknown project: {name}")
        if name not in seen:
            ordered.append(name)
            seen.add(name)
    return ordered


@app.command()
def list_projects(projects_file: Path = typer.Option(settings.projects_file, help="Path to projects.yaml")) -> None:
    projects = load_projects(projects_file)
    for name, project in projects.projects.items():
        typer.echo(f"- {name}: {project.path} ({project.description or 'n/a'})")


@app.command()
def list_tasks(tasks_file: Path = typer.Option(settings.tasks_file, help="Path to tasks.yaml")) -> None:
    tasks = load_tasks(tasks_file)
    for name, task in tasks.tasks.items():
        typer.echo(f"- {name}: {task.description} [{len(task.steps)} steps]")


@app.command("list-pipelines")
def list_pipelines(pipelines_file: Path = typer.Option(settings.pipelines_file, help="Path to pipelines.yaml")) -> None:
    from hivepilot.services.project_service import load_pipelines

    pipelines = load_pipelines(pipelines_file)
    for name, pipeline in pipelines.pipelines.items():
        typer.echo(f"- {name}: {pipeline.description} ({len(pipeline.stages)} stages)")


@app.command()
def run(
    project: str = typer.Argument(..., help="Primary project"),
    task: str = typer.Argument(..., help="Task name"),
    extra_prompt: Optional[str] = typer.Option(None, "--extra-prompt", "-e", help="Extra instructions"),
    auto_git: bool = typer.Option(False, "--auto-git", help="Run post-task git actions"),
    all_projects: bool = typer.Option(False, "--all", help="Run on every configured project"),
    projects: List[str] = typer.Option([], "--project", "-p", help="Additional projects"),
    concurrency: Optional[int] = typer.Option(None, "--concurrency", "-c", help="Parallel workers"),
) -> None:
    orchestrator = Orchestrator()
    target_projects = _resolve_projects(project, projects, all_projects)
    results = orchestrator.run_task(
        project_names=target_projects,
        task_name=task,
        extra_prompt=extra_prompt,
        auto_git=auto_git,
        concurrency=concurrency,
    )
    for result in results:
        status = "✅" if result.success else "❌"
        typer.echo(f"{status} {result.project} -> {result.target}")
        if result.detail:
            typer.echo(f"   {result.detail}")


@app.command("run-pipeline")
def run_pipeline(
    project: str = typer.Argument(..., help="Primary project"),
    pipeline: str = typer.Argument(..., help="Pipeline name"),
    extra_prompt: Optional[str] = typer.Option(None, "--extra-prompt", "-e", help="Extra instructions"),
    auto_git: bool = typer.Option(False, "--auto-git", help="Run post-task git actions"),
    all_projects: bool = typer.Option(False, "--all", help="Run on every configured project"),
    projects: List[str] = typer.Option([], "--project", "-p", help="Additional projects"),
    concurrency: Optional[int] = typer.Option(None, "--concurrency", "-c", help="Parallel workers"),
) -> None:
    orchestrator = Orchestrator()
    target_projects = _resolve_projects(project, projects, all_projects)
    results = orchestrator.run_pipeline(
        project_names=target_projects,
        pipeline_name=pipeline,
        extra_prompt=extra_prompt,
        auto_git=auto_git,
        concurrency=concurrency,
    )
    for result in results:
        status = "✅" if result.success else "❌"
        typer.echo(f"{status} {result.project} -> {result.target}")


@app.command()
def interactive() -> None:
    orchestrator = Orchestrator()
    orchestrator.interactive()


@app.command()
def doctor() -> None:
    typer.echo(f"Base dir: {settings.base_dir}")
    typer.echo(f"Projects file: {settings.projects_file}")
    typer.echo(f"Tasks file: {settings.tasks_file}")
    typer.echo(f"Pipelines file: {settings.pipelines_file}")
    typer.echo(f"Prompts dir: {settings.prompts_dir}")
    typer.echo(f"Runs dir: {settings.runs_dir}")


@app.command()
def dashboard() -> None:
    """Minimal Textual dashboard listing recent runs."""
    if not settings.enable_textual_ui:
        typer.echo("Enable HIVEPILOT_ENABLE_TEXTUAL_UI to launch the dashboard.")
        raise typer.Exit(1)
    try:
        from textual.app import App
        from textual.widgets import Header, Footer, DataTable
    except ImportError as exc:  # pragma: no cover
        raise typer.BadParameter("textual not installed. run `pip install textual`.") from exc

    run_dir = settings.resolve_path(settings.runs_dir)
    rows = []
    if run_dir.exists():
        for path in sorted(run_dir.iterdir(), reverse=True)[:50]:
            rows.append((path.name, (path / "summary.json").exists()))

    class RunDashboard(App):
        def compose(self):
            yield Header()
            table = DataTable(id="runs")
            table.add_columns("Run", "Summary")
            for run, has_summary in rows:
                table.add_row(run, "✅" if has_summary else "—")
            yield table
            yield Footer()

    RunDashboard().run()


@gh_app.command("repo-init")
def gh_repo_init(
    project: str = typer.Argument(..., help="Project key"),
    push: bool = typer.Option(True, "--push/--no-push", help="Push default branch after linking repo"),
) -> None:
    orchestrator = Orchestrator()
    project_cfg = orchestrator._project(project)  # pylint: disable=protected-access
    ensure_repository(project_cfg, settings, push=push)
    typer.echo(f"Repository ready for {project}")


@gh_app.command("issue")
def gh_issue(
    project: str = typer.Argument(..., help="Project key"),
    title: str = typer.Argument(..., help="Issue title"),
    body: Optional[str] = typer.Option(None, "--body", help="Issue body"),
) -> None:
    orchestrator = Orchestrator()
    project_cfg = orchestrator._project(project)  # pylint: disable=protected-access
    create_issue(project=project_cfg, settings=settings, title=title, body=body, labels=[])
    typer.echo("Issue created.")


@gh_app.command("release")
def gh_release(
    project: str = typer.Argument(..., help="Project key"),
    tag: str = typer.Argument(..., help="Release tag"),
    title: Optional[str] = typer.Option(None, "--title", help="Release title"),
) -> None:
    orchestrator = Orchestrator()
    project_cfg = orchestrator._project(project)  # pylint: disable=protected-access
    create_release(project=project_cfg, settings=settings, tag=tag, title=title)
    typer.echo("Release created.")


if __name__ == "__main__":
    app()
