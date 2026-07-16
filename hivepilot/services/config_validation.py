"""Cross-reference validation for a HivePilot config directory.

Loads the six core YAML files and checks that every identifier referenced
in one file exists in the file that defines it.  Returns a list of problem
strings; an empty list means the config is consistent.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import yaml

from hivepilot.config import settings
from hivepilot.services import scan_service


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
    explicit_base_dir = base_dir is not None
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

    if explicit_base_dir:
        prompts_dir = base_dir / "prompts" / "agents"
    else:
        prompts_dir = settings.resolve_config_path("prompts") / "agents"

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
            problems.append(f"Group '{group_name}' hub '{hub}' is not defined in projects.yaml")
        # single_repo (monorepo) groups: `components`/`tags` are pure scoping
        # labels, never resolved as projects (targets=[hub] always — see
        # orchestrator._run_pipeline_body), so they are exempt from the
        # "defined in projects.yaml" check below. Only `hub` must be a real
        # project for a single_repo group.
        if not group_def.get("single_repo"):
            for component in group_def.get("components") or []:
                if component not in project_names:
                    problems.append(
                        f"Group '{group_name}' component '{component}' is not "
                        "defined in projects.yaml"
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

    # -----------------------------------------------------------------------
    # Check: every policy's `block_on_severity` (Phase 21 Sprint 2 CVE gate),
    # when set, is one of scan_service.SEVERITY_LEVELS. Checked for the
    # `default` policy plus every per-project override -- a typo here would
    # otherwise only surface as a `ValueError` the first time that project
    # runs (policy_service.get_policy validates the same eagerly), so this
    # gives the same fail-closed guarantee at `config validate` time too.
    # -----------------------------------------------------------------------
    policy_default = (policies_data.get("policies") or {}).get("default") or {}
    _policy_entries: list[tuple[str, dict[str, Any]]] = [("default", policy_default)]
    _policy_entries.extend(policy_projects.items())
    for policy_scope, policy_rules in _policy_entries:
        if not isinstance(policy_rules, dict):
            continue
        block_on_severity = policy_rules.get("block_on_severity")
        if block_on_severity is not None and block_on_severity not in scan_service.SEVERITY_LEVELS:
            problems.append(
                f"Policy '{policy_scope}' has invalid block_on_severity "
                f"{block_on_severity!r}. Must be one of {scan_service.SEVERITY_LEVELS} or unset."
            )

    # -----------------------------------------------------------------------
    # Check: dangling inputs (PRD A2 Sprint 3).
    #
    # Data-flow graph: pipeline stage -> task (tasks.yaml `role:`) -> role
    # (roles.yaml `inputs`/`outputs`). Walk each pipeline's stages in
    # declared order, accumulating the set of output keys produced so far;
    # any stage whose role declares an `inputs` key that is not yet in that
    # accumulated set is a "dangling input" -- nothing upstream in this
    # pipeline produces it.
    #
    # Severity is intentionally NOT a flat hard-error like the checks above:
    # existing roles.yaml declares `inputs` cosmetically on every role (e.g.
    # `developer` lists `architecture_docs`/`codebase_context`, which no role
    # `outputs`) purely as human-readable documentation of what a role reads.
    # In `context_routing_mode="full"` (default) that's harmless -- the
    # orchestrator still builds prior_context from ALL prior chunks, so a
    # dangling input never changes behaviour -- and making it a hard
    # `problems` entry would break `config validate` for every pre-existing
    # config, including the bundled Noxys config. So:
    #   - "full" (default): surfaced as a WARNING via `warnings.warn`, NOT
    #     appended to `problems` -- `config validate` still exits 0/"OK".
    #   - "keyed": appended to `problems` as a hard error, because
    #     `_route_prior_context` (orchestrator.py) then actually narrows a
    #     stage's prior context to just its declared input keys, so a
    #     dangling input silently degrades that stage to the conservative
    #     whole-blob fallback instead of the data it expects.
    # This mirrors how the CLI's `config validate` command (hivepilot/cli.py)
    # only inspects the returned `problems` list -- warnings never gate it.
    # -----------------------------------------------------------------------
    role_by_name: dict[str, dict[str, Any]] = {
        r["name"]: r for r in (roles_data.get("roles") or []) if isinstance(r, dict) and "name" in r
    }
    keyed_mode = settings.context_routing_mode == "keyed"
    for pipeline_name, pipeline in (pipelines_data.get("pipelines") or {}).items():
        available_outputs: set[str] = set()
        for stage in pipeline.get("stages") or []:
            task_ref = stage.get("task")
            task_def = (tasks_data.get("tasks") or {}).get(task_ref) if task_ref else None
            role_ref = task_def.get("role") if isinstance(task_def, dict) else None
            role_def = role_by_name.get(role_ref) if role_ref else None
            if role_def is None:
                continue
            optional_inputs = set(role_def.get("optional_inputs") or [])
            for input_key in role_def.get("inputs") or []:
                if input_key in optional_inputs:
                    continue
                if input_key not in available_outputs:
                    message = (
                        f"Pipeline '{pipeline_name}' stage '{stage.get('name', '?')}' "
                        f"input '{input_key}' is not produced by any earlier stage's outputs "
                        "(dangling input)"
                    )
                    if keyed_mode:
                        problems.append(message)
                    else:
                        warnings.warn(f"[config validate] {message}", stacklevel=2)
            available_outputs.update(role_def.get("outputs") or [])

    # -----------------------------------------------------------------------
    # Check: every pipeline stage's `only_tags` values are defined in at
    # least one group's tags (groups.yaml).  There is no statically-bound
    # group per pipeline, so "defined in at least one group" is the correct
    # static rule here; the runtime fail-closed check in orchestrator.py
    # (`_validate_stage_tags`) enforces per-run group membership.
    # -----------------------------------------------------------------------
    all_group_tags: set[str] = set()
    for group_def in (groups_data.get("groups") or {}).values():
        if not isinstance(group_def, dict):
            continue
        all_group_tags.update((group_def.get("tags") or {}).keys())

    for pipeline_name, pipeline in (pipelines_data.get("pipelines") or {}).items():
        for stage in pipeline.get("stages") or []:
            for tag in stage.get("only_tags") or []:
                if tag not in all_group_tags:
                    problems.append(
                        f"Pipeline '{pipeline_name}' stage '{stage.get('name', '?')}' "
                        f"references only_tags '{tag}' not defined in any group's "
                        f"tags (groups.yaml)"
                    )

    # -----------------------------------------------------------------------
    # Check: every `skills:` entry (TaskStep.skills / PipelineStage.skills,
    # skill-plugin-type PRD Sprint 3) references a REGISTERED plugin skill,
    # and -- when that skill declares a `min_role` -- the referencing task's
    # `role:` satisfies it.
    #
    # Dormancy gate: `skills` is absent from every task step and pipeline
    # stage in the overwhelming majority of configs today. First collect
    # every reference as a cheap dict-walk (no plugin work at all); only if
    # at least one reference exists do we construct a `PluginManager()` (a
    # real scan of the plugins/ dir + entry points, with process-global side
    # effects on RUNNER_MAP/NOTIFIER_MAP/SECRETS_MAP -- see
    # tests/conftest.py::_isolate_runner_and_notifier_maps). This keeps a
    # no-skills config byte-identical: no new problems AND no new
    # construction/side effects (see
    # test_no_skills_config_is_byte_identical_to_pre_sprint3_behavior).
    #
    # "The step's resolved role" is the owning TASK's `role:` value --
    # TaskStep has no per-step role of its own; role is bound once per task
    # and shared by every step in it (mirrors how `resolve_runner(task.role,
    # policy)` resolves execution in orchestrator.py). A PipelineStage's
    # resolved role is its referenced task's role.
    #
    # A skill with NO `min_role` is intentionally public/ungated (see
    # `hivepilot/plugins.py` SkillSpec docstring -- unlike PanelSpec there is
    # no implicit default). Never conflate "not yet gated" with
    # "intentionally public": only a skill that explicitly declares
    # `min_role` is checked below.
    #
    # Fail-closed, mirroring `api_service.get_panel_endpoint` /
    # authz-config-fail-closed: `token_service.role_rank()` returns -1 for
    # any name it doesn't recognize. If EITHER the skill's `min_role` OR the
    # resolved task role is unrecognized (rank -1), that is a hard
    # validation error, never a silent pass -- a `-1 < -1` or `-1 < N`
    # comparison must never be allowed to evaluate as "satisfied". This also
    # means today's agent-persona role names (ceo, developer, reviewer, ...)
    # can never satisfy a `min_role` gate, because they are a different
    # namespace than `token_service.ROLE_RANKS` (read/run/approve/admin) --
    # see Sprint 3 Agent Notes for the explicit design decision.
    # -----------------------------------------------------------------------
    skill_refs: list[tuple[str, list[str], str | None]] = []
    for task_name, task_def in (tasks_data.get("tasks") or {}).items():
        if not isinstance(task_def, dict):
            continue
        task_role = task_def.get("role")
        for step in task_def.get("steps") or []:
            if not isinstance(step, dict):
                continue
            step_skills = step.get("skills")
            if step_skills:
                skill_refs.append(
                    (
                        f"Task '{task_name}' step '{step.get('name', '?')}'",
                        list(step_skills),
                        task_role,
                    )
                )

    for pipeline_name, pipeline in (pipelines_data.get("pipelines") or {}).items():
        for stage in pipeline.get("stages") or []:
            stage_skills = stage.get("skills")
            if stage_skills:
                task_ref = stage.get("task")
                stage_task_def = (tasks_data.get("tasks") or {}).get(task_ref) if task_ref else None
                stage_role = (
                    stage_task_def.get("role") if isinstance(stage_task_def, dict) else None
                )
                skill_refs.append(
                    (
                        f"Pipeline '{pipeline_name}' stage '{stage.get('name', '?')}'",
                        list(stage_skills),
                        stage_role,
                    )
                )

    if skill_refs:
        from hivepilot.plugins import PluginManager
        from hivepilot.services import token_service

        plugin_manager = PluginManager()
        for owner_label, skill_names, resolved_role in skill_refs:
            for skill_name in skill_names:
                skill = plugin_manager.get_skill(skill_name)
                if skill is None:
                    problems.append(f"{owner_label} references unknown skill '{skill_name}'")
                    continue
                min_role = skill.get("min_role")
                if not min_role:
                    continue
                required_rank = token_service.role_rank(min_role)
                actual_rank = (
                    token_service.role_rank(resolved_role) if isinstance(resolved_role, str) else -1
                )
                if required_rank < 0 or actual_rank < 0 or actual_rank < required_rank:
                    problems.append(
                        f"{owner_label} uses skill '{skill_name}' which requires "
                        f"min_role '{min_role}', but its resolved role "
                        f"{resolved_role!r} does not satisfy it"
                    )

    return problems
