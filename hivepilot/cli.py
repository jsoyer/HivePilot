from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

import typer

from hivepilot.config import settings
from hivepilot.orchestrator import Orchestrator
from hivepilot.services import state_service, token_service
from hivepilot.services.github_service import create_issue, create_release, ensure_repository
from hivepilot.services.project_service import load_projects, load_tasks
from hivepilot.utils.logging import get_logger

app = typer.Typer(help="HivePilot advanced orchestrator")
gh_app = typer.Typer(help="GitHub helpers")
app.add_typer(gh_app, name="gh")
approvals_app = typer.Typer(help="Approval queue")
app.add_typer(approvals_app, name="approvals")
api_app = typer.Typer(help="Workspace API server")
app.add_typer(api_app, name="api")
schedule_app = typer.Typer(help="Scheduler commands")
app.add_typer(schedule_app, name="schedule")
tokens_app = typer.Typer(help="Manage API tokens")
app.add_typer(tokens_app, name="tokens")
logger = get_logger(__name__)


def _get_token_value(token: Optional[str]) -> str:
    value = token or os.environ.get("HIVEPILOT_API_TOKEN")
    if not value:
        raise typer.BadParameter("Token required. Pass --token or set HIVEPILOT_API_TOKEN.")
    return value


def _require_cli_role(required: str, token: Optional[str]) -> token_service.TokenEntry:
    token_value = _get_token_value(token)
    entry = token_service.resolve_token(token_value)
    if not entry:
        raise typer.BadParameter("Invalid token")
    if token_service.role_rank(entry.role) < token_service.role_rank(required):
        raise typer.BadParameter(f"Token role '{entry.role}' lacks permission '{required}'")
    return entry


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


@app.command("discover")
def discover(
    roots: List[Path] = typer.Option([], "--root", "-r", help="Root directories to scan (repeatable)"),
    include_hidden: bool = typer.Option(False, "--include-hidden", help="Scan dot-directories"),
    max_depth: int = typer.Option(3, "--max-depth", help="Max directory depth"),
    github_org: Optional[str] = typer.Option(None, "--github-org", help="GitHub organization to scan"),
) -> None:
    """Discover local or GitHub projects and print project config entries."""
    from hivepilot.services.discovery_service import discover_local_projects, discover_github_repos

    if not roots and not github_org:
        typer.echo("Specify at least one --root or --github-org.")
        raise typer.Exit(1)

    discovered = []
    if roots:
        discovered.extend(
            discover_local_projects(roots, include_hidden=include_hidden, max_depth=max_depth)
        )
    if github_org:
        discovered.extend(discover_github_repos(org=github_org))

    if not discovered:
        typer.echo("Nothing discovered.")
        return

    typer.echo("Discovered projects:\n")
    for project in discovered:
        typer.echo(
            f"""projects:
  {project.path.name}:
    path: {project.path}
    description: {project.description or 'auto-discovered'}
    default_branch: {project.default_branch}
    owner_repo: {project.owner_repo or 'your-user/your-repo'}
"""
        )


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
    token: Optional[str] = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    _require_cli_role("run", token)
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
    token: Optional[str] = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    _require_cli_role("run", token)
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
        from hivepilot.ui.dashboard import RunDashboard
    except ImportError as exc:  # pragma: no cover
        raise typer.BadParameter("textual not installed. run `pip install textual`.") from exc

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


@api_app.command("serve")
def serve_api(
    host: str = typer.Option(settings.api_host, "--host"),
    port: int = typer.Option(settings.api_port, "--port"),
) -> None:
    """Launch the FastAPI server for remote control."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise typer.BadParameter("uvicorn not installed. Install hivepilot[full].") from exc
    from hivepilot.services.api_service import app as fastapi_app

    typer.echo(f"Serving HivePilot API on http://{host}:{port}")
    uvicorn.run(fastapi_app, host=host, port=port)


@schedule_app.command("list")
def schedule_list(
    token: Optional[str] = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    _require_cli_role("read", token)
    from hivepilot.services.schedule_service import load_schedules

    schedules = load_schedules()
    for name, entry in schedules.items():
        status = "enabled" if entry.enabled else "disabled"
        typer.echo(f"- {name}: task={entry.task} projects={entry.projects} interval={entry.interval_minutes}m ({status})")


@schedule_app.command("run")
def schedule_run(
    token: Optional[str] = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    _require_cli_role("run", token)
    from hivepilot.services import schedule_service

    orchestrator = Orchestrator()
    due = schedule_service.due_schedules()
    if not due:
        typer.echo("No schedules due.")
        return
    for entry in due:
        typer.echo(f"Running schedule {entry.name} -> {entry.task}")
        orchestrator.run_task(
            project_names=entry.projects,
            task_name=entry.task,
            extra_prompt=None,
            auto_git=False,
        )
        schedule_service.mark_run(entry)


if __name__ == "__main__":
    app()
@approvals_app.command("list")
def approvals_list(
    token: Optional[str] = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    _require_cli_role("run", token)
    pending = state_service.get_pending_approvals()
    if not pending:
        typer.echo("No pending approvals.")
        return
    for entry in pending:
        metadata = json.loads(entry["metadata"] or "{}")
        typer.echo(
            f"- run_id={entry['run_id']} project={entry['project']} task={entry['task']} "
            f"requested={entry['requested_at']} extra_prompt={metadata.get('extra_prompt')}"
        )


@approvals_app.command("approve")
def approvals_approve(
    run_id: int = typer.Argument(..., help="Run ID"),
    approver: str = typer.Option("cli", "--approver", help="Approver name"),
    token: Optional[str] = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    _require_cli_role("approve", token)
    orchestrator = Orchestrator()
    result = orchestrator.run_approved(run_id=run_id, approve=True, approver=approver)
    typer.echo(f"Run {run_id} approved. Status: {result.success}")


@approvals_app.command("deny")
def approvals_deny(
    run_id: int = typer.Argument(..., help="Run ID"),
    approver: str = typer.Option("cli", "--approver", help="Approver name"),
    reason: str = typer.Option("Denied via CLI", "--reason", help="Reason for rejection"),
    token: Optional[str] = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    _require_cli_role("approve", token)
    orchestrator = Orchestrator()
    result = orchestrator.run_approved(run_id=run_id, approve=False, approver=approver, reason=reason)
    typer.echo(f"Run {run_id} denied.")


@tokens_app.command("add")
def tokens_add(
    role: str = typer.Option("run", "--role", help="Token role (read/run/approve/admin)", show_default=True),
    note: Optional[str] = typer.Option(None, "--note", help="Description"),
    token: Optional[str] = typer.Option(None, "--token", help="Admin token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    role = role.lower()
    if role not in token_service.ROLE_RANKS:
        raise typer.BadParameter("Role must be one of read/run/approve/admin")
    existing = token_service.load_tokens()
    if existing:
        _require_cli_role("admin", token)
    else:
        if role != "admin":
            raise typer.BadParameter("First token must be admin")
    entry = token_service.add_token(role, note)
    typer.echo(f"New token ({entry.role}): {entry.token}")
    if entry.note:
        typer.echo(f"Note: {entry.note}")


@tokens_app.command("list")
def tokens_list(
    token: Optional[str] = typer.Option(None, "--token", help="Admin token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    tokens = token_service.load_tokens()
    if not tokens:
        typer.echo("No tokens configured.")
        return
    _require_cli_role("admin", token)
    for entry in tokens:
        typer.echo(f"{entry.token} role={entry.role} note={entry.note or '-'}")


@tokens_app.command("remove")
def tokens_remove(
    token_value: str = typer.Argument(..., help="Token value"),
    token: Optional[str] = typer.Option(None, "--token", help="Admin token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    _require_cli_role("admin", token)
    if token_service.remove_token(token_value):
        typer.echo("Token removed.")
    else:
        typer.echo("Token not found.")


@app.command("lint")
def lint_config() -> None:
    from hivepilot.services.lint_service import lint_configuration

    errors = lint_configuration()
    if errors:
        typer.echo("Lint errors found:")
        for error in errors:
            typer.echo(f"- {error}")
        raise typer.Exit(code=1)
    typer.echo("Configuration looks good.")
