from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional, cast

import typer

if TYPE_CHECKING:
    from rich.console import Console

    from hivepilot.plugins import HealthStatus, PluginManager
    from hivepilot.services import agent_checks, init_service

from hivepilot.config import settings
from hivepilot.orchestrator import Orchestrator
from hivepilot.services import state_service, token_service
from hivepilot.services.github_service import create_issue, create_release, ensure_repository
from hivepilot.services.project_service import (
    load_groups,
    load_projects,
    load_tasks,
    resolve_targets,
)
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
project_app = typer.Typer(help="Manage projects.yaml entries")
app.add_typer(project_app, name="project")
task_app = typer.Typer(help="Manage tasks.yaml entries")
app.add_typer(task_app, name="task")
role_app = typer.Typer(help="Manage roles.yaml entries")
app.add_typer(role_app, name="role")
stage_app = typer.Typer(help="Manage pipelines.yaml stage entries")
app.add_typer(stage_app, name="stage")
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
obsidian_app = typer.Typer(help="Obsidian vault integration")
app.add_typer(obsidian_app, name="obsidian")
plugins_app = typer.Typer(help="Inspect loaded plugins")
app.add_typer(plugins_app, name="plugins")
skills_app = typer.Typer(help="Inspect plugin-contributed skills")
app.add_typer(skills_app, name="skills")
scan_app = typer.Typer(help="Supply-chain security scanning (SBOM + vulnerability scan)")
app.add_typer(scan_app, name="scan")
drift_app = typer.Typer(help="Infrastructure drift detection")
app.add_typer(drift_app, name="drift")
playbooks_app = typer.Typer(help="Multi-agent collaboration playbook templates")
app.add_typer(playbooks_app, name="playbooks")
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
    names: list[str] = []
    for _n in [project, *extras]:
        names.extend(resolve_targets(_n))
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
def list_projects(
    projects_file: Path = typer.Option(settings.projects_file, help="Path to projects.yaml"),
) -> None:
    projects = load_projects(projects_file)
    for name, project in projects.projects.items():
        typer.echo(f"- {name}: {project.path} ({project.description or 'n/a'})")


@app.command("discover")
def discover(
    roots: list[Path] = typer.Option(
        [], "--root", "-r", help="Root directories to scan (repeatable)"
    ),
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
    description: {project.description or "auto-discovered"}
    default_branch: {project.default_branch}
    owner_repo: {project.owner_repo or "your-user/your-repo"}
"""
        )


@app.command()
def list_tasks(
    tasks_file: Path = typer.Option(settings.tasks_file, help="Path to tasks.yaml"),
) -> None:
    tasks = load_tasks(tasks_file)
    for name, task in tasks.tasks.items():
        typer.echo(f"- {name}: {task.description} [{len(task.steps)} steps]")


@app.command("list-pipelines")
def list_pipelines(
    pipelines_file: Path = typer.Option(settings.pipelines_file, help="Path to pipelines.yaml"),
) -> None:
    from hivepilot.services.project_service import load_pipelines

    pipelines = load_pipelines(pipelines_file)
    for name, pipeline in pipelines.pipelines.items():
        typer.echo(f"- {name}: {pipeline.description} ({len(pipeline.stages)} stages)")


@app.command()
def run(
    project: str = typer.Argument(..., help="Primary project"),
    task: str = typer.Argument(..., help="Task name"),
    extra_prompt: str | None = typer.Option(
        None, "--extra-prompt", "-e", help="Extra instructions"
    ),
    auto_git: bool = typer.Option(False, "--auto-git", help="Run post-task git actions"),
    all_projects: bool = typer.Option(False, "--all", help="Run on every configured project"),
    projects: list[str] = typer.Option([], "--project", "-p", help="Additional projects"),
    concurrency: int | None = typer.Option(None, "--concurrency", "-c", help="Parallel workers"),
    simulate: bool = typer.Option(
        False, "--simulate", help="Simulate agents — record steps without invoking real runners"
    ),
    token: str | None = typer.Option(
        None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"
    ),
) -> None:
    _require_cli_role("run", token)
    from hivepilot.observability.tracing import init_tracing

    init_tracing(settings)
    orchestrator = Orchestrator()
    target_projects = _resolve_projects(project, projects, all_projects)
    results = orchestrator.run_task(
        project_names=target_projects,
        task_name=task,
        extra_prompt=extra_prompt,
        auto_git=auto_git,
        concurrency=concurrency,
        simulate=simulate,
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
    extra_prompt: str | None = typer.Option(
        None, "--extra-prompt", "-e", help="Extra instructions"
    ),
    auto_git: bool = typer.Option(False, "--auto-git", help="Run post-task git actions"),
    all_projects: bool = typer.Option(False, "--all", help="Run on every configured project"),
    projects: list[str] = typer.Option([], "--project", "-p", help="Additional projects"),
    concurrency: int | None = typer.Option(None, "--concurrency", "-c", help="Parallel workers"),
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run", help="Skip vault writes (default: dry-run)"
    ),
    simulate: bool = typer.Option(
        False, "--simulate", help="Simulate agents — record steps without invoking real runners"
    ),
    token: str | None = typer.Option(
        None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"
    ),
) -> None:
    _require_cli_role("run", token)
    from hivepilot.observability.tracing import init_tracing

    init_tracing(settings)
    orchestrator = Orchestrator()
    _groups = load_groups().groups
    if project in _groups:
        # Group mode: plan once in the hub, fan out execution over the components.
        grp = _groups[project]
        hub = grp.hub or project
        results = orchestrator.run_pipeline(
            project_names=[hub],
            pipeline_name=pipeline,
            extra_prompt=extra_prompt,
            auto_git=auto_git,
            concurrency=concurrency,
            dry_run=dry_run,
            simulate=simulate,
            hub=hub,
            components=grp.components,
            group=grp,
        )
    else:
        target_projects = _resolve_projects(project, projects, all_projects)
        results = orchestrator.run_pipeline(
            project_names=target_projects,
            pipeline_name=pipeline,
            extra_prompt=extra_prompt,
            auto_git=auto_git,
            concurrency=concurrency,
            dry_run=dry_run,
            simulate=simulate,
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

    typer.echo("\n=== Agent runner CLIs ===")
    try:
        from hivepilot.services.project_service import load_tasks

        _cli_kinds = {"claude", "codex", "gemini", "opencode", "ollama", "cursor"}
        _seen: set[str] = set()
        for _name, _rdef in load_tasks().runners.items():
            if _rdef.kind not in _cli_kinds or not _rdef.command:
                continue
            _binary = _rdef.command.strip().split()[0]
            if _binary in _seen:
                continue
            _seen.add(_binary)
            _found = shutil.which(_binary)
            typer.echo(f"  {_binary:<14}: {'found at ' + _found if _found else 'NOT FOUND'}")
    except Exception as _exc:  # noqa: BLE001
        typer.echo(f"  (could not inspect runners: {_exc})")

    typer.echo("\n=== Mandatory agent CLIs ===")
    from hivepilot.services.agent_checks import MANDATORY_AGENTS, check_mandatory_agents

    _report = check_mandatory_agents()
    for _agent_name in MANDATORY_AGENTS:
        _status = "found" if _agent_name in _report.present else "NOT FOUND"
        typer.echo(f"  {_agent_name:<12}: {_status}")
    if _report.any_ok:
        _verdict = (
            "PASS (claude present)"
            if _report.claude_ok
            else f"PASS with WARNING (claude missing; using {', '.join(_report.present)})"
        )
    else:
        _verdict = "FAIL (none of claude/codex/vibe found -- run `hivepilot init` for details)"
    typer.echo(f"  verdict     : {_verdict}")

    typer.echo("\n=== OpenRouter (optional, API-only agent) ===")
    typer.echo(
        "  OPENROUTER_API_KEY: "
        + ("set" if os.environ.get("OPENROUTER_API_KEY") else "(not set) -- optional")
    )

    typer.echo("\n=== Optional Python extras ===")
    for dep in (
        "langchain",
        "langgraph",
        "crewai",
        "boto3",
        "docker",
        "telegram",
        "fastapi",
        "textual",
    ):
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
    push: bool = typer.Option(
        True, "--push/--no-push", help="Push default branch after linking repo"
    ),
    set_remote: bool = typer.Option(
        True, "--set-remote/--no-set-remote", help="Update origin remote URL"
    ),
    remote_protocol: str = typer.Option(
        "ssh", "--remote-protocol", help="Remote protocol (ssh or https)", show_default=True
    ),
    visibility: str = typer.Option(
        "private", "--visibility", help="Repo visibility (private/public)", show_default=True
    ),
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
    generate_notes: bool = typer.Option(
        True, "--generate-notes/--no-generate-notes", help="Auto-generate release notes"
    ),
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
    working_dir: str = typer.Option(
        str(settings.base_dir), "--working-dir", help="Working directory"
    ),
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
    shutdown_timeout: int = typer.Option(
        120, "--shutdown-timeout", help="Seconds to wait for in-flight tasks on SIGTERM"
    ),
    token: str | None = typer.Option(
        None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"
    ),
) -> None:
    """Run the scheduler daemon — polls due schedules and processes the retry queue."""
    _require_cli_role("run", token)
    from hivepilot.services.scheduler_daemon import SchedulerDaemon

    typer.echo(
        f"Starting scheduler daemon (interval={interval}s, shutdown_timeout={shutdown_timeout}s)"
    )
    typer.echo("Press Ctrl+C or send SIGTERM to stop gracefully.")
    SchedulerDaemon(check_interval=interval, shutdown_timeout=shutdown_timeout).run()


@schedule_app.command("health")
def schedule_health(
    token: str | None = typer.Option(
        None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"
    ),
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
        typer.echo(
            f"  {name:<20} task={entry.task:<15} interval={entry.interval_minutes}m  last={last or 'never':<25} next={next_str}  [{status}]"
        )

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
    typer.echo(
        "  sudo hivepilot schedule systemd-unit > /etc/systemd/system/hivepilot-scheduler.service"
    )
    typer.echo("  sudo systemctl daemon-reload && sudo systemctl enable --now hivepilot-scheduler")


@schedule_app.command("retry-list")
def schedule_retry_list(
    status: str | None = typer.Option(
        None, "--status", help="Filter by status: pending/running/succeeded/dead"
    ),
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
    token: str | None = typer.Option(
        None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"
    ),
) -> None:
    _require_cli_role("read", token)
    from hivepilot.services.schedule_service import load_schedules

    schedules = load_schedules()
    for name, entry in schedules.items():
        status = "enabled" if entry.enabled else "disabled"
        typer.echo(
            f"- {name}: task={entry.task} projects={entry.projects} interval={entry.interval_minutes}m ({status})"
        )


@schedule_app.command("run")
def schedule_run(
    token: str | None = typer.Option(
        None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"
    ),
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


@approvals_app.command("list")
def approvals_list(
    token: str | None = typer.Option(
        None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"
    ),
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
    token: str | None = typer.Option(
        None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"
    ),
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
    token: str | None = typer.Option(
        None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"
    ),
) -> None:
    _require_cli_role("approve", token)
    orchestrator = Orchestrator()
    orchestrator.run_approved(run_id=run_id, approve=False, approver=approver, reason=reason)
    typer.echo(f"Run {run_id} denied.")


@tokens_app.command("add")
def tokens_add(
    role: str = typer.Option(
        "run", "--role", help="Token role (read/run/approve/admin)", show_default=True
    ),
    note: str | None = typer.Option(None, "--note", help="Description"),
    ttl: int | None = typer.Option(
        None, "--ttl", help="Expiry in days (overrides HIVEPILOT_TOKEN_TTL_DAYS)"
    ),
    token: str | None = typer.Option(
        None, "--token", help="Admin token", envvar="HIVEPILOT_API_TOKEN"
    ),
    tenant: str = typer.Option("default", "--tenant", help="Tenant this token belongs to"),
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
    raw_token, entry = token_service.add_token(role, note, ttl_days=ttl, tenant=tenant)
    typer.echo(f"New token ({entry.role}): {raw_token}")
    typer.echo("Save this token now -- it will not be shown again.")
    if entry.note:
        typer.echo(f"Note: {entry.note}")
    if entry.expires_at:
        typer.echo(f"Expires: {entry.expires_at.strftime('%Y-%m-%d %H:%M UTC')}")
    if entry.tenant != "default":
        typer.echo(f"Tenant: {entry.tenant}")


@tokens_app.command("list")
def tokens_list(
    token: str | None = typer.Option(
        None, "--token", help="Admin token", envvar="HIVEPILOT_API_TOKEN"
    ),
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
    token: str | None = typer.Option(
        None, "--token", help="Admin token", envvar="HIVEPILOT_API_TOKEN"
    ),
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
    token: str | None = typer.Option(
        None, "--token", help="Admin token", envvar="HIVEPILOT_API_TOKEN"
    ),
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


@config_app.command("get")
def config_get(
    key: str = typer.Argument(..., help="Settings field name (see `config list` for options)"),
) -> None:
    """Print a resolved setting's value, source file, and XDG rank.

    Secret-typed fields (tokens, passwords, API keys, ...) always render as
    REDACTED, never the raw value.
    """
    from hivepilot.services.config_provenance import all_keys, resolve_with_provenance

    try:
        prov = resolve_with_provenance(key)
    except KeyError:
        valid = ", ".join(sorted(all_keys()))
        typer.echo(f"Error: unknown key {key!r}.", err=True)
        typer.echo(f"Valid keys: {valid}", err=True)
        raise typer.Exit(1)

    typer.echo(f"{key} = {prov.value}")
    typer.echo(f"source: {prov.source_path if prov.source_path else '(default/env)'}")
    typer.echo(f"xdg_rank: {prov.xdg_rank}")


@config_app.command("list")
def config_list() -> None:
    """Show every resolved Settings field with its value, source, and XDG rank.

    Secret-typed fields always render as REDACTED, never the raw value.
    """
    from rich.console import Console
    from rich.table import Table

    from hivepilot.services.config_provenance import all_keys, resolve_with_provenance

    table = Table(title="HivePilot Config")
    table.add_column("key")
    table.add_column("value")
    table.add_column("source")
    table.add_column("rank")

    for key in all_keys():
        prov = resolve_with_provenance(key)
        source = str(prov.source_path) if prov.source_path else "-"
        table.add_row(key, str(prov.value), source, str(prov.xdg_rank))

    Console(width=200).print(table)


# ---------------------------------------------------------------------------
# Guided config mutations (Sprint 3 of config-edit-commands): project/task/
# role edit commands. Every write goes through
# hivepilot.services.config_writer.apply_and_validate so a mutation that
# would introduce a validate_config() problem is refused and nothing is
# written (dry_run also always leaves the file untouched).
# ---------------------------------------------------------------------------


def _load_raw_config_file(filename: str):
    """Load *filename* through the same real-path resolution
    ``apply_and_validate`` uses (XDG -> config_repo -> base_dir), returning
    the raw round-trip map. Idempotency checks compare against this raw,
    on-disk representation -- not the pydantic-normalized model, which
    expands paths / applies field defaults and would give false negatives."""
    from ruamel.yaml.comments import CommentedMap

    from hivepilot.services.config_writer import load_roundtrip

    real_path = settings.resolve_config_path(filename)
    try:
        return load_roundtrip(real_path)
    except FileNotFoundError:
        return CommentedMap()


_TRUE_STRINGS = frozenset({"true", "1", "yes"})
_FALSE_STRINGS = frozenset({"false", "0", "no"})


def _coerce_bool(raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE_STRINGS:
        return True
    if lowered in _FALSE_STRINGS:
        return False
    raise ValueError(f"expected one of true/false/1/0/yes/no, got {raw!r}")


@project_app.command("add")
def project_add(
    name: str = typer.Argument(..., help="Project key"),
    path: str = typer.Argument(..., help="Filesystem path to the project"),
    description: str | None = typer.Option(None, "--description", help="Human description"),
    claude_md: str | None = typer.Option(None, "--claude-md", help="Path to project CLAUDE.md"),
    default_branch: str = typer.Option("main", "--default-branch", help="Default git branch"),
    owner_repo: str | None = typer.Option(None, "--owner-repo", help="GitHub owner/repo"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print diff, write nothing"),
    no_input: bool = typer.Option(
        False, "--no-input", help="Never prompt interactively (kept for option parity)"
    ),
) -> None:
    """Add (or fully replace) a projects.yaml entry.

    The passed flags describe the *whole* desired entry (declarative, not a
    merge) -- re-running with identical arguments is a no-op. Only writes
    when the prospective projects.yaml still validates clean.
    """
    from hivepilot.services.config_writer import apply_and_validate

    candidate: dict[str, object] = {"path": path}
    if description is not None:
        candidate["description"] = description
    if claude_md is not None:
        candidate["claude_md"] = claude_md
    if default_branch != "main":
        candidate["default_branch"] = default_branch
    if owner_repo is not None:
        candidate["owner_repo"] = owner_repo

    raw = _load_raw_config_file("projects.yaml")
    existing = (raw.get("projects") or {}).get(name)
    if existing is not None and dict(existing) == candidate:
        typer.echo(f"Project {name!r} already configured identically. No changes.")
        raise typer.Exit(0)

    if existing is not None:
        dropped_fields = sorted(key for key in existing if key not in candidate)
        if dropped_fields:
            typer.echo(
                f"Warning: project {name!r} previously had "
                f"{', '.join(dropped_fields)} set; this run omits the "
                "corresponding flag(s), so they will be cleared "
                "(declarative replace).",
                err=True,
            )

    def mutate(data):
        if "projects" not in data:
            data["projects"] = {}
        data["projects"][name] = candidate
        return data

    result = apply_and_validate("projects.yaml", mutate, dry_run=dry_run, base_dir=None)
    if result.errors:
        for error in result.errors:
            typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1)
    if dry_run:
        typer.echo(result.diff or "No changes.")
        raise typer.Exit(0)
    typer.echo(f"Project {name!r} written to projects.yaml.")


@project_app.command("rm")
def project_rm(
    name: str = typer.Argument(..., help="Project key to remove"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print diff, write nothing"),
    no_input: bool = typer.Option(
        False, "--no-input", help="Never prompt interactively (kept for option parity)"
    ),
) -> None:
    """Remove a projects.yaml entry."""
    from hivepilot.services.config_writer import apply_and_validate
    from hivepilot.services.project_service import load_projects

    projects = load_projects().projects
    if name not in projects:
        typer.echo(f"Error: unknown project {name!r}.", err=True)
        valid = ", ".join(sorted(projects)) or "(none configured)"
        typer.echo(f"Valid projects: {valid}", err=True)
        raise typer.Exit(1)

    def mutate(data):
        if "projects" in data and name in data["projects"]:
            del data["projects"][name]
        return data

    result = apply_and_validate("projects.yaml", mutate, dry_run=dry_run, base_dir=None)
    if result.errors:
        for error in result.errors:
            typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1)
    if dry_run:
        typer.echo(result.diff or "No changes.")
        raise typer.Exit(0)
    typer.echo(f"Project {name!r} removed from projects.yaml.")


@task_app.command("set-role")
def task_set_role(
    task: str = typer.Argument(..., help="Task key"),
    role: str = typer.Argument(..., help="Role name to bind"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print diff, write nothing"),
    no_input: bool = typer.Option(
        False, "--no-input", help="Force the non-interactive refuse path even at a TTY"
    ),
) -> None:
    """Bind a tasks.yaml entry's `role` field to a valid role name.

    If *role* is unknown: at a TTY (and without --no-input) an interactive
    picker offers the valid roles; headlessly (or with --no-input) the
    command refuses and lists the valid roles instead of writing anything.
    """
    from hivepilot.roles import load_roles
    from hivepilot.services.config_writer import apply_and_validate, prompt_or_refuse
    from hivepilot.services.project_service import load_tasks

    tasks = load_tasks().tasks
    if task not in tasks:
        typer.echo(f"Error: unknown task {task!r}.", err=True)
        valid_tasks = ", ".join(sorted(tasks)) or "(none configured)"
        typer.echo(f"Valid tasks: {valid_tasks}", err=True)
        raise typer.Exit(1)

    valid_roles = sorted(load_roles())
    resolved_role = role
    if role not in valid_roles:
        picked = (
            None
            if no_input
            else prompt_or_refuse(valid_roles, f"Unknown role {role!r} -- pick one:")
        )
        if picked is None:
            typer.echo(f"Error: unknown role {role!r}.", err=True)
            valid = ", ".join(valid_roles) or "(none configured)"
            typer.echo(f"Valid roles: {valid}", err=True)
            raise typer.Exit(1)
        resolved_role = picked

    raw = _load_raw_config_file("tasks.yaml")
    raw_tasks = raw.get("tasks") or {}
    if task not in raw_tasks:
        # `tasks` (pydantic-normalized via load_tasks()) reported the task as
        # present, but the raw round-trip map used for the actual mutation
        # does not have it under this exact key (e.g. divergent config
        # layering, or a numeric-looking key YAML parses as int while
        # pydantic's dict[str, TaskConfig] coerces to str). Refuse cleanly
        # instead of letting the mutate() below raise an uncaught KeyError.
        typer.echo(f"Error: task {task!r} not found in the on-disk tasks.yaml.", err=True)
        valid_raw_tasks = ", ".join(sorted(str(k) for k in raw_tasks)) or "(none configured)"
        typer.echo(f"Valid tasks: {valid_raw_tasks}", err=True)
        raise typer.Exit(1)

    raw_task = raw_tasks[task]
    if raw_task.get("role") == resolved_role:
        typer.echo(f"Task {task!r} already bound to role {resolved_role!r}. No changes.")
        raise typer.Exit(0)

    def mutate(data):
        if task not in data.get("tasks", {}):
            # Defensive: apply_and_validate reloads its own copy of the raw
            # map, so re-check membership here too rather than trust the
            # pre-check above never goes stale.
            typer.echo(f"Error: task {task!r} not found in the on-disk tasks.yaml.", err=True)
            raise typer.Exit(1)
        data["tasks"][task]["role"] = resolved_role
        return data

    result = apply_and_validate("tasks.yaml", mutate, dry_run=dry_run, base_dir=None)
    if result.errors:
        for error in result.errors:
            typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1)
    if dry_run:
        typer.echo(result.diff or "No changes.")
        raise typer.Exit(0)
    typer.echo(f"Task {task!r} bound to role {resolved_role!r}.")


_ROLE_INT_FIELDS = frozenset({"order"})
_ROLE_BOOL_FIELDS = frozenset({"can_block"})
_ROLE_LIST_FIELDS = frozenset({"models", "inputs", "outputs", "optional_inputs"})
_ROLE_PERMISSION_MODES = frozenset({"acceptEdits", "bypassPermissions", "plan", "default"})


@role_app.command("wire")
def role_wire(
    role: str = typer.Argument(..., help="Role name"),
    field: str = typer.Argument(..., help="Role field to set (see the `Role` model)"),
    value: str = typer.Argument(..., help="New value for the field"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print diff, write nothing"),
    no_input: bool = typer.Option(
        False, "--no-input", help="Never prompt interactively (kept for option parity)"
    ),
) -> None:
    """Set any field on a roles.yaml entry.

    *value* is coerced to the field's declared type (int for `order`, bool
    for `can_block`, comma-split list for `models`/`inputs`/`outputs`/
    `optional_inputs`, plain str otherwise). `permission_mode` is checked
    against its allowed enum; `prompt_file`/`runner`/`model_profile` are
    checked against their real registries (prompts/agents/, RunnerKind,
    model_profiles.yaml). Any coercion, enum, or reference failure exits 1
    and writes nothing.
    """
    from hivepilot.registry import RUNNER_MAP
    from hivepilot.roles import Role, load_roles
    from hivepilot.services.config_writer import apply_and_validate, resolve_reference
    from hivepilot.services.profile_service import load_claude_profiles

    roles = load_roles()
    if role not in roles:
        typer.echo(f"Error: unknown role {role!r}.", err=True)
        typer.echo(f"Valid roles: {', '.join(sorted(roles))}", err=True)
        raise typer.Exit(1)

    valid_fields = sorted(Role.model_fields)
    if field not in valid_fields:
        typer.echo(f"Error: unknown role field {field!r}.", err=True)
        typer.echo(f"Valid fields: {', '.join(valid_fields)}", err=True)
        raise typer.Exit(1)

    coerced: object
    try:
        if field in _ROLE_INT_FIELDS:
            coerced = int(value)
        elif field in _ROLE_BOOL_FIELDS:
            coerced = _coerce_bool(value)
        elif field in _ROLE_LIST_FIELDS:
            coerced = [item.strip() for item in value.split(",") if item.strip()]
        else:
            coerced = value
    except ValueError as exc:
        typer.echo(f"Error: invalid value {value!r} for field {field!r}: {exc}", err=True)
        raise typer.Exit(1)

    if field == "permission_mode" and coerced not in _ROLE_PERMISSION_MODES:
        typer.echo(f"Error: invalid permission_mode {value!r}.", err=True)
        typer.echo(f"Valid values: {', '.join(sorted(_ROLE_PERMISSION_MODES))}", err=True)
        raise typer.Exit(1)

    if field == "prompt_file" and not resolve_reference("prompt_file", str(coerced)):
        typer.echo(f"Error: prompt_file {value!r} not found under prompts/agents/.", err=True)
        raise typer.Exit(1)

    if field == "runner":
        # Validate against the ACTUAL live registry (RUNNER_MAP), not the
        # static KNOWN_RUNNER_KINDS tuple — so plugin-contributed runner
        # kinds are accepted and advertised-but-unregistered orphan names
        # (e.g. the historical "api" kind; see roadmap Phase 26a) are
        # rejected consistently with resolve-time behavior. RUNNER_MAP only
        # holds the 11 builtins until a PluginManager has run (it's what
        # registers plugin runner kinds into RUNNER_MAP) — construct one
        # first so a genuinely plugin-contributed kind is actually seen
        # here, not just by callers that happen to build an Orchestrator
        # first. PluginManager itself is fail-isolated (a broken plugin is
        # logged and skipped, never raised) and honors
        # settings.plugins_enabled internally.
        from hivepilot.plugins import PluginManager

        PluginManager()
        valid_runners = sorted(RUNNER_MAP)
        if coerced not in valid_runners:
            typer.echo(f"Error: unknown runner kind {value!r}.", err=True)
            typer.echo(f"Valid runner kinds: {', '.join(valid_runners)}", err=True)
            raise typer.Exit(1)

    if field == "model_profile":
        valid_profiles = sorted(load_claude_profiles())
        if coerced not in valid_profiles:
            typer.echo(f"Error: unknown model_profile {value!r}.", err=True)
            valid = ", ".join(valid_profiles) or "(none configured)"
            typer.echo(f"Valid model_profiles: {valid}", err=True)
            raise typer.Exit(1)

    raw = _load_raw_config_file("roles.yaml")
    raw_roles = raw.get("roles") or []
    raw_entry = next(
        (entry for entry in raw_roles if isinstance(entry, dict) and entry.get("name") == role),
        None,
    )
    if raw_entry is None:
        # `roles` (load_roles(), pydantic-normalized) reported the role as
        # valid, but no entry in the raw roles.yaml list matches its name.
        # Without this guard, mutate()'s for/break loop below would silently
        # no-op (write the file back unchanged) while the command still
        # echoed success. Refuse explicitly instead.
        typer.echo(f"Error: role {role!r} not found in the on-disk roles.yaml.", err=True)
        valid_raw_roles = (
            ", ".join(
                sorted(
                    str(entry.get("name"))
                    for entry in raw_roles
                    if isinstance(entry, dict) and entry.get("name") is not None
                )
            )
            or "(none configured)"
        )
        typer.echo(f"Valid roles: {valid_raw_roles}", err=True)
        raise typer.Exit(1)

    existing_value = raw_entry.get(field)
    if existing_value == coerced:
        typer.echo(f"Role {role!r} field {field!r} already set to {value!r}. No changes.")
        raise typer.Exit(0)

    def mutate(data):
        for entry in data.get("roles", []):
            if isinstance(entry, dict) and entry.get("name") == role:
                entry[field] = coerced
                break
        else:
            # Defensive: apply_and_validate reloads its own copy of the raw
            # map -- re-check membership rather than trust the pre-check
            # above never goes stale, so this never silently no-ops either.
            typer.echo(f"Error: role {role!r} not found in the on-disk roles.yaml.", err=True)
            raise typer.Exit(1)
        return data

    result = apply_and_validate("roles.yaml", mutate, dry_run=dry_run, base_dir=None)
    if result.errors:
        for error in result.errors:
            typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1)
    if dry_run:
        typer.echo(result.diff or "No changes.")
        raise typer.Exit(0)
    typer.echo(f"Role {role!r} field {field!r} set to {value!r}.")


def _resolve_raw_pipeline_stage(pipeline: str, stage: str):
    """Validate *pipeline*/*stage* exist (pydantic-normalized, matching every
    other guided-mutation command's pre-check style -- see `task_set_role` /
    `role_wire` above) and return the matching RAW round-trip stage entry
    from pipelines.yaml. Exits 1 with a helpful message listing valid names
    when either is unknown, or when the pydantic-normalized view and the raw
    round-trip map have drifted (mirrors the defensive raw-map re-check
    every other Sprint 3 command performs)."""
    from hivepilot.services.project_service import load_pipelines

    pipelines = load_pipelines().pipelines
    if pipeline not in pipelines:
        typer.echo(f"Error: unknown pipeline {pipeline!r}.", err=True)
        valid = ", ".join(sorted(pipelines)) or "(none configured)"
        typer.echo(f"Valid pipelines: {valid}", err=True)
        raise typer.Exit(1)

    stage_names = [s.name for s in pipelines[pipeline].stages]
    if stage not in stage_names:
        typer.echo(f"Error: unknown stage {stage!r} in pipeline {pipeline!r}.", err=True)
        valid = ", ".join(stage_names) or "(none configured)"
        typer.echo(f"Valid stages: {valid}", err=True)
        raise typer.Exit(1)

    raw = _load_raw_config_file("pipelines.yaml")
    raw_pipeline = (raw.get("pipelines") or {}).get(pipeline)
    if isinstance(raw_pipeline, dict):
        for entry in raw_pipeline.get("stages") or []:
            if isinstance(entry, dict) and entry.get("name") == stage:
                return entry
    typer.echo(
        f"Error: pipeline {pipeline!r} stage {stage!r} not found in the on-disk pipelines.yaml.",
        err=True,
    )
    raise typer.Exit(1)


@stage_app.command("attach-skill")
def stage_attach_skill(
    pipeline: str = typer.Argument(..., help="Pipeline name"),
    stage: str = typer.Argument(..., help="Stage name"),
    skill: str = typer.Argument(..., help="Skill name to attach"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print diff, write nothing"),
) -> None:
    """Attach a plugin-contributed skill to a pipelines.yaml stage's
    `skills` list (`PipelineStage.skills` -- ordered, deduped).

    Does NOT re-check *skill* against the registered skill catalog itself --
    that fail-closed cross-reference (unknown skill -> hard error, plus the
    optional `min_role` gate) is entirely REUSED from
    `hivepilot.services.config_validation.validate_config`, which
    `apply_and_validate` already runs against the prospective pipelines.yaml
    before ever writing it. An unknown skill therefore refuses the write and
    prints that validator's "references unknown skill '<name>'" error,
    same as any other cross-reference violation this command family guards
    against.
    """
    from hivepilot.services.config_writer import apply_and_validate

    raw_stage = _resolve_raw_pipeline_stage(pipeline, stage)
    existing_skills = list(raw_stage.get("skills") or [])
    if skill in existing_skills:
        typer.echo(
            f"Skill {skill!r} already attached to pipeline {pipeline!r} stage {stage!r}. "
            "No changes."
        )
        raise typer.Exit(0)

    def mutate(data):
        stages = ((data.get("pipelines") or {}).get(pipeline) or {}).get("stages") or []
        for entry in stages:
            if isinstance(entry, dict) and entry.get("name") == stage:
                current = list(entry.get("skills") or [])
                current.append(skill)
                entry["skills"] = current
                return data
        # Defensive: apply_and_validate reloads its own copy of the raw map
        # -- re-check membership rather than trust the pre-check above never
        # goes stale, so this never silently no-ops (mirrors task_set_role /
        # role_wire's identical guard).
        typer.echo(
            f"Error: pipeline {pipeline!r} stage {stage!r} not found in the on-disk "
            "pipelines.yaml.",
            err=True,
        )
        raise typer.Exit(1)

    result = apply_and_validate("pipelines.yaml", mutate, dry_run=dry_run, base_dir=None)
    if result.errors:
        for error in result.errors:
            typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1)
    if dry_run:
        typer.echo(result.diff or "No changes.")
        raise typer.Exit(0)
    typer.echo(f"Skill {skill!r} attached to pipeline {pipeline!r} stage {stage!r}.")


@stage_app.command("detach-skill")
def stage_detach_skill(
    pipeline: str = typer.Argument(..., help="Pipeline name"),
    stage: str = typer.Argument(..., help="Stage name"),
    skill: str = typer.Argument(..., help="Skill name to detach"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print diff, write nothing"),
) -> None:
    """Detach a skill from a pipelines.yaml stage's `skills` list. A no-op
    (exit 0, writes nothing) when the skill is already absent. An empty
    `skills` list is dropped entirely rather than left as `skills: []`, so a
    fully-detached stage round-trips byte-identical to one that never had
    any skill attached (`PipelineStage.skills` defaults to `None`)."""
    from hivepilot.services.config_writer import apply_and_validate

    raw_stage = _resolve_raw_pipeline_stage(pipeline, stage)
    existing_skills = list(raw_stage.get("skills") or [])
    if skill not in existing_skills:
        typer.echo(
            f"Skill {skill!r} already absent from pipeline {pipeline!r} stage {stage!r}. "
            "No changes."
        )
        raise typer.Exit(0)

    def mutate(data):
        stages = ((data.get("pipelines") or {}).get(pipeline) or {}).get("stages") or []
        for entry in stages:
            if isinstance(entry, dict) and entry.get("name") == stage:
                remaining = [s for s in (entry.get("skills") or []) if s != skill]
                if remaining:
                    entry["skills"] = remaining
                else:
                    entry.pop("skills", None)
                return data
        # Defensive: same re-check as stage_attach_skill's mutate() above.
        typer.echo(
            f"Error: pipeline {pipeline!r} stage {stage!r} not found in the on-disk "
            "pipelines.yaml.",
            err=True,
        )
        raise typer.Exit(1)

    result = apply_and_validate("pipelines.yaml", mutate, dry_run=dry_run, base_dir=None)
    if result.errors:
        for error in result.errors:
            typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1)
    if dry_run:
        typer.echo(result.diff or "No changes.")
        raise typer.Exit(0)
    typer.echo(f"Skill {skill!r} detached from pipeline {pipeline!r} stage {stage!r}.")


@telegram_app.command("start")
def telegram_start(
    mode: str = typer.Option("polling", "--mode", "-m", help="polling or webhook"),
    webhook_url: str | None = typer.Option(
        None, "--webhook-url", help="Public base URL for webhook mode (e.g. https://myserver.com)"
    ),
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
            typer.echo(
                f"Starting Telegram bot in webhook mode on port {port or settings.telegram_webhook_port}…"
            )
            tgbot.run_webhook(webhook_url=url, port=port, secret=secret)
        else:
            typer.echo(f"Unknown mode: {mode!r}. Use 'polling' or 'webhook'.", err=True)
            raise typer.Exit(1)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


@telegram_app.command("chat-id")
def telegram_chat_id() -> None:
    """List chat IDs that recently messaged the bot (DM the bot first)."""
    from hivepilot.services import telegram_bot

    chats = telegram_bot.fetch_recent_chats()
    if not chats:
        typer.echo("No recent chats. Send a message to your bot in Telegram, then retry.")
        raise typer.Exit(1)
    for ch in chats:
        typer.echo(f"{ch['id']}\t{ch['name']}")
    typer.echo("\nAdd to .env: HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS=[<id>]")


@telegram_app.command("systemd-unit")
def telegram_systemd_unit(
    working_dir: str = typer.Option(str(settings.base_dir), "--working-dir"),
    env_file: str | None = typer.Option(None, "--env-file", help="Optional EnvironmentFile"),
) -> None:
    """Print a systemd *user* unit to run the Telegram bot persistently."""
    import os
    import shutil

    hivepilot_bin = shutil.which("hivepilot") or str(
        settings.base_dir / ".venv" / "bin" / "hivepilot"
    )
    # Capture the dirs of the agent CLIs so the (minimal-PATH) systemd service finds them.
    _dirs: list[str] = [str(settings.base_dir / ".venv" / "bin")]
    for _b in ("claude", "codex", "gemini", "opencode", "cursor-agent", "gh", "git"):
        _p = shutil.which(_b)
        if _p and os.path.dirname(_p) not in _dirs:
            _dirs.append(os.path.dirname(_p))
    path_line = (
        "Environment=PATH=" + ":".join(_dirs + ["/usr/local/bin", "/usr/bin", "/bin"]) + "\n"
    )
    env_line = f"EnvironmentFile={env_file}\n" if env_file else ""
    unit = f"""[Unit]
Description=HivePilot Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={working_dir}
{path_line}{env_line}ExecStart={hivepilot_bin} telegram start
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""
    typer.echo(unit.strip())
    typer.echo("\n# Install (user service, no sudo):")
    typer.echo("#   mkdir -p ~/.config/systemd/user")
    typer.echo(
        "#   hivepilot telegram systemd-unit > ~/.config/systemd/user/hivepilot-telegram.service"
    )
    typer.echo(
        "#   systemctl --user daemon-reload && systemctl --user enable --now hivepilot-telegram"
    )
    typer.echo("#   loginctl enable-linger $USER   # keep running after logout")
    typer.echo(
        "#   journalctl --user -u hivepilot-telegram -f   # logs (token is no longer logged)"
    )


@telegram_app.command("set-webhook")
def telegram_set_webhook(
    url: str = typer.Argument(..., help="Public base URL, e.g. https://myserver.com"),
    secret: str | None = typer.Option(
        None, "--secret", help="Secret token sent in X-Telegram-Bot-Api-Secret-Token header"
    ),
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
    tls_internal: bool = typer.Option(
        False, "--tls-internal", help="Use self-signed cert (LAN/dev)"
    ),
    api_port: int | None = typer.Option(None, "--api-port", help="Upstream API port"),
) -> None:
    """Print the generated Caddyfile without writing it."""
    from hivepilot.services import caddy_service

    typer.echo(
        caddy_service.generate_caddyfile(
            domain=domain, email=email, tls_internal=tls_internal, api_port=api_port
        )
    )


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
    auto_install: bool = typer.Option(
        True, "--auto-install/--no-auto-install", help="Install Caddy if missing"
    ),
) -> None:
    """Full one-shot Caddy setup: install, configure, start."""
    from hivepilot.services import caddy_service

    try:
        result = caddy_service.setup(
            domain=domain,
            email=email,
            tls_internal=tls_internal,
            api_port=api_port,
            auto_install=auto_install,
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
            typer.echo("Then start the API server with: hivepilot api start")
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


@app.command("init-template")
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


def _cli_actor() -> str:
    """Best-effort OS username for the CLI audit trail (Part B-cli). Falls
    back to a generic label rather than failing the whole operation over an
    unreadable environment."""
    import getpass

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "cli"


def _run_iac_operation(
    project_name: str, operation: str, kind: str = "opentofu", *, yes: bool = False
) -> None:
    """Execute a single IaC operation against *project_name* through the
    resolved runner directly (no orchestrator run/task involved -- this IS
    the orchestrator-bypass CLI path), while still honouring the same two
    contracts an orchestrator-run step gets (Part B-cli — closes the gap left
    by Part B-core, which only gates `Orchestrator.run_task`):

    * Secrets: any ``${secret:NAME}`` reference in the project's ``env`` is
      resolved through the exact same mechanism
      ``Orchestrator._resolve_secrets`` uses for an orchestrator-run step
      (``secret_refs.resolve_secret_refs`` + ``secrets_service.secret_resolver``),
      so a CLI-triggered ``apply``/``destroy`` gets real ``TF_VAR_*``/cloud
      credentials instead of a literal unresolved ``${secret:...}`` string --
      and every resolved value is registered with
      ``config_provenance.register_secret_value`` so it can never leak into
      echoed CLI output or the audit summary below.
    * Destructive-op gate: when the resolved runner's optional
      ``is_destructive(payload)`` (see
      ``hivepilot.orchestrator.step_requires_approval`` for the
      orchestrator's equivalent gate) reports that *operation* mutates real
      infrastructure, a human must explicitly confirm -- via ``--yes`` or an
      interactive prompt -- before anything runs, and the confirmed
      operation is always recorded via ``state_service.record_interaction``,
      the audit trail this direct CLI path would otherwise have none of.
    """
    from hivepilot.models import RunnerDefinition, RunnerKind, TaskStep
    from hivepilot.registry import resolve_runner_class
    from hivepilot.runners.base import RunnerPayload
    from hivepilot.services import policy_service
    from hivepilot.services.config_provenance import register_secret_value
    from hivepilot.services.secret_refs import resolve_secret_refs
    from hivepilot.services.secrets_service import secret_resolver

    projects = load_projects()
    if project_name not in projects.projects:
        raise typer.BadParameter(f"Unknown project: {project_name}")
    project = projects.projects[project_name]

    definition = RunnerDefinition(name=kind, kind=cast(RunnerKind, kind), command=operation)
    step = TaskStep(name=f"iac-{operation}", runner=kind, command=operation)

    # Mirrors Orchestrator._resolve_secrets: direct step.secrets form (always
    # empty for a CLI-built step today, but kept for parity) plus lazily
    # resolved ${secret:NAME} references embedded in project.env.
    policy = policy_service.get_policy(project_name)
    resolved_secrets: dict[str, str] = {}
    if step.secrets:
        resolved_secrets.update(secret_resolver.resolve(step.secrets))
    if project.env:
        resolved_secrets.update(
            resolve_secret_refs(
                project.env, catalog=project.secrets, fail_mode=policy.secrets_fail_mode
            )
        )
    for value in resolved_secrets.values():
        register_secret_value(value)

    payload = RunnerPayload(
        project_name=project_name,
        project=project,
        task_name=f"iac-{operation}",
        step=step,
        metadata={},
        secrets=resolved_secrets,
    )

    runner_cls = resolve_runner_class(kind)
    runner = runner_cls(definition=definition, settings=settings)

    is_destructive_fn = getattr(runner, "is_destructive", None)
    destructive = bool(is_destructive_fn(payload)) if is_destructive_fn is not None else False

    if destructive:
        if not yes:
            typer.confirm(
                f"⚠️  This will run a DESTRUCTIVE {kind} {operation} on project "
                f"'{project_name}' — continue?",
                abort=True,
            )
        state_service.record_interaction(
            actor=_cli_actor(),
            action=f"iac.{operation}",
            target=project_name,
            summary=(
                f"Destructive IaC '{operation}' ({kind}) run via CLI for project '{project_name}'"
            ),
            metadata={
                "operation": operation,
                "runner": kind,
                "confirmed_via": "--yes" if yes else "interactive prompt",
                "destructive": True,
            },
        )

    runner.run(payload)


@iac_app.command("plan")
def iac_plan(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    runner: str = typer.Option(
        "opentofu", "--runner", "-r", help="Runner: opentofu, terraform, pulumi"
    ),
) -> None:
    """Run infrastructure plan."""
    _run_iac_operation(project, "plan", kind=runner)


@iac_app.command("apply")
def iac_apply(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    runner: str = typer.Option(
        "opentofu", "--runner", "-r", help="Runner: opentofu, terraform, pulumi"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Apply infrastructure changes.

    A destructive apply always requires explicit confirmation (interactive
    prompt, or --yes for non-interactive use) and is always audit-logged --
    see `_run_iac_operation`.
    """
    _run_iac_operation(project, "apply", kind=runner, yes=yes)


@iac_app.command("destroy")
def iac_destroy(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    runner: str = typer.Option(
        "opentofu", "--runner", "-r", help="Runner: opentofu, terraform, pulumi"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Destroy infrastructure.

    Always requires explicit confirmation (interactive prompt, or --yes for
    non-interactive use) and is always audit-logged -- see
    `_run_iac_operation`.
    """
    _run_iac_operation(project, "destroy", kind=runner, yes=yes)


@iac_app.command("drift")
def iac_drift(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    runner: str = typer.Option(
        "opentofu", "--runner", "-r", help="Runner: opentofu, terraform, pulumi"
    ),
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
    runner: str = typer.Option(
        "opentofu", "--runner", "-r", help="Runner: opentofu, terraform, pulumi"
    ),
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
# Drift detection commands (Phase 20 Sprint D4)
#
# `drift scan` is READ-ONLY: it calls `drift_service.scan_and_record`
# directly (a plain service, not `_run_iac_operation`) and only ever prints
# integer plan counts / status, never raw plan stdout -- same anti-leak
# guarantee as `drift_schedule.run_drift_scan`'s alerts. `drift status`/
# `drift report` are read-only state queries, tenant-scoped explicitly on
# every call. There is deliberately NO destructive command in this group --
# a gated remediation `apply` only ever happens via the operator-configured
# `remediate_task`, routed through `Orchestrator.run_task` (see
# `hivepilot.services.drift_schedule._attempt_remediation`), never a raw CLI
# apply.
# ---------------------------------------------------------------------------


@drift_app.command("scan")
def drift_scan_cmd(
    project: str = typer.Option(..., "--project", "-p", help="Project name"),
    runner: str = typer.Option("opentofu", "--runner", "-r", help="Runner: opentofu, terraform"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant to scope this scan to"),
) -> None:
    """Run an IaC drift scan for a project and record the result."""
    from hivepilot.services import drift_service

    projects = load_projects()
    if project not in projects.projects:
        raise typer.BadParameter(f"Unknown project: {project}")
    project_cfg = projects.projects[project]

    try:
        result = drift_service.scan_and_record(project_cfg, runner_kind=runner, tenant=tenant)
    except (RuntimeError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)

    if not result.drifted:
        typer.echo("No drift detected.")
        return

    if result.summary is not None:
        s = result.summary
        typer.echo(f"Drift detected: +{s.to_add} ~{s.to_change} -{s.to_destroy}")
    else:
        typer.echo("Drift detected (changes detected).")


@drift_app.command("status")
def drift_status_cmd(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Filter by project"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max rows to show"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant to scope this read to"),
) -> None:
    """Show recent drift-scan history."""
    rows = state_service.get_recent_drift_scans(project=project, limit=limit, tenant=tenant)
    if not rows:
        typer.echo("No drift scans recorded.")
        return

    typer.echo(f"{'Checked At':<20} {'Project':<20} {'Runner':<10} {'Status':<8} Changes")
    typer.echo("-" * 80)
    for row in rows:
        if row.get("status") == "drift":
            changes = (
                f"+{row.get('to_add') or 0} ~{row.get('to_change') or 0} "
                f"-{row.get('to_destroy') or 0}"
            )
        else:
            changes = "-"
        typer.echo(
            f"{str(row.get('checked_at', '?')):<20} {str(row.get('project', '?')):<20} "
            f"{str(row.get('runner', '?')):<10} {str(row.get('status', '?')):<8} {changes}"
        )


@drift_app.command("report")
def drift_report_cmd(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Filter by project"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant to scope this read to"),
) -> None:
    """Show the current no-drift baseline + recent history for a project."""
    if project is not None:
        baseline = state_service.get_drift_baseline(project, tenant=tenant)
        if baseline is None:
            typer.echo(f"No no-drift baseline recorded yet for {project}.")
        else:
            typer.echo(
                f"Baseline ({project}): last clean scan at "
                f"{baseline.get('checked_at', '?')} via {baseline.get('runner', '?')}"
            )
        typer.echo("")

    rows = state_service.get_recent_drift_scans(project=project, limit=10, tenant=tenant)
    if not rows:
        typer.echo("No drift scan history recorded.")
        return

    typer.echo("Recent history:")
    for row in rows:
        typer.echo(
            f"  {row.get('checked_at', '?')}  {str(row.get('project', '?')):<20} "
            f"{str(row.get('status', '?')):<8} "
            f"(+{row.get('to_add') or 0} ~{row.get('to_change') or 0} "
            f"-{row.get('to_destroy') or 0})"
        )


# ---------------------------------------------------------------------------
# Supply-chain scanning: vulnerability scan (grype/osv-scanner) + SBOM (syft)
# (Phase 21 Sprint 1)
#
# Both commands resolve the project's `path` (the same `load_projects()`
# lookup the `iac`/`project` commands use) and delegate to
# `hivepilot.services.scan_service` -- a plain service, not a runner, invoked
# directly here (no `Orchestrator`/`RunResult` in this path). The service
# already returns fully parsed/structured results, so nothing raw ever
# reaches this CLI layer to leak. `scan vulns --fail-on <severity>` is a
# manual gate at this CLI layer only; the automatic pipeline-run CVE gate
# (`policy.block_on_severity`, Phase 21 Sprint 2) lives in
# `Orchestrator._run_task_body`/`_cve_gate_block_detail`, not here.
# ---------------------------------------------------------------------------


def _resolve_project_path(project_name: str) -> Path:
    projects = load_projects()
    if project_name not in projects.projects:
        raise typer.BadParameter(f"Unknown project: {project_name}")
    return projects.projects[project_name].path


@scan_app.command("vulns")
def scan_vulns(
    project: str = typer.Argument(..., help="Project name"),
    tool: str = typer.Option("grype", "--tool", help="Scanner: grype, osv-scanner"),
    fail_on: Optional[str] = typer.Option(
        None,
        "--fail-on",
        help="Exit non-zero if any finding is at or above this severity "
        "(critical, high, medium, low, negligible)",
    ),
) -> None:
    """Scan a project's dependencies for known vulnerabilities (CVEs)."""
    from hivepilot.services import scan_service

    project_path = _resolve_project_path(project)

    if fail_on is not None and fail_on.strip().lower() not in scan_service.SEVERITY_LEVELS:
        raise typer.BadParameter(
            f"--fail-on must be one of {', '.join(scan_service.SEVERITY_LEVELS)}"
        )

    try:
        result = scan_service.scan_vulnerabilities(project_path, tool=tool)
    except (RuntimeError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Scan tool: {result.tool}")
    typer.echo(f"Total findings: {result.total}")
    for severity in scan_service.SEVERITY_LEVELS:
        typer.echo(f"  {severity}: {result.by_severity.get(severity, 0)}")

    if result.findings:
        typer.echo("")
        typer.echo(f"{'ID':<20} {'PACKAGE':<25} {'VERSION':<15} {'SEVERITY':<10} FIXED")
        for finding in result.findings:
            typer.echo(
                f"{finding.id:<20} {finding.package:<25} {finding.version:<15} "
                f"{finding.severity:<10} {finding.fixed_version or '-'}"
            )

    if fail_on is not None and scan_service.exceeds_severity(result, fail_on.strip().lower()):
        typer.echo(f"\nFound findings at or above '--fail-on {fail_on}' severity.", err=True)
        raise typer.Exit(code=1)


@scan_app.command("sbom")
def scan_sbom(
    project: str = typer.Argument(..., help="Project name"),
    format: str = typer.Option("cyclonedx", "--format", help="SBOM format: cyclonedx, spdx"),
    output: Optional[str] = typer.Option(
        None, "--output", "-o", help="Write SBOM to this file instead of stdout"
    ),
) -> None:
    """Generate a Software Bill of Materials (SBOM) for a project."""
    from hivepilot.services import scan_service

    project_path = _resolve_project_path(project)

    try:
        sbom = scan_service.generate_sbom(
            project_path, format=format, output_path=Path(output) if output else None
        )
    except (RuntimeError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)

    if output:
        typer.echo(f"SBOM written to {output}")
    else:
        typer.echo(sbom)


# ---------------------------------------------------------------------------
# Templates marketplace (Phase 22)
# ---------------------------------------------------------------------------


@app.command("templates")
def templates_cmd(
    action: str = typer.Argument("list", help="Action: list, list-remote, pull"),
    name: Optional[str] = typer.Argument(None, help="Template name (for pull)"),
    source: Optional[str] = typer.Option(
        None, "--source", "-s", help="Remote source: user/repo or HTTPS URL"
    ),
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
# Multi-agent collaboration playbooks (Phase 16)
# ---------------------------------------------------------------------------


@playbooks_app.command("list")
def playbooks_list_cmd() -> None:
    """List the built-in multi-agent collaboration playbook templates."""
    from hivepilot.scaffold.playbooks import list_playbooks

    typer.echo("Multi-agent collaboration playbooks:")
    for playbook in list_playbooks():
        typer.echo(f"  {playbook.name:<24} {playbook.title}")
        typer.echo(f"  {'':<24} {playbook.description}")
        typer.echo(f"  {'':<24} Flow: {playbook.flow_summary}")
        typer.echo("")


@playbooks_app.command("show")
def playbooks_show_cmd(
    name: str = typer.Argument(..., help="Playbook name"),
) -> None:
    """Show a playbook's flow summary, README, and the files it provides."""
    from hivepilot.scaffold.playbooks import get_playbook

    playbook = get_playbook(name)
    if playbook is None:
        typer.echo(f"Unknown playbook: {name!r}. Run `hivepilot playbooks list`.", err=True)
        raise typer.Exit(1)

    typer.echo(f"{playbook.title}  ({playbook.name})")
    typer.echo(playbook.description)
    typer.echo("")
    typer.echo(f"Flow: {playbook.flow_summary}")
    typer.echo("")
    typer.echo("Files:")
    for rel in sorted(playbook.files):
        typer.echo(f"  {rel}")
    typer.echo("")
    typer.echo(playbook.files["README.md"])


@playbooks_app.command("scaffold")
def playbooks_scaffold_cmd(
    name: str = typer.Argument(..., help="Playbook name"),
    target: Path = typer.Option(
        Path("."), "--target", "-t", help="Deployment config directory to scaffold into"
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing playbook files instead of refusing"
    ),
) -> None:
    """Scaffold a playbook's config fragments into <target>/playbooks/<name>/."""
    from hivepilot.scaffold.playbooks import get_playbook, scaffold_playbook

    if get_playbook(name) is None:
        typer.echo(f"Unknown playbook: {name!r}. Run `hivepilot playbooks list`.", err=True)
        raise typer.Exit(1)

    try:
        written = scaffold_playbook(name, target, force=force)
    except FileExistsError as exc:
        typer.echo(f"Error: {exc}", err=True)
        typer.echo("Pass --force to overwrite existing files.", err=True)
        raise typer.Exit(1)

    typer.echo(f"Scaffolded playbook {name!r} ({len(written)} file(s)):")
    for path in written:
        typer.echo(f"  {path}")


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
    parent_page_id: str = typer.Argument(
        ..., help="Notion page ID to create the runs database under"
    ),
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
        typer.echo(
            f"  {team.get('key', '?'):<10} {team.get('name', '?')}  (id={team.get('id', '?')})"
        )


@linear_app.command("issue")
def linear_issue(
    project: str = typer.Argument(..., help="Project name"),
    task: str = typer.Argument(..., help="Task name"),
    error: str | None = typer.Option(
        None, "--error", "-e", help="Error message to include in issue body"
    ),
    priority: int = typer.Option(
        2, "--priority", "-p", help="Priority: 0=none 1=urgent 2=high 3=medium 4=low"
    ),
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
    team_id: str | None = typer.Option(
        None, "--team-id", help="Team ID (defaults to linear_team_id in config)"
    ),
) -> None:
    """List workflow states for a Linear team."""
    from hivepilot.services.linear_service import get_workflow_states

    resolved_team_id = team_id or settings.linear_team_id
    if not resolved_team_id:
        typer.echo("Error: --team-id required or set HIVEPILOT_LINEAR_TEAM_ID.", err=True)
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


# ---------------------------------------------------------------------------
# Obsidian vault integration
# ---------------------------------------------------------------------------


@obsidian_app.command("audit")
def obsidian_audit(
    vault: str | None = typer.Option(
        None,
        "--vault",
        "-v",
        help="Path to Obsidian vault root (defaults to HIVEPILOT_OBSIDIAN_VAULT setting)",
    ),
) -> None:
    """Scan the Obsidian vault and report present/missing folders and HivePilot subtree status."""

    from hivepilot.services.obsidian_service import ObsidianService

    vault_path = vault or str(settings.obsidian_vault)
    svc = ObsidianService(vault_path=vault_path, dry_run=True)

    try:
        report = svc.audit()
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Vault: {vault_path}\n")

    typer.echo(f"Present folders ({len(report['present'])}):")
    for folder in report["present"]:
        typer.echo(f"  [x] {folder}")

    typer.echo(f"\nMissing folders ({len(report['missing'])}):")
    for folder in report["missing"]:
        typer.echo(f"  [ ] {folder}")

    typer.echo("\nFrozen folders (must not be renamed or deleted):")
    for folder in report["frozen"]:
        typer.echo(f"  {folder}")

    subtree = report["hivepilot_subtree"]
    exists = subtree.get("exists", False)
    typer.echo(f"\n12 - HivePilot subtree: {'exists' if exists else 'MISSING'}")
    for key, val in subtree.items():
        if key == "exists":
            continue
        status = "[x]" if val else "[ ]"
        typer.echo(f"  {status} {key}")


@app.command("debate")
def debate(
    project: str = typer.Argument(..., help="Project to debate within"),
    topic: str = typer.Argument(..., help="Debate topic / decision"),
    role: str = typer.Option(
        "developer",
        "--role",
        help="Dual-model role (default: developer -- the only role guaranteed present "
        "without a custom roles.yaml; see examples/roles.yaml for ceo/cto/etc.)",
    ),
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run", help="Skip vault ADR write (default: dry-run)"
    ),
    simulate: bool = typer.Option(
        False, "--simulate", help="Stub model positions instead of invoking runners"
    ),
    token: str | None = typer.Option(
        None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"
    ),
) -> None:
    """Run a dual-model debate for a role and write the outcome as an ADR."""
    _require_cli_role("run", token)
    orchestrator = Orchestrator()
    adr = orchestrator.run_debate(
        project_name=project, role_name=role, topic=topic, dry_run=dry_run, simulate=simulate
    )
    if adr is None:
        typer.echo("Debate complete — no vault configured, ADR not written.")
    else:
        prefix = "(dry-run) " if adr.get("dry_run") else ""
        typer.echo(f"ADR {prefix}-> {adr.get('path')}")


@app.command("audit")
def audit(
    project: str = typer.Argument(..., help="Project to audit"),
    deep: bool = typer.Option(
        False, "--deep", help="Deep audit: propose improvements to the agent prompts"
    ),
    run_id: int | None = typer.Option(
        None, "--run-id", help="Observe a specific completed run (light mode)"
    ),
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run", help="Skip vault note write (default: dry-run)"
    ),
    token: str | None = typer.Option(
        None, "--token", help="API token", envvar="HIVEPILOT_API_TOKEN"
    ),
) -> None:
    """Run Henri (external auditor): observe a cycle or propose prompt improvements."""
    _require_cli_role("run", token)
    from hivepilot.services import auditor_service

    orch = Orchestrator()
    proj = orch._project(project)
    if deep or run_id is None:
        out = auditor_service.audit(project=proj, registry=orch.registry, dry_run=dry_run)
    else:
        out = auditor_service.observe(
            project=proj, run_id=run_id, registry=orch.registry, dry_run=dry_run
        )
    typer.echo(out[:1000])


@app.command("groups")
def groups_cmd() -> None:
    """List configured component groups (e.g. acme -> its component repos)."""
    groups = load_groups().groups
    if not groups:
        typer.echo("No groups configured.")
        return
    for name, g in groups.items():
        typer.echo(f"{name} ({len(g.components)} components, hub={g.hub or '-'})")
        if g.description:
            typer.echo(f"  {g.description}")
        for c in g.components:
            typer.echo(f"  - {c}")


@app.command("worker")
def worker(
    port: int = typer.Option(settings.worker_port, "--port", help="Port to listen on"),
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Bind address (use 0.0.0.0 to expose; token required)"
    ),
) -> None:
    """Start a HivePilot worker that runs agent steps dispatched by a remote hub."""
    import uvicorn

    from hivepilot.services.worker_service import create_app

    typer.echo(f"HivePilot worker listening on {host}:{port}")
    uvicorn.run(create_app(), host=host, port=port)


@app.command("workers")
def workers(
    check: bool = typer.Option(
        True, "--check/--no-check", help="Ping each worker's /health and refresh status"
    ),
) -> None:
    """List remote HivePilot workers (referenced by role hosts) and their health."""
    from hivepilot.services import state_service, worker_registry

    rows = worker_registry.refresh() if check else state_service.list_workers()
    if not rows:
        typer.echo("No workers configured (set a role host to an http(s):// URL).")
        return
    for w in rows:
        typer.echo(f"{w['status']:<11} {w['url']}  (seen {w.get('last_seen')})")


def _handle_mandatory_agent_verdict(report: agent_checks.MandatoryAgentReport) -> None:
    """Warn (never hard-fail) `hivepilot init` when a mandatory agent CLI is
    missing from PATH.

    `init`'s whole job is to scaffold a working config so you have somewhere
    to install an agent CLI *into* -- hard-failing here is a
    chicken-and-egg regression (you can't have `claude`/`codex`/`vibe` on
    PATH before running `init` on a fresh machine or in CI). Run-time
    enforcement (before an actual pipeline run) is a separate concern and is
    not affected by this function -- see `hivepilot.services.agent_checks`
    for where the mandatory set is defined.

    Emits a stronger warning when none of claude/codex/vibe are present, a
    softer one when only a non-claude agent is present. `claude` is the
    strongest/most-tested prerequisite.
    """
    from hivepilot.services import agent_checks

    if not report.any_ok:
        typer.echo("")
        typer.echo("WARNING: no mandatory agent CLI found on PATH.")
        typer.echo(
            "  HivePilot needs at least one of: "
            f"{', '.join(agent_checks.MANDATORY_AGENTS)} to run pipelines "
            "-- install one before running `hivepilot run`."
        )
        typer.echo("  `claude` is the primary/most-tested prerequisite.")
        typer.echo("  Install hints:")
        typer.echo(
            "    claude : https://docs.claude.com/en/docs/claude-code "
            "(npm i -g @anthropic-ai/claude-code)"
        )
        typer.echo("    codex  : npm i -g @openai/codex")
        typer.echo("    vibe   : see your package manager or vibe's install docs")
        return

    if not report.claude_ok:
        typer.echo("")
        typer.echo(
            "WARNING: 'claude' not found on PATH "
            f"(present: {', '.join(report.present)}). "
            "`claude` is the strongest/most-tested agent CLI -- some features "
            "may be less reliable without it."
        )


def _print_init_outcome(outcome: init_service.InitOutcome) -> None:
    if outcome.mode == "clone":
        if outcome.synced_files:
            typer.echo(f"Synced {len(outcome.synced_files)} file(s):")
            for f in outcome.synced_files:
                typer.echo(f"  {f}")
        else:
            typer.echo("Config repo already up to date.")
        if outcome.target != settings.xdg_config_home:
            typer.echo(
                "Note: `config sync` always targets the XDG config dir "
                f"({settings.xdg_config_home}); --path only controls where .env is written."
            )
    else:
        for result in outcome.scaffold_results:
            typer.echo(f"  {result.action:<11} {result.path}")

    if outcome.env_result:
        typer.echo(f"  {outcome.env_result.action:<11} {outcome.env_result.path}")

    typer.echo("")
    if outcome.validated_target != outcome.target:
        typer.echo(f"Validation (against {outcome.validated_target}):")
    else:
        typer.echo("Validation:")
    for v in outcome.validation:
        status = "OK" if v.ok else "FAILED"
        line = f"  [{status:<6}] {v.name}"
        if not v.ok and v.detail:
            line += f" -- {v.detail}"
        typer.echo(line)

    typer.echo("")
    typer.echo("Next steps:")
    typer.echo(
        f"  1. Edit {outcome.target / '.env'} — fill in secrets (e.g. HIVEPILOT_TELEGRAM_BOT_TOKEN)."
    )
    typer.echo("  2. Run: hivepilot doctor")


@app.command("init")
def init_config(
    config_repo: Optional[str] = typer.Option(
        None,
        "--config-repo",
        help="Git URL or local path to an existing HivePilot config repo. "
        "Forces CLONE mode non-interactively.",
    ),
    path: Optional[Path] = typer.Option(
        None,
        "--path",
        help="Target config directory (default: the resolved XDG config dir, "
        "e.g. ~/.config/hivepilot).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Non-interactive: never prompt. Without --config-repo, scaffolds with defaults.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing config files instead of skipping them."
    ),
) -> None:
    """Onboarding wizard: get a fresh HivePilot install to a working config.

    Two modes:

    \b
    - CLONE mode: you already have a HivePilot config repo. Pass --config-repo
      (or answer "yes" when prompted interactively) and the repo is synced in
      via the existing `hivepilot config sync` logic.
    - SCAFFOLD mode: no existing repo. Minimal valid placeholder config files
      are written locally so `hivepilot validate` / `hivepilot doctor` have
      something to check.

    Non-interactive shells (no TTY) without --yes and without --config-repo
    default to SCAFFOLD mode with defaults -- the least surprising choice for
    first-run automation -- rather than blocking on a prompt nobody can
    answer. Pass --yes explicitly to make that choice unambiguous, or
    --config-repo to clone instead.
    """
    from hivepilot.services import init_service

    target = init_service.resolve_target_dir(path)
    typer.echo(f"Target config directory: {target}")

    interactive = init_service.is_interactive_tty() and not yes and not config_repo

    if interactive:
        import questionary

        has_repo = questionary.confirm(
            "Do you already have a HivePilot config repo?", default=False
        ).ask()
        if has_repo:
            config_repo = (
                config_repo or questionary.text("Config repo git URL or local path:").ask()
            )
            if not config_repo:
                typer.echo("No repo provided -- aborting.", err=True)
                raise typer.Exit(1)

    auto_copy_env = True
    if interactive and not config_repo:
        example_only = (target / ".env.example").exists() and not (target / ".env").exists()
        if example_only:
            import questionary

            auto_copy_env = bool(
                questionary.confirm("Copy .env.example to .env?", default=True).ask()
            )

    outcome = init_service.run_init(
        config_repo=config_repo,
        path=path,
        force=force,
        auto_copy_env=auto_copy_env,
    )

    _print_init_outcome(outcome)
    _handle_mandatory_agent_verdict(outcome.mandatory_agents)


@app.command("validate")
def validate(
    config_dir: Path = typer.Option(Path("."), "--dir", "-d", help="Config directory to validate"),
) -> None:
    """Validate cross-references in a HivePilot config directory."""
    from hivepilot.services.config_validation import validate_config

    problems = validate_config(base_dir=config_dir)
    if not problems:
        typer.echo("OK")
        return

    for problem in problems:
        typer.echo(f"  ERROR  {problem}", err=True)
    raise typer.Exit(1)


def _health_badge(status: str) -> str:
    """Rich-markup-colored badge for a `HealthStatus.status` value — green
    ok / yellow degraded / red error, falling back to plain text for any
    unrecognized value (defensive; `_normalize_health_result` never actually
    produces one)."""
    color = {"ok": "green", "degraded": "yellow", "error": "red"}.get(status)
    return f"[{color}]{status}[/{color}]" if color else status


# Rendering order for the per-plugin "contributes" column in `plugins list`'s
# Loaded Plugins table — mirrors the six `register()` contribution-type keys
# `PluginManager` pops (`hivepilot/plugins.py`) plus lifecycle `hooks`, in a
# stable, deterministic order (not insertion order of the underlying dict).
_CONTRIBUTION_RENDER_ORDER = (
    "runners",
    "notifiers",
    "secrets",
    "health",
    "panels",
    "skills",
    "hooks",
)


def _format_contributions(contributions: dict[str, list[str]]) -> str:
    """Render a `PluginRecord.contributions` dict as a compact one-line
    summary, e.g. `"runners: hugo · hooks: before_step"` — `"-"` when the
    plugin contributed nothing attributable (e.g. its `register()` returned
    `{}`, or the record predates Phase 26a attribution)."""
    parts = [
        f"{kind}: {', '.join(contributions[kind])}"
        for kind in _CONTRIBUTION_RENDER_ORDER
        if contributions.get(kind)
    ]
    return " · ".join(parts) if parts else "-"


def _print_health_table(
    console: Console, plugins: PluginManager, *, title: str = "Health"
) -> dict[str, HealthStatus]:
    """Render the plugin Health table (name / status badge / detail) by
    running every registered health check via `PluginManager.check_all()`
    (never-raise). Returns the raw `{name: HealthStatus}` results so callers
    (e.g. `plugins health`'s exit-code logic) don't need to re-run checks."""
    from rich.table import Table

    health_table = Table(title=title)
    health_table.add_column("name")
    health_table.add_column("status")
    health_table.add_column("detail")

    results = plugins.check_all()
    for name in sorted(results):
        status, detail = results[name]
        health_table.add_row(name, _health_badge(status), detail)
    if not results:
        health_table.add_row("-", "-", "-")
    console.print(health_table)
    return results


@plugins_app.command("list")
def plugins_list() -> None:
    """List loaded plugins and the runner kinds / notifiers / secrets backends
    currently registered.

    v1 simplification: this is an inventory (what's loaded, from where) plus a
    separate list of what runner kinds / notifier names / secrets backends are
    currently registered (built-in vs. plugin-contributed, inferred by
    membership) — not a full join between the runner/notifier/secrets
    taxonomy tables and the Loaded Plugins table. The **Loaded Plugins**
    table itself DOES carry real per-plugin attribution (Phase 26a) via
    `PluginRecord.contributions` — see the "contributes" column below.
    See docs/v4/PLUGINS.md.
    """
    from rich.console import Console
    from rich.table import Table

    from hivepilot.models import KNOWN_RUNNER_KINDS
    from hivepilot.registry import (
        _OPTIONAL_AGENT_PLUGIN_KINDS,
        KNOWN_SECRET_BACKENDS,
        RUNNER_MAP,
        SECRETS_MAP,
    )
    from hivepilot.services.notification_service import KNOWN_NOTIFIER_NAMES, NOTIFIER_MAP

    orchestrator = Orchestrator()
    console = Console(width=200)

    plugins_table = Table(title="Loaded Plugins")
    plugins_table.add_column("name")
    plugins_table.add_column("source")
    plugins_table.add_column("location")
    plugins_table.add_column("contributes")
    for record in orchestrator.plugins.loaded:
        plugins_table.add_row(
            record.name, record.source, record.location, _format_contributions(record.contributions)
        )
    if not orchestrator.plugins.loaded:
        plugins_table.add_row("-", "-", "-", "-")
    console.print(plugins_table)

    # Sprint 5 (runner-defaults-plugins-mode PRD): the agent taxonomy gets its
    # own table, distinct from every other runner kind — built-in agents
    # {claude, codex, vibe, openrouter} (openrouter tagged API-only, no CLI
    # binary) plus every PATH-gated plugin agent (gemini/opencode/ollama/pi/
    # qwen-code/kimi-cli — see hivepilot.registry._OPTIONAL_AGENT_PLUGIN_KINDS,
    # the single source of truth this reuses), each tagged active (flag on +
    # binary on PATH — i.e. currently in RUNNER_MAP) or inactive (flag off,
    # or binary absent), with its per-plugin enable-flag env var so an
    # inactive row is immediately actionable. See docs/v4/PLUGINS.md.
    _builtin_agent_kinds = ("claude", "codex", "vibe", "openrouter")
    _api_only_agent_kinds = frozenset({"openrouter"})

    agents_table = Table(title="Agent Runners")
    agents_table.add_column("kind")
    agents_table.add_column("source")
    agents_table.add_column("status")
    agents_table.add_column("enable flag")
    for kind in _builtin_agent_kinds:
        # Sprint 05 (plugin-arch-overhaul PRD): built-in agent kinds are now
        # individually disable-able (Sprint 01 -- claude_enabled/codex_enabled/
        # vibe_enabled/openrouter_enabled gate hivepilot.registry._BUILTIN_RUNNERS'
        # registration loop). Reflect that live in RUNNER_MAP membership, exactly
        # like every plugin agent kind below, instead of assuming a built-in is
        # always active.
        if kind not in RUNNER_MAP:
            status = "inactive"
        elif kind in _api_only_agent_kinds:
            status = "API-only"
        else:
            status = "active"
        agents_table.add_row(kind, "built-in", status, f"HIVEPILOT_{kind.upper()}_ENABLED")
    for kind in sorted(_OPTIONAL_AGENT_PLUGIN_KINDS):
        flag_name, _binary = _OPTIONAL_AGENT_PLUGIN_KINDS[kind]
        status = "active" if kind in RUNNER_MAP else "inactive"
        agents_table.add_row(kind, "plugin", status, f"HIVEPILOT_{flag_name.upper()}")
    console.print(agents_table)

    _agent_kinds = set(_builtin_agent_kinds) | set(_OPTIONAL_AGENT_PLUGIN_KINDS)
    runners_table = Table(title="Other Runner Kinds")
    runners_table.add_column("kind")
    runners_table.add_column("source")
    for kind in sorted(RUNNER_MAP):
        if kind in _agent_kinds:
            continue  # already covered by the Agent Runners table above
        source = "built-in" if kind in KNOWN_RUNNER_KINDS else "plugin"
        runners_table.add_row(kind, source)
    console.print(runners_table)

    notifiers_table = Table(title="Notifiers")
    notifiers_table.add_column("name")
    notifiers_table.add_column("source")
    for name in sorted(NOTIFIER_MAP):
        source = "built-in" if name in KNOWN_NOTIFIER_NAMES else "plugin"
        notifiers_table.add_row(name, source)
    console.print(notifiers_table)

    secrets_table = Table(title="Secrets Backends")
    secrets_table.add_column("name")
    secrets_table.add_column("source")
    for name in sorted(SECRETS_MAP):
        source = "built-in" if name in KNOWN_SECRET_BACKENDS else "plugin"
        secrets_table.add_row(name, source)
    console.print(secrets_table)

    _print_health_table(console, orchestrator.plugins)


@plugins_app.command("health")
def plugins_health() -> None:
    """Print only the plugin Health table and exit non-zero if any check
    reports `error` — useful for monitoring/CI, unlike `plugins list` (which
    always exits 0)."""
    from rich.console import Console

    orchestrator = Orchestrator()
    console = Console(width=200)

    results = _print_health_table(console, orchestrator.plugins, title="Plugin Health")

    if any(status == "error" for status, _detail in results.values()):
        raise typer.Exit(1)


@plugins_app.command("tui")
def plugins_tui() -> None:
    """Interactive (read-only) Textual browser/inspector for loaded plugins.

    v1 is browse + inspect only — no enable/disable (see docs/v4/PLUGINS.md).
    """
    if not settings.enable_textual_ui:
        typer.echo("Enable HIVEPILOT_ENABLE_TEXTUAL_UI to launch the plugin manager TUI.")
        raise typer.Exit(1)
    try:
        from hivepilot.ui.plugin_manager import PluginManagerApp
    except ImportError as exc:  # pragma: no cover
        raise typer.BadParameter("textual not installed. run `pip install textual`.") from exc

    PluginManagerApp().run()


@plugins_app.command("search")
def plugins_search(
    query: str = typer.Argument("", help="Substring to match against plugin name/description"),
) -> None:
    """Search the configured plugin discovery INDEX (Phase 26b Approach A).

    METADATA ONLY — see docs/v4/PLUGINS.md "Trust model": this fetches a
    small JSON document (name/description/install-hint/checksum) from
    `HIVEPILOT_PLUGINS_INDEX_URL` and displays it. It never downloads or
    executes plugin code; installation stays on your own `pip install` /
    `git clone` (see `plugins info <name>` for the exact command).
    """
    from rich.console import Console
    from rich.markup import escape as rich_escape
    from rich.table import Table

    from hivepilot.services.plugin_index import fetch_index, format_install_hint, search_index

    try:
        entries = fetch_index()
    except RuntimeError as exc:
        typer.echo(f"plugins search: {exc}", err=True)
        raise typer.Exit(1) from exc

    matches = search_index(entries, query)

    console = Console(width=200)
    table = Table(title="Plugin Index")
    table.add_column("name")
    table.add_column("version")
    table.add_column("description")
    table.add_column("install")
    for entry in matches:
        # Every index field is ATTACKER-CONTROLLED (compromised/MITM'd index
        # host) — escape before it ever reaches rich's Table renderer, which
        # otherwise interprets `[...]` as markup (style injection, or a
        # crash on unbalanced tags) even when Rich's own color output is
        # suppressed for a non-terminal. See plugin_index.py's parse-time
        # control-char stripping for the other half of this defense.
        table.add_row(
            rich_escape(entry.name),
            rich_escape(entry.version or "-"),
            rich_escape(entry.description),
            rich_escape(format_install_hint(entry.install)),
        )
    if not matches:
        table.add_row("-", "-", "-", "-")
    console.print(table)


@plugins_app.command("info")
def plugins_info(
    name: str = typer.Argument(..., help="Plugin name as listed in the index"),
) -> None:
    """Show full index metadata for one plugin: description, author,
    homepage, contributes, checksum, and the exact install command to run
    yourself (Phase 26b Approach A).

    METADATA ONLY — this command never installs anything for you (see
    docs/v4/PLUGINS.md "Trust model"). It only prints the `pip install` /
    `git clone` command the operator should run through their own trusted
    path.
    """
    from rich.console import Console
    from rich.markup import escape as rich_escape
    from rich.table import Table

    from hivepilot.services.plugin_index import fetch_index, format_install_hint

    try:
        entries = fetch_index()
    except RuntimeError as exc:
        typer.echo(f"plugins info: {exc}", err=True)
        raise typer.Exit(1) from exc

    entry = next((e for e in entries if e.name.lower() == name.lower()), None)
    if entry is None:
        typer.echo(f"plugins info: no plugin named {name!r} found in the index", err=True)
        raise typer.Exit(1)

    orchestrator = Orchestrator()
    installed = any(record.name == entry.name for record in orchestrator.plugins.loaded)

    # Every index field is ATTACKER-CONTROLLED (compromised/MITM'd index
    # host) — escape before it ever reaches rich's Table renderer. See
    # plugin_index.py's parse-time control-char stripping for the other
    # half of this defense, and `format_install_hint`'s own allow-list
    # validation for the install command specifically.
    console = Console(width=200)
    table = Table(title=f"Plugin: {rich_escape(entry.name)}")
    table.add_column("field")
    table.add_column("value")
    table.add_row("name", rich_escape(entry.name))
    table.add_row("version", rich_escape(entry.version or "-"))
    table.add_row("description", rich_escape(entry.description))
    table.add_row("author", rich_escape(entry.author or "-"))
    table.add_row("homepage", rich_escape(entry.homepage or "-"))
    table.add_row(
        "contributes",
        rich_escape(", ".join(entry.contributes) if entry.contributes else "-"),
    )
    table.add_row("checksum", rich_escape(entry.checksum or "-"))
    table.add_row("installed locally", "yes" if installed else "no")
    table.add_row(
        "install command",
        rich_escape(f"To install, run: {format_install_hint(entry.install)}"),
    )
    console.print(table)


@skills_app.command("list")
def skills_list() -> None:
    """List every registered plugin-contributed skill (skill-plugin-type PRD,
    Sprint 5): name, description, contributing plugin's `provider`, and
    which runner kinds it `applies_to` ("any" when unset -- see
    `hivepilot/plugins.py`'s `SkillSpec` docstring).

    Mirrors `plugins list`'s rendering style. Sourced from
    `PluginManager.list_skills()`, which is never-raise-free of side
    effects beyond the `PluginManager()` construction itself.
    """
    from rich.console import Console
    from rich.table import Table

    orchestrator = Orchestrator()
    console = Console(width=200)

    skills_table = Table(title="Skills")
    skills_table.add_column("name")
    skills_table.add_column("description")
    skills_table.add_column("provider")
    skills_table.add_column("applies_to")
    skills = orchestrator.plugins.list_skills()
    for skill in skills:
        applies_to = skill.get("applies_to")
        applies_to_display = ", ".join(applies_to) if applies_to else "any"
        skills_table.add_row(
            skill["name"], skill["description"], skill["provider"], applies_to_display
        )
    if not skills:
        skills_table.add_row("-", "-", "-", "-")
    console.print(skills_table)


if __name__ == "__main__":
    app()
