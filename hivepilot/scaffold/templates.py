"""Scaffold a fresh generic HivePilot deployment config directory.

Call ``scaffold_config(target_dir)`` to generate all required YAML files and
prompt stubs.  The generated files are neutral placeholders — not tied to any
specific deployment — so operators can customise them before running
``hivepilot validate``.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Template content strings
# ---------------------------------------------------------------------------

_PROJECTS_YAML = """\
# HivePilot projects configuration.
# Each key is a unique project name used throughout pipelines, groups, and policies.
projects:
  example-project:
    path: ~/dev/example-project
    description: Replace with your project description.
    claude_md: CLAUDE.md
    default_branch: main
    owner_repo: my-org/example-project
    env:
      PYTHONUNBUFFERED: "1"

  example-frontend:
    path: ~/dev/example-frontend
    description: Replace with your frontend project description.
    default_branch: main
    owner_repo: my-org/example-frontend
"""

_ROLES_YAML = """\
# HivePilot agent role definitions.
# prompt_file paths are relative to prompts/agents/.
roles:
  - name: planner
    display_name: "Planner"
    title: "Planner"
    prompt_file: "planner.md"
    can_block: false
    order: 1
    runner: "claude"

  - name: developer
    display_name: "Developer"
    title: "Developer"
    prompt_file: "developer.md"
    can_block: false
    order: 2
    runner: "claude"
    permission_mode: "bypassPermissions"

  - name: reviewer
    display_name: "Reviewer"
    title: "Reviewer"
    prompt_file: "reviewer.md"
    can_block: true
    order: 3
    runner: "claude"
"""

_POLICIES_YAML = """\
# Per-project policy overrides.
policies:
  default:
    allow_auto_git: true
    require_approval: false
    allow_containers: false
  projects:
    example-project:
      allow_auto_git: true
      require_approval: false
      allow_containers: false
"""

_GROUPS_YAML = """\
# Component groups — a logical product made of multiple repos.
# The `hub` is where group-level planning runs.
groups:
  example-group:
    description: Example group — all components of a product.
    hub: example-project
    components:
      - example-frontend
"""

_PIPELINES_YAML = """\
# HivePilot pipeline definitions.
# Each pipeline is an ordered list of stages; each stage maps to a task in tasks.yaml.
pipelines:
  example-pipeline:
    description: A simple plan → develop → review cycle.
    stages:
      - name: Planning
        task: plan-task
      - name: Implementation
        task: develop-task
        pause_before: true
      - name: Review
        task: review-task
"""

_TASKS_YAML = """\
# HivePilot task definitions.
tasks:
  plan-task:
    role: planner
    description: Planner produces a technical spec.
    steps:
      - name: plan
        runner: claude
        prompt_file: prompts/agents/planner.md
        timeout_seconds: 3600
    git:
      commit: false
      push: false
      create_pr: false

  develop-task:
    role: developer
    description: Developer implements the spec.
    steps:
      - name: implement
        runner: claude
        prompt_file: prompts/agents/developer.md
        timeout_seconds: 5400
    git:
      commit: true
      push: true
      create_pr: false
      commit_message: "feat: automated implementation"
      branch_prefix: hivepilot

  review-task:
    role: reviewer
    description: Reviewer inspects the implementation.
    steps:
      - name: review
        runner: claude
        prompt_file: prompts/agents/reviewer.md
        timeout_seconds: 3600
    git:
      commit: false
      push: false
      create_pr: true
      pr_title: "HivePilot: automated implementation"
      branch_prefix: hivepilot
"""

_ENV_EXAMPLE = """\
# HivePilot environment — copy to .env (gitignored) and fill in values.
# All settings use the HIVEPILOT_ prefix (pydantic-settings).

# --- Paths ---
# HIVEPILOT_OBSIDIAN_VAULT=/home/you/vault
# HIVEPILOT_STATE_DB=state.db

# --- External binaries (defaults shown) ---
# HIVEPILOT_GH_COMMAND=gh
# HIVEPILOT_GIT_COMMAND=git
# HIVEPILOT_CLAUDE_COMMAND=claude

# --- Telegram bot (optional) ---
HIVEPILOT_TELEGRAM_BOT_TOKEN=
HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS=[]
# HIVEPILOT_TELEGRAM_NOTIFICATION_CHAT_ID=

# --- Other notification webhooks (optional) ---
# HIVEPILOT_SLACK_BOT_TOKEN=
# HIVEPILOT_DISCORD_BOT_TOKEN=

# --- ChatOps / API (optional) ---
# HIVEPILOT_CHATOPS_TOKEN=
# HIVEPILOT_API_TOKEN=
"""

_PLANNER_PROMPT = """\
# Planner

You are the Planner agent. Your job is to analyse the task objective and produce
a clear, actionable technical specification for the Developer to implement.

## Inputs
- Objective description
- Any relevant context from the codebase

## Outputs
- Technical specification document
- List of files to create or modify
- Acceptance criteria

## Instructions
1. Read the objective carefully.
2. Identify ambiguities and state your assumptions.
3. Produce a concise technical spec — avoid padding.
4. End with a numbered acceptance-criteria list.
"""

_DEVELOPER_PROMPT = """\
# Developer

You are the Developer agent. Your job is to implement the technical specification
produced by the Planner.

## Inputs
- Technical specification
- Existing codebase context

## Outputs
- Implementation (code changes)
- Test suite
- Brief implementation notes

## Instructions
1. Read the technical spec in full before writing any code.
2. Follow the project's coding conventions.
3. Write tests alongside the implementation.
4. Keep the implementation focused — do not gold-plate.
"""

_REVIEWER_PROMPT = """\
# Reviewer

You are the Reviewer agent. Your job is to assess the implementation against the
technical specification and produce an actionable review report.

## Inputs
- Implementation diff
- Technical specification
- Test results

## Outputs
- Review report (approved / changes-requested)
- List of issues found (CRITICAL / HIGH / MEDIUM / LOW)

## Instructions
1. Check the implementation against every acceptance criterion.
2. Flag security issues as CRITICAL.
3. If all criteria are met and no CRITICAL issues exist, output APPROVED.
4. Otherwise output CHANGES REQUESTED with a numbered list of issues.
"""

# ---------------------------------------------------------------------------
# Mapping of relative path → content
# ---------------------------------------------------------------------------

_FILES: dict[str, str] = {
    "projects.yaml": _PROJECTS_YAML,
    "roles.yaml": _ROLES_YAML,
    "policies.yaml": _POLICIES_YAML,
    "groups.yaml": _GROUPS_YAML,
    "pipelines.yaml": _PIPELINES_YAML,
    "tasks.yaml": _TASKS_YAML,
    ".env.example": _ENV_EXAMPLE,
    "prompts/agents/planner.md": _PLANNER_PROMPT,
    "prompts/agents/reviewer.md": _REVIEWER_PROMPT,
    "prompts/agents/developer.md": _DEVELOPER_PROMPT,
}


def scaffold_config(target_dir: Path, force: bool = False) -> list[Path]:
    """Scaffold a generic HivePilot deployment config into *target_dir*.

    Parameters
    ----------
    target_dir:
        Directory to write files into.  Created if it does not exist.
    force:
        When ``True``, overwrite files that already exist.  When ``False``
        (default), raise :exc:`FileExistsError` if any target file is present.

    Returns
    -------
    list[Path]
        Absolute paths of every file that was written.

    Raises
    ------
    FileExistsError
        If ``force=False`` and one or more target files already exist.
    """
    target_dir = Path(target_dir)

    # Check for conflicts before writing anything
    if not force:
        conflicts = [target_dir / rel for rel in _FILES if (target_dir / rel).exists()]
        if conflicts:
            raise FileExistsError(
                "Files already exist (use force=True to overwrite): "
                + ", ".join(str(p) for p in conflicts)
            )

    created: list[Path] = []
    for rel, content in _FILES.items():
        dest = target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        created.append(dest)

    return created
