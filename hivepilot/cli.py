from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

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
config_app = typer.Typer(help="Config repo sync")
app.add_typer(config_app, name="config")
telegram_app = typer.Typer(help="Telegram bot")
app.add_typer(telegram_app, name="telegram")
caddy_app = typer.Typer(help="Caddy reverse proxy management")
app.add_typer(caddy_app, name="caddy")
slack_app = typer.Typer(help="Slack bot")
app.add_typer(slack_app, name="slack")
discord_app = typer.Typer(help="Discord bot")
app.add_typer(discord_app, name="discord")
linear_app = typer.Typer(help="Linear issue tracker integration")
app.add_typer(linear_app, name="linear")
iac_app = typer.Typer(help="Infrastructure-as-Code operations")
app.add_typer(iac_app, name="iac")
notion_app = typer.Typer(help="Notion integration")
app.add_typer(notion_app, name="notion")
logger = get_logger(__name__)


def _get_token_value(token: str | None) -> str:
    value = token or os.environ.get("HIVEPILOT_API_TOKEN")
    if not value:
        raise typer.BadParameter("Token required. Pass --token or set HIVEPILOT_API_TOKEN.")
    return value


def _require_cli_role(required: str, token: str | None) -> token_service.TokenEntry:
    token_value = _get_token_value(token)
    entry = token_service.resolve_token(token_value)
    if not entry:
        raise typer.BadParameter("Invalid token")
    if token_service.role_rank(entry.role) < token_service.role_rank(required):
        raise typer.BadParameter(f"Token role '{entry.role}' lacks permission '{required}'")
    return entry


def _resolve_projects(project: str, extras: list[str], run_all: bool) -> list[str]:
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
    roots: list[Path] = typer.Option([], "--root", "-r", help="Root directories to scan (repeatable)"),
    include_hidden: bool = typer.Option(False, "--include-hidden", help="Scan dot-directories"),
    max_depth: int = typer.Option(3, "--max-depth", help="Max directory depth"),
    github_org: str | None = typer.Option(None, "--github-org", help="GitHub organization to scan"),
) -> None:
    """Discover local or GitHub projects and print project config entries."""
    from hivepilot.services.discovery_service import discover_github_repos, discover_local_projects

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
    extra_prompt: str | None = typer.Option(None, "--extra-prompt", "-e", help="Extra instructions"),
    auto_git: bool = typer.Option(False, "--auto-git", help="Run post-task git actions"),
    all_projects: bool = typer.Option(False, "--all", help="Run on every configured project"),
    projects: list[str] = typer.Option([], "--project", "-p", help="Additional projects"),
    concurrency: int | None = typer.Option(None, "--concurrency", "-c", help="Parallel workers"),
    token: str | None = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
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
    extra_prompt: str | None = typer.Option(None, "--extra-prompt", "-e", help="Extra instructions"),
    auto_git: bool = typer.Option(False, "--auto-git", help="Run post-task git actions"),
    all_projects: bool = typer.Option(False, "--all", help="Run on every configured project"),
    projects: list[str] = typer.Option([], "--project", "-p", help="Additional projects"),
    concurrency: int | None = typer.Option(None, "--concurrency", "-c", help="Parallel workers"),
    token: str | None = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
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
    """Diagnose your HivePilot installation and environment."""
    import shutil

    typer.echo("=== Paths ===")
    typer.echo(f"  Base dir      : {settings.base_dir}")
    typer.echo(f"  Projects file : {settings.resolve_path(settings.projects_file)}")
    typer.echo(f"  Tasks file    : {settings.resolve_path(settings.tasks_file)}")
    typer.echo(f"  Pipelines file: {settings.resolve_path(settings.pipelines_file)}")
    typer.echo(f"  Prompts dir   : {settings.resolve_path(settings.prompts_dir)}")
    typer.echo(f"  Runs dir      : {settings.resolve_path(settings.runs_dir)}")
    typer.echo(f"  State DB      : {settings.resolve_path(settings.state_db)}")

    typer.echo("\n=== External binaries ===")
    for binary in [settings.claude_command, settings.gh_command, settings.git_command, "caddy"]:
        found = shutil.which(binary)
        typer.echo(f"  {binary:<12}: {'found at ' + found if found else 'NOT FOUND'}")

    typer.echo("\n=== Optional Python extras ===")
    for dep in ("langchain", "langgraph", "crewai", "boto3", "docker", "telegram", "fastapi", "textual"):
        try:
            __import__(dep)
            typer.echo(f"  {dep:<14}: installed")
        except ImportError:
            typer.echo(f"  {dep:<14}: not installed")

    typer.echo("\n=== Proxy settings ===")
    typer.echo(f"  HTTP_PROXY    : {settings.http_proxy or '(not set)'}")
    typer.echo(f"  HTTPS_PROXY   : {settings.https_proxy or '(not set)'}")
    typer.echo(f"  NO_PROXY      : {settings.no_proxy or '(not set)'}")

    typer.echo("\n=== Config repo ===")
    typer.echo(f"  Repo          : {settings.config_repo or '(not configured)'}")
    typer.echo(f"  Branch        : {settings.config_branch}")

    typer.echo("\n=== Telegram ===")
    typer.echo(f"  Bot token     : {'set' if settings.telegram_bot_token else '(not set)'}")
    typer.echo(f"  Webhook URL   : {settings.telegram_webhook_url or '(not set)'}")


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
    set_remote: bool = typer.Option(True, "--set-remote/--no-set-remote", help="Update origin remote URL"),
    remote_protocol: str = typer.Option("ssh", "--remote-protocol", help="Remote protocol (ssh or https)", show_default=True),
    visibility: str = typer.Option("private", "--visibility", help="Repo visibility (private/public)", show_default=True),
) -> None:
    orchestrator = Orchestrator()
    project_cfg = orchestrator._project(project)  # pylint: disable=protected-access
    remote_protocol = remote_protocol.lower()
    if remote_protocol not in {"ssh", "https"}:
        raise typer.BadParameter("remote-protocol must be 'ssh' or 'https'")
    visibility = visibility.lower()
    if visibility not in {"private", "public"}:
        raise typer.BadParameter("visibility must be 'private' or 'public'")
    ensure_repository(
        project_cfg,
        settings,
        push=push,
        set_remote=set_remote,
        remote_protocol=remote_protocol,
        visibility=visibility,
    )
    typer.echo(f"Repository ready for {project}")


@gh_app.command("issue")
def gh_issue(
    project: str = typer.Argument(..., help="Project key"),
    title: str = typer.Argument(..., help="Issue title"),
    body: str | None = typer.Option(None, "--body", help="Issue body"),
) -> None:
    orchestrator = Orchestrator()
    project_cfg = orchestrator._project(project)  # pylint: disable=protected-access
    create_issue(project=project_cfg, settings=settings, title=title, body=body, labels=[])
    typer.echo("Issue created.")


@gh_app.command("release")
def gh_release(
    project: str = typer.Argument(..., help="Project key"),
    tag: str = typer.Argument(..., help="Release tag"),
    title: str | None = typer.Option(None, "--title", help="Release title"),
    notes_file: Path | None = typer.Option(None, "--notes-file", help="Path to release notes file"),
    generate_notes: bool = typer.Option(True, "--generate-notes/--no-generate-notes", help="Auto-generate release notes"),
) -> None:
    orchestrator = Orchestrator()
    project_cfg = orchestrator._project(project)  # pylint: disable=protected-access
    create_release(
        project=project_cfg,
        settings=settings,
        tag=tag,
        title=title,
        notes_file=notes_file,
        generate_notes=generate_notes,
    )
    typer.echo("Release created.")


@api_app.command("serve")
def serve_api(
    host: str = typer.Option(settings.api_host, "--host"),
    port: int = typer.Option(settings.api_port, "--port"),
    workers: int = typer.Option(1, "--workers", help="Number of uvicorn worker processes"),
) -> None:
    """Launch the FastAPI server for remote control."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise typer.BadParameter("uvicorn not installed. Install hivepilot[full].") from exc

    if workers > 1:
        typer.echo(
            f"WARNING: Running {workers} workers with SQLite state.db may cause data corruption. "
            "Use workers=1 or migrate to PostgreSQL before scaling.",
            err=True,
        )
    typer.echo(f"Serving HivePilot API on http://{host}:{port} (workers={workers})")
    uvicorn.run("hivepilot.services.api_service:app", host=host, port=port, workers=workers)


@api_app.command("systemd-unit")
def api_systemd_unit(
    host: str = typer.Option(settings.api_host, "--host"),
    port: int = typer.Option(settings.api_port, "--port"),
    user: str = typer.Option("hivepilot", "--user", help="System user to run the service"),
    working_dir: str = typer.Option(str(settings.base_dir), "--working-dir", help="Working directory"),
    env_file: str | None = typer.Option(None, "--env-file", help="Path to .env file"),
) -> None:
    """Print a systemd unit file for the HivePilot API server."""
    import shutil
    python_bin = shutil.which("python3") or "/usr/bin/python3"
    hivepilot_bin = shutil.which("hivepilot") or f"{python_bin} -m hivepilot"
    env_line = f"EnvironmentFile={env_file}" if env_file else ""
    unit = f"""[Unit]
Description=HivePilot API Server
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={working_dir}
{env_line}
ExecStart={hivepilot_bin} api serve --host {host} --port {port}
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""
    typer.echo(unit.strip())
    typer.echo("\nTo install:")
    typer.echo("  sudo hivepilot api systemd-unit > /etc/systemd/system/hivepilot-api.service")
    typer.echo("  sudo systemctl daemon-reload && sudo systemctl enable --now hivepilot-api")


@schedule_app.command("daemon")
def schedule_daemon(
    interval: int = typer.Option(30, "--interval", "-i", help="Seconds between schedule checks"),
    shutdown_timeout: int = typer.Option(120, "--shutdown-timeout", help="Seconds to wait for in-flight tasks on SIGTERM"),
    token: str | None = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    """Run the scheduler daemon — polls due schedules and processes the retry queue."""
    _require_cli_role("run", token)
    from hivepilot.services.scheduler_daemon import SchedulerDaemon

    typer.echo(f"Starting scheduler daemon (interval={interval}s, shutdown_timeout={shutdown_timeout}s)")
    typer.echo("Press Ctrl+C or send SIGTERM to stop gracefully.")
    SchedulerDaemon(check_interval=interval, shutdown_timeout=shutdown_timeout).run()


@schedule_app.command("health")
def schedule_health(
    token: str | None = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    """Show schedule status, next run times, and retry queue depth."""
    _require_cli_role("read", token)
    from datetime import datetime, timedelta, timezone

    from hivepilot.services import retry_service, state_service
    from hivepilot.services.schedule_service import load_schedules

    schedules = load_schedules()
    now = datetime.now(timezone.utc)

    typer.echo("=== Schedules ===")
    if not schedules:
        typer.echo("  (none configured)")
    for name, entry in schedules.items():
        status = "enabled" if entry.enabled else "disabled"
        last = state_service.get_schedule_last_run(name)
        if last:
            next_run = last + timedelta(minutes=entry.interval_minutes)
            due_in = (next_run - now).total_seconds()
            next_str = f"due in {int(due_in)}s" if due_in > 0 else "OVERDUE"
        else:
            next_str = "never run"
        typer.echo(f"  {name:<20} task={entry.task:<15} interval={entry.interval_minutes}m  last={last or 'never':<25} next={next_str}  [{status}]")

    pending = retry_service.list_queue("pending")
    running = retry_service.list_queue("running")
    dlq = retry_service.list_dlq()
    typer.echo("\n=== Retry Queue ===")
    typer.echo(f"  Pending : {len(pending)}")
    typer.echo(f"  Running : {len(running)}")
    typer.echo(f"  Dead (DLQ): {len(dlq)}")


@schedule_app.command("systemd-unit")
def schedule_systemd_unit(
    user: str = typer.Option("hivepilot", "--user", help="System user"),
    working_dir: str = typer.Option(str(settings.base_dir), "--working-dir"),
    env_file: str | None = typer.Option(None, "--env-file"),
    interval: int = typer.Option(30, "--interval"),
) -> None:
    """Print a systemd unit file for the scheduler daemon."""
    import shutil
    hivepilot_bin = shutil.which("hivepilot") or "hivepilot"
    env_line = f"EnvironmentFile={env_file}" if env_file else ""
    unit = f"""[Unit]
Description=HivePilot Scheduler Daemon
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={working_dir}
{env_line}
ExecStart={hivepilot_bin} schedule daemon --interval {interval}
Restart=on-failure
RestartSec=10s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""
    typer.echo(unit.strip())
    typer.echo("\nTo install:")
    typer.echo("  sudo hivepilot schedule systemd-unit > /etc/systemd/system/hivepilot-scheduler.service")
    typer.echo("  sudo systemctl daemon-reload && sudo systemctl enable --now hivepilot-scheduler")


@schedule_app.command("retry-list")
def schedule_retry_list(
    status: str | None = typer.Option(None, "--status", help="Filter by status: pending/running/succeeded/dead"),
    token: str | None = typer.Option(None, "--token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    """List jobs in the retry queue."""
    _require_cli_role("read", token)
    from hivepilot.services import retry_service

    jobs = retry_service.list_queue(status)
    if not jobs:
        typer.echo("No retry jobs.")
        return
    for job in jobs:
        import json as _json
        projects = _json.loads(job["projects"])
        typer.echo(
            f"id={job['id']} schedule={job['schedule_name']} task={job['task']} "
            f"projects={projects} attempt={job['attempt']}/{job['max_attempts']} "
            f"status={job['status']} next={job['next_retry_at']}"
        )
        if job.get("error"):
            typer.echo(f"  error: {job['error'][:120]}")


@schedule_app.command("dlq-list")
def schedule_dlq_list(
    token: str | None = typer.Option(None, "--token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    """List dead-letter jobs (permanently failed)."""
    _require_cli_role("read", token)
    from hivepilot.services import retry_service

    jobs = retry_service.list_dlq()
    if not jobs:
        typer.echo("Dead-letter queue is empty.")
        return
    typer.echo(f"{len(jobs)} dead job(s):")
    for job in jobs:
        typer.echo(
            f"  id={job['id']} schedule={job['schedule_name']} task={job['task']} "
            f"attempts={job['attempt']} created={job['created_at']}"
        )
        if job.get("error"):
            typer.echo(f"    error: {job['error'][:120]}")


@schedule_app.command("dlq-purge")
def schedule_dlq_purge(
    confirm: bool = typer.Option(False, "--yes", help="Skip confirmation"),
    token: str | None = typer.Option(None, "--token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    """Delete all jobs from the dead-letter queue."""
    _require_cli_role("approve", token)
    from hivepilot.services import retry_service

    if not confirm:
        typer.confirm("Permanently delete all dead-letter jobs?", abort=True)
    count = retry_service.purge_dlq()
    typer.echo(f"Purged {count} dead job(s).")


@schedule_app.command("list")
def schedule_list(
    token: str | None = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    _require_cli_role("read", token)
    from hivepilot.services.schedule_service import load_schedules

    schedules = load_schedules()
    for name, entry in schedules.items():
        status = "enabled" if entry.enabled else "disabled"
        typer.echo(f"- {name}: task={entry.task} projects={entry.projects} interval={entry.interval_minutes}m ({status})")


@schedule_app.command("run")
def schedule_run(
    token: str | None = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
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
        ok = schedule_service.run_entry(entry, orchestrator)
        if ok:
            typer.echo("  OK")
        else:
            typer.echo("  Failed — enqueued for retry")


if __name__ == "__main__":
    app()
@approvals_app.command("list")
def approvals_list(
    token: str | None = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
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
    token: str | None = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
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
    token: str | None = typer.Option(None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    _require_cli_role("approve", token)
    orchestrator = Orchestrator()
    orchestrator.run_approved(run_id=run_id, approve=False, approver=approver, reason=reason)
    typer.echo(f"Run {run_id} denied.")


@tokens_app.command("add")
def tokens_add(
    role: str = typer.Option("run", "--role", help="Token role (read/run/approve/admin)", show_default=True),
    note: str | None = typer.Option(None, "--note", help="Description"),
    ttl: int | None = typer.Option(None, "--ttl", help="Expiry in days (overrides HIVEPILOT_TOKEN_TTL_DAYS)"),
    token: str | None = typer.Option(None, "--token", help="Admin token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    from hivepilot.utils.validation import validate_note
    role = role.lower()
    if role not in token_service.ROLE_RANKS:
        raise typer.BadParameter("Role must be one of read/run/approve/admin")
    try:
        note = validate_note(note)
    except ValueError as e:
        raise typer.BadParameter(str(e))
    existing = token_service.load_tokens()
    if existing:
        _require_cli_role("admin", token)
    else:
        if role != "admin":
            raise typer.BadParameter("First token must be admin")
    raw_token, entry = token_service.add_token(role, note, ttl_days=ttl)
    typer.echo(f"New token ({entry.role}): {raw_token}")
    typer.echo("Save this token now -- it will not be shown again.")
    if entry.note:
        typer.echo(f"Note: {entry.note}")
    if entry.expires_at:
        typer.echo(f"Expires: {entry.expires_at.strftime('%Y-%m-%d %H:%M UTC')}")


@tokens_app.command("list")
def tokens_list(
    token: str | None = typer.Option(None, "--token", help="Admin token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    from datetime import datetime, timezone
    tokens = token_service.load_tokens()
    if not tokens:
        typer.echo("No tokens configured.")
        return
    _require_cli_role("admin", token)
    now = datetime.now(timezone.utc)
    for entry in tokens:
        masked = entry.token[:8] + "..." + entry.token[-4:]
        if entry.expires_at:
            if entry.is_expired:
                expiry_str = f"EXPIRED ({entry.expires_at.strftime('%Y-%m-%d')})"
            else:
                days_left = (entry.expires_at - now).days
                expiry_str = f"expires {entry.expires_at.strftime('%Y-%m-%d')} ({days_left}d)"
        else:
            expiry_str = "no expiry"
        typer.echo(f"{masked} role={entry.role} {expiry_str} note={entry.note or '-'}")


@tokens_app.command("rotate")
def tokens_rotate(
    token_value: str = typer.Argument(..., help="Current raw token value to rotate"),
    token: str | None = typer.Option(None, "--token", help="Admin token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    _require_cli_role("admin", token)
    result = token_service.rotate_token(token_value)
    if result is None:
        typer.echo("Token not found.")
        raise typer.Exit(1)
    new_raw, entry = result
    typer.echo(f"New token ({entry.role}): {new_raw}")
    typer.echo("Old token has been invalidated. Save this token now -- it will not be shown again.")
    if entry.expires_at:
        typer.echo(f"Expires: {entry.expires_at.strftime('%Y-%m-%d %H:%M UTC')}")


@tokens_app.command("remove")
def tokens_remove(
    token_value: str = typer.Argument(..., help="Token value"),
    token: str | None = typer.Option(None, "--token", help="Admin token", envvar="HIVEPILOT_API_TOKEN"),
) -> None:
    _require_cli_role("admin", token)
    if token_service.remove_token(token_value):
        typer.echo("Token removed.")
    else:
        typer.echo("Token not found.")


@config_app.command("sync")
def config_sync() -> None:
    """Pull latest config from the remote repo into base_dir."""
    from hivepilot.services import config_service

    try:
        updated = config_service.sync()
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    if updated:
        typer.echo(f"Synced {len(updated)} file(s):")
        for f in updated:
            typer.echo(f"  {f}")
    else:
        typer.echo("Already up to date.")


@config_app.command("push")
def config_push(
    message: str = typer.Option("chore: update config", "--message", "-m", help="Commit message"),
) -> None:
    """Push local config changes to the remote repo."""
    from hivepilot.services import config_service

    try:
        config_service.push(message=message)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo("Config pushed.")


@config_app.command("status")
def config_status() -> None:
    """Show git status of the local config repo clone."""
    from hivepilot.services import config_service

    repo_url = settings.config_repo or "(not configured)"
    branch = settings.config_branch
    typer.echo(f"Repo  : {repo_url}")
    typer.echo(f"Branch: {branch}")
    typer.echo("")
    typer.echo(config_service.get_status())


@config_app.command("log")
def config_log(
    n: int = typer.Option(10, "--n", help="Number of commits to show"),
) -> None:
    """Show recent commits from the config repo."""
    from hivepilot.services import config_service

    typer.echo(config_service.get_log(n=n))


@telegram_app.command("start")
def telegram_start(
    mode: str = typer.Option("polling", "--mode", "-m", help="polling or webhook"),
    webhook_url: str | None = typer.Option(None, "--webhook-url", help="Public base URL for webhook mode (e.g. https://myserver.com)"),
    port: int | None = typer.Option(None, "--port", help="Local port for built-in webhook server"),
    secret: str | None = typer.Option(None, "--secret", help="Webhook secret token"),
) -> None:
    """Start the Telegram bot. Blocking — run in a dedicated terminal or systemd unit."""
    from hivepilot.services import telegram_bot as tgbot

    mode = mode.lower()
    try:
        if mode == "polling":
            typer.echo("Starting Telegram bot in polling mode (Ctrl+C to stop)…")
            tgbot.run_polling()
        elif mode == "webhook":
            url = webhook_url or settings.telegram_webhook_url
            if not url:
                typer.echo(
                    "Error: --webhook-url required for webhook mode "
                    "(or set HIVEPILOT_TELEGRAM_WEBHOOK_URL).",
                    err=True,
                )
                raise typer.Exit(1)
            typer.echo(f"Starting Telegram bot in webhook mode on port {port or settings.telegram_webhook_port}…")
            tgbot.run_webhook(webhook_url=url, port=port, secret=secret)
        else:
            typer.echo(f"Unknown mode: {mode!r}. Use 'polling' or 'webhook'.", err=True)
            raise typer.Exit(1)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


@telegram_app.command("set-webhook")
def telegram_set_webhook(
    url: str = typer.Argument(..., help="Public base URL, e.g. https://myserver.com"),
    secret: str | None = typer.Option(None, "--secret", help="Secret token sent in X-Telegram-Bot-Api-Secret-Token header"),
) -> None:
    """Register the webhook URL with Telegram (one-shot, non-blocking)."""
    from hivepilot.services import telegram_bot as tgbot

    try:
        registered = tgbot.set_webhook(url, secret=secret)
        typer.echo(f"Webhook registered: {registered}")
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


@telegram_app.command("delete-webhook")
def telegram_delete_webhook() -> None:
    """Remove the registered webhook from Telegram (switch back to polling)."""
    from hivepilot.services import telegram_bot as tgbot

    try:
        tgbot.delete_webhook()
        typer.echo("Webhook deleted. You can now use polling mode.")
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


@telegram_app.command("info")
def telegram_info() -> None:
    """Show current webhook info from Telegram."""
    from hivepilot.services import telegram_bot as tgbot

    try:
        info = tgbot.get_webhook_info()
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    if info["url"]:
        typer.echo("Mode   : webhook")
        typer.echo(f"URL    : {info['url']}")
        typer.echo(f"Pending: {info['pending_update_count']} updates")
        if info["last_error_message"]:
            typer.echo(f"Error  : {info['last_error_message']}")
    else:
        typer.echo("Mode   : polling (no webhook registered)")
    typer.echo(f"Max connections: {info['max_connections']}")


@caddy_app.command("generate")
def caddy_generate(
    domain: str = typer.Argument(..., help="Public domain name (e.g. hivepilot.example.com)"),
    email: str = typer.Option("", "--email", help="ACME email for Let's Encrypt"),
    tls_internal: bool = typer.Option(False, "--tls-internal", help="Use self-signed cert (LAN/dev)"),
    api_port: int | None = typer.Option(None, "--api-port", help="Upstream API port"),
) -> None:
    """Print the generated Caddyfile without writing it."""
    from hivepilot.services import caddy_service
    typer.echo(caddy_service.generate_caddyfile(domain=domain, email=email, tls_internal=tls_internal, api_port=api_port))


@caddy_app.command("show")
def caddy_show() -> None:
    """Show the current Caddyfile on disk."""
    from hivepilot.services.caddy_service import _CADDYFILE_PATH
    path = _CADDYFILE_PATH
    if not path.exists():
        typer.echo(f"No Caddyfile found at {path}")
        raise typer.Exit(1)
    typer.echo(path.read_text())


@caddy_app.command("setup")
def caddy_setup(
    domain: str = typer.Argument(..., help="Public domain name"),
    email: str = typer.Option("", "--email", help="ACME email for Let's Encrypt"),
    tls_internal: bool = typer.Option(False, "--tls-internal", help="Use self-signed cert"),
    api_port: int | None = typer.Option(None, "--api-port", help="Upstream API port"),
    auto_install: bool = typer.Option(True, "--auto-install/--no-auto-install", help="Install Caddy if missing"),
) -> None:
    """Full one-shot Caddy setup: install, configure, start."""
    from hivepilot.services import caddy_service
    try:
        result = caddy_service.setup(
            domain=domain, email=email, tls_internal=tls_internal,
            api_port=api_port, auto_install=auto_install,
        )
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    for k, v in result.items():
        typer.echo(f"  {k:<14}: {v}")
    typer.echo(f"\nCaddy is serving {domain} -> :{result.get('api_port', settings.api_port)}")


@caddy_app.command("reload")
def caddy_reload() -> None:
    """Reload Caddy configuration without downtime."""
    from hivepilot.services import caddy_service
    try:
        caddy_service.reload_caddy()
        typer.echo("Caddy reloaded.")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


@caddy_app.command("status")
def caddy_status() -> None:
    """Show Caddy service status."""
    from hivepilot.services import caddy_service
    typer.echo(caddy_service.caddy_status())


@caddy_app.command("logs")
def caddy_logs(
    lines: int = typer.Option(50, "--lines", "-n", help="Number of log lines"),
) -> None:
    """Tail Caddy logs."""
    from hivepilot.services import caddy_service
    typer.echo(caddy_service.caddy_logs(lines=lines))


@caddy_app.command("teardown")
def caddy_teardown(
    confirm: bool = typer.Option(False, "--yes", help="Skip confirmation prompt"),
) -> None:
    """Stop and disable Caddy."""
    from hivepilot.services import caddy_service
    if not confirm:
        typer.confirm("Stop and disable Caddy?", abort=True)
    caddy_service.teardown_caddy()
    typer.echo("Caddy stopped and disabled.")


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


# ---------------------------------------------------------------------------
# slack subapp
# ---------------------------------------------------------------------------

@slack_app.command("start")
def slack_start(
    mode: str = typer.Option("socket", "--mode", "-m", help="socket or webhook"),
) -> None:
    """Start the Slack bot. Blocking — run in a dedicated terminal or systemd unit."""
    from hivepilot.services import slack_bot

    mode = mode.lower()
    try:
        if mode == "socket":
            typer.echo("Starting Slack bot in Socket Mode (Ctrl+C to stop)...")
            slack_bot.run_socket_mode()
        elif mode == "webhook":
            typer.echo("Starting Slack bot in webhook mode (served via FastAPI /webhook/slack)...")
            slack_bot.run_webhook_mode()
        else:
            typer.echo(f"Unknown mode: {mode!r}. Use 'socket' or 'webhook'.", err=True)
            raise typer.Exit(1)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


@slack_app.command("notify")
def slack_notify(
    message: str = typer.Argument(..., help="Message to send to the notification channel"),
) -> None:
    """Send a plain text message to the configured Slack notification channel."""
    from hivepilot.services import slack_bot

    try:
        slack_bot.notify(message)
        typer.echo("Message sent.")
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Discord commands (Phase 23d)
# ---------------------------------------------------------------------------

@discord_app.command("start")
def discord_start(
    mode: str = typer.Option("gateway", "--mode", "-m", help="gateway or webhook"),
) -> None:
    """Start the Discord bot. Blocking for gateway mode. For webhook mode, shows the endpoint URL."""
    from hivepilot.services import discord_bot as dbot

    mode = mode.lower()
    try:
        if mode == "gateway":
            typer.echo("Starting Discord bot in gateway mode (Ctrl+C to stop)…")
            dbot.run_gateway()
        elif mode == "webhook":
            base = settings.domain or f"http://{settings.api_host}:{settings.api_port}"
            endpoint = f"{base.rstrip('/')}/webhook/discord"
            typer.echo(f"Discord webhook endpoint: {endpoint}")
            typer.echo(
                "Register this URL in your Discord application's Interactions Endpoint URL field."
            )
            typer.echo(
                "Then start the API server with: hivepilot api start"
            )
        else:
            typer.echo(f"Unknown mode: {mode!r}. Use 'gateway' or 'webhook'.", err=True)
            raise typer.Exit(1)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


@discord_app.command("notify")
def discord_notify(
    message: str = typer.Argument(..., help="Message to send to the notification channel"),
) -> None:
    """Send a plain text message to the configured Discord notification channel."""
    from hivepilot.services import discord_bot as dbot

    try:
        dbot.notify(message)
        typer.echo("Message sent.")
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


@app.command("init")
def init_project(
    template: str = typer.Argument("minimal", help="Template name: minimal, blog, iac, security"),
    project_name: Optional[str] = typer.Option(None, "--name", "-n", help="Project name"),
    project_path: str = typer.Option(".", "--path", "-p", help="Project path"),
    author: str = typer.Option("", "--author", "-a", help="Author name"),
    dest: str = typer.Option(".", "--dest", "-d", help="Destination directory for config files"),
    list_templates: bool = typer.Option(False, "--list", "-l", help="List available templates"),
) -> None:
    """Scaffold a new HivePilot config directory from a built-in template."""
    from hivepilot.services import template_service

    if list_templates:
        for name in template_service.list_templates():
            t = template_service.get_template(name)
            typer.echo(f"  {name:<12} {t['description']}")
        return

    if not project_name:
        typer.echo("Error: --name / -n is required.", err=True)
        raise typer.Exit(1)

    try:
        template_service.get_template(template)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        typer.echo("Run with --list to see available templates.", err=True)
        raise typer.Exit(1)

    variables = {
        "project_name": project_name,
        "project_path": project_path,
        "author": author,
    }

    import warnings
    dest_path = Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        written = template_service.write_template(template, dest_path, variables)

    for w in caught:
        typer.echo(f"warning: {w.message}", err=True)

    if written:
        typer.echo(f"Created {len(written)} file(s) in {dest_path}:")
        for path in written:
            typer.echo(f"  {path}")
    else:
        typer.echo("No files written (all already exist).")

    typer.echo("")
    typer.echo("Next steps:")
    typer.echo(f"  export HIVEPILOT_BASE_DIR={dest_path.resolve()}")
    typer.echo("  hivepilot config sync")
    typer.echo("  hivepilot lint")
    typer.echo("  hivepilot list-projects")


# ---------------------------------------------------------------------------
# IaC commands (Phase 17a)
# ---------------------------------------------------------------------------

def _run_iac_operation(project_name: str, operation: str, kind: str = "opentofu") -> None:
    from hivepilot.models import RunnerDefinition, TaskStep
    from hivepilot.registry import RUNNER_MAP
    from hivepilot.runners.base import RunnerPayload

    projects = load_projects()
    if project_name not in projects.projects:
        raise typer.BadParameter(f"Unknown project: {project_name}")
    project = projects.projects[project_name]

    definition = RunnerDefinition(name=kind, kind=kind, command=operation)
    step = TaskStep(name=f"iac-{operation}", runner=kind, command=operation)
    payload = RunnerPayload(
        project_name=project_name,
        project=project,
        task_name=f"iac-{operation}",
        step=step,
        metadata={},
    )

    runner_cls = RUNNER_MAP[kind]
    runner = runner_cls(definition=definition, settings=settings)
    runner.run(payload)


@iac_app.command("plan")
def iac_plan(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    runner: str = typer.Option("opentofu", "--runner", "-r", help="Runner: opentofu, terraform, pulumi"),
) -> None:
    """Run infrastructure plan."""
    _run_iac_operation(project, "plan", kind=runner)


@iac_app.command("apply")
def iac_apply(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    runner: str = typer.Option("opentofu", "--runner", "-r", help="Runner: opentofu, terraform, pulumi"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Apply infrastructure changes."""
    if not yes:
        typer.confirm(f"Apply infrastructure changes for project '{project}'?", abort=True)
    _run_iac_operation(project, "apply", kind=runner)


@iac_app.command("destroy")
def iac_destroy(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    runner: str = typer.Option("opentofu", "--runner", "-r", help="Runner: opentofu, terraform, pulumi"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Destroy infrastructure."""
    if not yes:
        typer.confirm(f"Destroy infrastructure for project '{project}'? This is irreversible.", abort=True)
    _run_iac_operation(project, "destroy", kind=runner)


@iac_app.command("drift")
def iac_drift(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    runner: str = typer.Option("opentofu", "--runner", "-r", help="Runner: opentofu, terraform, pulumi"),
) -> None:
    """Detect infrastructure drift."""
    try:
        _run_iac_operation(project, "drift", kind=runner)
        typer.echo("No drift detected.")
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)


@iac_app.command("output")
def iac_output(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    runner: str = typer.Option("opentofu", "--runner", "-r", help="Runner: opentofu, terraform, pulumi"),
) -> None:
    """Show infrastructure outputs as JSON."""
    _run_iac_operation(project, "output", kind=runner)


@iac_app.command("cost")
def iac_cost(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    runner: str = typer.Option("opentofu", "--runner", "-r", help="Runner: opentofu, terraform"),
) -> None:
    """Estimate infrastructure cost with Infracost (requires infracost CLI)."""
    try:
        _run_iac_operation(project, "cost", kind=runner)
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Templates marketplace (Phase 22)
# ---------------------------------------------------------------------------

@app.command("templates")
def templates_cmd(
    action: str = typer.Argument("list", help="Action: list, list-remote, pull"),
    name: Optional[str] = typer.Argument(None, help="Template name (for pull)"),
    source: Optional[str] = typer.Option(None, "--source", "-s", help="Remote source: user/repo or HTTPS URL"),
    dest: str = typer.Option(".", "--dest", "-d", help="Destination directory (for pull)"),
) -> None:
    """Manage built-in and community templates."""
    from hivepilot.services import template_service

    if action == "list":
        typer.echo("Built-in templates:")
        for tname in template_service.list_templates():
            t = template_service.get_template(tname)
            typer.echo(f"  {tname:<14} {t['description']}")

    elif action == "list-remote":
        typer.echo(f"Fetching remote templates from {source or 'official registry'}…")
        try:
            remote = template_service.list_remote_templates(source)
        except RuntimeError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1)
        if not remote:
            typer.echo("No templates found.")
            return
        typer.echo(f"{'Name':<20} Description")
        typer.echo("-" * 60)
        for t in remote:
            typer.echo(f"  {t.get('name', '?'):<18} {t.get('description', '')}")

    elif action == "pull":
        if not name:
            typer.echo("Error: template name is required for pull.", err=True)
            raise typer.Exit(1)
        typer.echo(f"Pulling template {name!r} from {source or 'official registry'}…")
        import warnings
        dest_path = Path(dest)
        dest_path.mkdir(parents=True, exist_ok=True)
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                written = template_service.pull_template(name, dest_path, source)
        except (RuntimeError, ValueError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1)
        for w in caught:
            typer.echo(f"warning: {w.message}", err=True)
        if written:
            typer.echo(f"Pulled {len(written)} file(s) into {dest_path}:")
            for path in written:
                typer.echo(f"  {path}")
        else:
            typer.echo("No files written (all already exist).")

    else:
        typer.echo(f"Unknown action: {action!r}. Use list, list-remote, or pull.", err=True)


# ---------------------------------------------------------------------------
# Notion integration
# ---------------------------------------------------------------------------


@notion_app.command("status")
def notion_status() -> None:
    """Show Notion runs database info and last 5 run pages."""
    from hivepilot.services import notion_service

    if not settings.notion_token:
        typer.echo("HIVEPILOT_NOTION_TOKEN is not configured.", err=True)
        raise typer.Exit(1)
    if not settings.notion_runs_database_id:
        typer.echo("HIVEPILOT_NOTION_RUNS_DATABASE_ID is not configured.", err=True)
        raise typer.Exit(1)

    info = notion_service.get_database_info()
    if not info:
        typer.echo("Could not retrieve database info.", err=True)
        raise typer.Exit(1)

    title_parts = info.get("title", [])
    title = title_parts[0]["plain_text"] if title_parts else "(untitled)"
    typer.echo(f"Database: {title} ({info.get('id', '')})")

    runs = notion_service.list_recent_runs(limit=5)
    if not runs:
        typer.echo("No run pages found.")
        return

    typer.echo(f"\nLast {len(runs)} run(s):")
    for page in runs:
        props = page.get("properties", {})
        name_parts = props.get("Name", {}).get("title", [])
        name = name_parts[0]["plain_text"] if name_parts else "(no name)"
        status_sel = props.get("Status", {}).get("select") or {}
        status = status_sel.get("name", "unknown")
        typer.echo(f"  [{status}] {name}")


@notion_app.command("setup")
def notion_setup(
    parent_page_id: str = typer.Argument(..., help="Notion page ID to create the runs database under"),
) -> None:
    """Create the HivePilot runs database schema in Notion. Prints the database_id to add to .env."""
    from hivepilot.services import notion_service

    if not settings.notion_token:
        typer.echo("HIVEPILOT_NOTION_TOKEN is not configured.", err=True)
        raise typer.Exit(1)

    try:
        database_id = notion_service.setup_database(parent_page_id)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Created runs database: {database_id}")
    typer.echo("Add this to your .env:")
    typer.echo(f"  HIVEPILOT_NOTION_RUNS_DATABASE_ID={database_id}")


@notion_app.command("sync")
def notion_sync() -> None:
    """Push last 20 runs from local state to Notion (backfill)."""
    from hivepilot.services import notion_service

    if not settings.notion_token or not settings.notion_runs_database_id:
        typer.echo("Notion is not fully configured (token + runs_database_id required).", err=True)
        raise typer.Exit(1)

    runs = state_service.list_recent_runs(20)
    if not runs:
        typer.echo("No runs found in local state.")
        return

    synced = 0
    for run in runs:
        page_id = notion_service.log_run(
            run_id=run["id"],
            project=run.get("project", ""),
            task=run.get("task", ""),
            status=run.get("status", "unknown"),
            detail=run.get("detail") or "",
            started_at=run.get("started_at") or "",
        )
        if page_id:
            synced += 1

    typer.echo(f"Synced {synced}/{len(runs)} run(s) to Notion.")


# ---------------------------------------------------------------------------
# Linear commands (Phase 17d)
# ---------------------------------------------------------------------------

@linear_app.command("teams")
def linear_teams() -> None:
    """List Linear teams."""
    from hivepilot.services.linear_service import get_teams

    try:
        teams = get_teams()
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    if not teams:
        typer.echo("No teams found.")
        return

    for team in teams:
        typer.echo(f"  {team.get('key', '?'):<10} {team.get('name', '?')}  (id={team.get('id', '?')})")


@linear_app.command("issue")
def linear_issue(
    project: str = typer.Argument(..., help="Project name"),
    task: str = typer.Argument(..., help="Task name"),
    error: str | None = typer.Option(None, "--error", "-e", help="Error message to include in issue body"),
    priority: int = typer.Option(2, "--priority", "-p", help="Priority: 0=none 1=urgent 2=high 3=medium 4=low"),
    team_id: str | None = typer.Option(None, "--team-id", help="Linear team ID (overrides config)"),
) -> None:
    """Manually create a Linear issue for a project/task."""
    from hivepilot.services.linear_service import create_issue

    title = f"[HivePilot] {project}/{task} failed"
    description_parts = [f"**Project:** {project}", f"**Task:** {task}"]
    if error:
        description_parts.append(f"**Error:** {error}")
    description = "\n".join(description_parts)

    try:
        issue = create_issue(title, description, team_id=team_id, priority=priority)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Created issue: {issue.get('identifier', '?')} — {issue.get('title', '?')}")
    typer.echo(f"URL: {issue.get('url', '?')}")


@linear_app.command("states")
def linear_states(
    team_id: str | None = typer.Option(None, "--team-id", help="Team ID (defaults to linear_team_id in config)"),
) -> None:
    """List workflow states for a Linear team."""
    from hivepilot.services.linear_service import get_workflow_states

    resolved_team_id = team_id or settings.linear_team_id
    if not resolved_team_id:
        typer.echo(
            "Error: --team-id required or set HIVEPILOT_LINEAR_TEAM_ID.", err=True
        )
        raise typer.Exit(1)

    try:
        states = get_workflow_states(resolved_team_id)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    if not states:
        typer.echo("No workflow states found.")
        return

    for state in states:
        typer.echo(
            f"  {state.get('name', '?'):<20} type={state.get('type', '?'):<12} id={state.get('id', '?')}"
        )


@linear_app.command("sync")
def linear_sync() -> None:
    """Show last 10 runs and their Linear issue status."""
    from hivepilot.services import state_service

    runs = state_service.list_recent_runs(limit=10)
    if not runs:
        typer.echo("No runs recorded.")
        return

    configured = bool(settings.linear_api_key)
    typer.echo(f"Linear configured: {'yes' if configured else 'no'}")
    typer.echo("")
    typer.echo(f"{'ID':<6} {'Project':<20} {'Task':<20} {'Status':<10} {'Started'}")
    typer.echo("-" * 80)
    for run in runs:
        typer.echo(
            f"{run['id']:<6} {run['project']:<20} {run['task']:<20} "
            f"{run['status']:<10} {run.get('started_at', '?')}"
        )
