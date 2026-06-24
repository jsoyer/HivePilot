"""Cross-reference validation for a HivePilot config directory.

Loads the six core YAML files and checks that every identifier referenced
in one file exists in the file that defines it.  Returns a list of problem
strings; an empty list means the config is consistent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _load(path: Path) -> Any:
    """Load a YAML file and return the parsed object, or None on error."""
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        return None
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error in {path}: {exc}") from exc


def validate_config(base_dir: Path | None = None) -> list[str]:
    """Validate cross-references in a HivePilot config directory.

    Parameters
    ----------
    base_dir:
        Directory containing the config files.  Defaults to ``Path.cwd()``.

    Returns
    -------
    list[str]
        A list of problem descriptions.  Empty means everything is consistent.
    """
    if base_dir is None:
        base_dir = Path.cwd()

    problems: list[str] = []

    # -----------------------------------------------------------------------
    # Load all files
    # -----------------------------------------------------------------------
    required_files = [
        "projects.yaml",
        "roles.yaml",
        "policies.yaml",
        "groups.yaml",
        "pipelines.yaml",
        "tasks.yaml",
    ]
    data: dict[str, Any] = {}
    for filename in required_files:
        path = base_dir / filename
        if not path.exists():
            problems.append(f"Missing required config file: {filename}")
            data[filename] = None
        else:
            data[filename] = _load(path)

    # -----------------------------------------------------------------------
    # Collect defined names
    # -----------------------------------------------------------------------
    projects_data = data.get("projects.yaml") or {}
    project_names: set[str] = set((projects_data.get("projects") or {}).keys())

    roles_data = data.get("roles.yaml") or {}
    role_names: set[str] = {r["name"] for r in (roles_data.get("roles") or []) if "name" in r}

    tasks_data = data.get("tasks.yaml") or {}
    task_names: set[str] = set((tasks_data.get("tasks") or {}).keys())

    prompts_dir = base_dir / "prompts" / "agents"

    # -----------------------------------------------------------------------
    # Check: every pipeline stage's `task` exists in tasks.yaml
    # -----------------------------------------------------------------------
    pipelines_data = data.get("pipelines.yaml") or {}
    for pipeline_name, pipeline in (pipelines_data.get("pipelines") or {}).items():
        for stage in pipeline.get("stages") or []:
            task_ref = stage.get("task")
            if task_ref and task_ref not in task_names:
                problems.append(
                    f"Pipeline '{pipeline_name}' stage '{stage.get('name', '?')}' "
                    f"references unknown task '{task_ref}'"
                )

    # -----------------------------------------------------------------------
    # Check: every task's `role` exists in roles.yaml
    # -----------------------------------------------------------------------
    for task_name, task_def in (tasks_data.get("tasks") or {}).items():
        if not isinstance(task_def, dict):
            continue
        role_ref = task_def.get("role")
        if role_ref and role_ref not in role_names:
            problems.append(f"Task '{task_name}' references unknown role '{role_ref}'")

    # -----------------------------------------------------------------------
    # Check: every group's `hub` and `components` exist in projects.yaml
    # -----------------------------------------------------------------------
    groups_data = data.get("groups.yaml") or {}
    for group_name, group_def in (groups_data.get("groups") or {}).items():
        if not isinstance(group_def, dict):
            continue
        hub = group_def.get("hub")
        if hub and hub not in project_names:
            problems.append(
                f"Group '{group_name}' hub '{hub}' is not defined in projects.yaml"
            )
        for component in group_def.get("components") or []:
            if component not in project_names:
                problems.append(
                    f"Group '{group_name}' component '{component}' "
                    f"is not defined in projects.yaml"
                )

    # -----------------------------------------------------------------------
    # Check: every role's `prompt_file` resolves (file exists)
    # -----------------------------------------------------------------------
    for role_def in roles_data.get("roles") or []:
        if not isinstance(role_def, dict):
            continue
        prompt_file = role_def.get("prompt_file")
        if prompt_file:
            resolved = prompts_dir / prompt_file
            if not resolved.exists():
                problems.append(
                    f"Role '{role_def.get('name', '?')}' prompt_file "
                    f"'{prompt_file}' not found at {resolved}"
                )

    # -----------------------------------------------------------------------
    # Check: every policy project name exists in projects.yaml
    # -----------------------------------------------------------------------
    policies_data = data.get("policies.yaml") or {}
    policy_projects = (policies_data.get("policies") or {}).get("projects") or {}
    for project_key in policy_projects.keys():
        if project_key not in project_names:
            problems.append(
                f"Policy entry for project '{project_key}' is not defined in projects.yaml"
            )

    return problems
