"""Cross-reference validation for a HivePilot config directory.

Loads the six core YAML files and checks that every identifier referenced
in one file exists in the file that defines it.  Returns a list of problem
strings; an empty list means the config is consistent.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class ValidationReport:
    """Full result of `validate_config_report()` -- `problems` is the exact
    pre-existing `list[str]` contract `validate_config()` has always
    returned (kept as a thin wrapper around this for backward
    compatibility with every existing caller/test). `config_dir`,
    `plugin_dirs` and `skill_dirs` are ADDITIONAL, resolved-to-absolute
    context so `hivepilot validate` can print, at the top of its output,
    exactly what it read and where it looked for plugins/skills -- the fix
    for the "validate silently follows the process cwd instead of --dir"
    bug: an operator seeing this header can never again mistake a
    cwd-dependent verdict for flakiness.

    `skill_dirs` is the SECOND skill source (`<root>/skills/<name>/SKILL.md`
    directories -- see `hivepilot/skill_dirs.py`), listed separately from
    `plugin_dirs` because the two are genuinely different roots. Both lists
    are named in an unknown-skill problem's "searched: ..." suffix: naming
    only a subset of the roots actually consulted is precisely what made the
    original "config repo skills are silently ignored" bug so hard to
    diagnose on a live box.
    """

    problems: list[str] = field(default_factory=list)
    config_dir: Path = field(default_factory=Path.cwd)
    plugin_dirs: list[Path] = field(default_factory=list)
    skill_dirs: list[Path] = field(default_factory=list)


def validate_config(base_dir: Path | None = None) -> list[str]:
    """Validate cross-references in a HivePilot config directory.

    Thin wrapper around `validate_config_report()` returning just its
    `problems` list -- the pre-existing contract every caller in this
    codebase (and dozens of tests) relies on. Use `validate_config_report`
    directly when the resolved config/plugin directories are also needed
    (e.g. `hivepilot validate`'s CLI header).
    """
    return validate_config_report(base_dir=base_dir).problems


def validate_config_report(base_dir: Path | None = None) -> ValidationReport:
    """Validate cross-references in a HivePilot config directory, and
    report exactly which directories were read/scanned along the way.

    Parameters
    ----------
    base_dir:
        Directory containing the config files. When explicitly provided,
        every required file (and `prompts/agents/`) is resolved LITERALLY
        relative to it -- this is the "validate this exact directory in
        isolation" contract that `config_writer._validate_prospective`'s
        scratch-copy validation, `init_service.run_init`'s target-dir
        validation, and `hivepilot validate --dir X` all rely on: the
        directory being validated is deliberately NOT (yet, or ever) the
        globally-active config, so it must never be shadowed by XDG/
        config_repo state.
        When omitted (the default), every required file AND the prompts
        directory are instead resolved through the same XDG-aware
        `settings.resolve_config_path()` chain the runtime config loaders
        use (`project_service.load_projects/tasks/pipelines/groups`) --
        1. $XDG_CONFIG_HOME/hivepilot/<file>, 2. config_repo/<file>,
        3. settings.base_dir/<file> -- so "validate the config that's
        actually active" (e.g. after `hivepilot config sync`, which writes
        to XDG, never to cwd) reflects reality instead of always reading
        cwd literally.

        This same `explicit_base_dir` split now ALSO governs plugin/skill
        discovery (`hivepilot/plugins.py` `PluginManager`/`plugin_scan_dirs`)
        for the `skills:` cross-reference check below -- previously
        `PluginManager()` always read the process-global `settings.base_dir`
        (`default_factory=Path.cwd`) regardless of `base_dir`, so validating
        a config repo from any cwd OTHER than that repo silently reported
        every `skills:` reference as "unknown skill" (the confirmed
        `hivepilot validate --dir` cwd bug). An explicit `base_dir` is now
        threaded straight into `PluginManager(base_dir=base_dir)`, mirroring
        the config-files split exactly: isolated when explicit, the
        pre-existing global-settings-derived behavior otherwise.

    Returns
    -------
    ValidationReport
        `problems`: list of problem descriptions (empty means everything is
        consistent). `config_dir`: the absolute directory the six required
        config files were actually read from (the explicit `base_dir` when
        given; otherwise the resolved location of the first of them,
        representative of the XDG -> config_repo -> base_dir chain).
        `plugin_dirs`: every absolute directory plugin/skill discovery
        looked in (see `plugin_scan_dirs`) -- populated even when no
        `skills:` reference exists anywhere in this config, since computing
        it is pure path arithmetic with no filesystem scanning or
        `PluginManager` construction (see the dormancy-gate note on the
        `skills:` check below). `skill_dirs`: the same, for the OTHER skill
        source -- `<root>/skills/<name>/SKILL.md` directories (see
        `hivepilot.skill_dirs.skill_scan_dirs`). Both lists are named in an
        unknown-skill problem's "searched: ..." suffix.
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
    resolved_required_paths: list[Path] = []
    for filename in required_files:
        if explicit_base_dir:
            path = base_dir / filename
        else:
            path = settings.resolve_config_path(filename)
        resolved_required_paths.append(path)
        if not path.exists():
            problems.append(f"Missing required config file: {filename}")
            data[filename] = None
        else:
            data[filename] = _load(path)

    # The directory this run actually read its config from -- `base_dir`
    # itself when explicit (the isolated-directory contract); otherwise the
    # resolved location of the first required file, representative of
    # whichever XDG -> config_repo -> base_dir tier `resolve_config_path`
    # picked (every real deployment keeps all six files together in the
    # same tier, so "first file's parent" is never misleading in practice).
    config_dir = (base_dir if explicit_base_dir else resolved_required_paths[0].parent).resolve()

    # Every directory plugin/skill discovery would look in for THIS run --
    # cheap path arithmetic (see `plugin_scan_dirs`'s docstring), computed
    # unconditionally so `hivepilot validate`'s header is accurate even for
    # a config with no `skills:` reference at all (which never constructs a
    # real `PluginManager` -- see the dormancy-gate note below).
    from hivepilot.plugins import plugin_scan_dirs

    plugin_dirs = [
        d.resolve() for d in plugin_scan_dirs(base_dir=base_dir if explicit_base_dir else None)
    ]

    # Same, for the OTHER skill source: `<root>/skills/<name>/SKILL.md`
    # directories (`hivepilot/skill_dirs.py`). Also pure path arithmetic, so
    # it is computed unconditionally alongside `plugin_dirs` and stays
    # accurate for a config with no `skills:` reference at all.
    from hivepilot.skill_dirs import skill_scan_dirs

    skill_dirs = [
        d.resolve() for d in skill_scan_dirs(base_dir=base_dir if explicit_base_dir else None)
    ]

    # -----------------------------------------------------------------------
    # Collect defined names
    # -----------------------------------------------------------------------
    projects_data = data.get("projects.yaml") or {}
    project_names: set[str] = set((projects_data.get("projects") or {}).keys())

    roles_data = data.get("roles.yaml") or {}
    role_names: set[str] = {r["name"] for r in (roles_data.get("roles") or []) if "name" in r}

    tasks_data = data.get("tasks.yaml") or {}
    task_names: set[str] = set((tasks_data.get("tasks") or {}).keys())

    # NOTE: this explicit_base_dir/else split is now mirrored by required_files
    # above (previously only prompts_dir did this -- the inconsistency that
    # let `Missing required config file` false-positive after a `config sync`
    # to XDG). Left AS IS here, deliberately NOT collapsed to always use
    # resolve_config_path: `config_writer._validate_prospective` builds a
    # scratch copy of a config dir (including its own `prompts/` subtree) and
    # calls `validate_config(base_dir=tmp_dir)` specifically so a prospective
    # `prompt_file` reference is checked against THAT scratch copy, not the
    # globally-active prompts dir -- collapsing this branch would silently
    # validate the wrong (currently-active) prompts directory instead of the
    # one actually being proposed, breaking `config set`/`config edit`'s
    # pre-write validation gate.
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
        # `plugins.enable` scopes lifecycle hooks and nothing else. A runner
        # is chosen explicitly by a task (`runner: rtk`), so it never fires
        # unbidden and needs no pipeline-level opt-out -- which makes
        # `enable: [rtk]` inert, though an operator who wrote it plainly
        # believed otherwise. Reported, not rejected: the line is harmless,
        # but leaving it silent lets something that looks load-bearing hold
        # nothing up.
        enable = ((pipeline.get("plugins") or {}).get("enable")) or []
        if enable:
            from hivepilot.models import unscoped_plugin_names
            from hivepilot.plugins import PluginManager

            manager = PluginManager()
            hook_plugins = {
                manager.hook_owner[id(fn)]
                for fns in manager.hooks.values()
                for fn in fns
                if id(fn) in manager.hook_owner
            }
            inert = unscoped_plugin_names(list(enable), hook_plugins=hook_plugins)
            if inert:
                problems.append(
                    f"Pipeline '{pipeline_name}' plugins.enable lists "
                    f"{inert} which contribute no lifecycle hook — "
                    "`enable` scopes before_step/after_step only, so these "
                    "entries have no effect (runners are selected per task)"
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
    #
    # Fail-closed (dangling-prompt_file sprint): a non-mapping role entry and
    # an empty-or-whitespace-only `prompt_file` used to be silently skipped
    # (`continue` / falsy-check) -- "I could not inspect this" must be a
    # reported problem, never silence, mirroring the governing principle
    # `hivepilot config doctor` already applies elsewhere in this codebase.
    # -----------------------------------------------------------------------
    for index, role_def in enumerate(roles_data.get("roles") or []):
        if not isinstance(role_def, dict):
            problems.append(
                f"Role entry at index {index} is not a mapping (got "
                f"{type(role_def).__name__}) -- its prompt_file could not be checked"
            )
            continue
        if "prompt_file" not in role_def:
            continue
        prompt_file = role_def.get("prompt_file")
        role_label = f"Role '{role_def.get('name', '?')}'"
        if not isinstance(prompt_file, str):
            problems.append(
                f"{role_label} has an invalid prompt_file (must be a non-empty string, got "
                f"{type(prompt_file).__name__})"
            )
            continue
        if not prompt_file.strip():
            problems.append(f"{role_label} has an empty-or-whitespace-only prompt_file")
            continue
        resolved = prompts_dir / prompt_file
        if not resolved.exists():
            problems.append(f"{role_label} prompt_file '{prompt_file}' not found at {resolved}")

    # -----------------------------------------------------------------------
    # Check: every task step's `prompt_file` resolves through the SAME chain
    # the runtime uses to load it. Unlike a role's `prompt_file` (checked
    # above -- resolved relative to `prompts/agents/`, via `prompts_dir`),
    # a TaskStep's `prompt_file` is resolved VERBATIM by
    # `Settings.resolve_config_path`, with NO `prompts/agents/` prefix --
    # see `ClaudeRunner._assemble_prompt` / `PromptCliRunner._load_prompt`
    # (hivepilot/runners/claude_runner.py, hivepilot/runners/
    # prompt_cli_runner.py), both of which call
    # `self.settings.resolve_config_path(step.prompt_file)` verbatim. This
    # check calls that EXACT method (never reimplements the search order)
    # so it can never disagree with what actually happens at run time.
    #
    # Real incident: an operator's tasks.yaml referenced
    # `prompt_file: security_review.md` with no such file anywhere on the
    # box. Both `hivepilot validate` and `hivepilot config doctor` reported
    # zero findings; the failure only surfaced at RUNTIME
    # (`FileNotFoundError: Prompt file not found: /security_review.md` --
    # the service's cwd=/ base_dir tier), after the operator had already
    # launched the pipeline and waited. This check closes that gap.
    #
    # Fail-closed (non-negotiable per this sprint): a non-mapping task/step
    # entry, a non-list `steps`, and an empty-or-whitespace-only
    # `prompt_file` are all reported as problems, never silently skipped --
    # a config this check "could not inspect" must never look clean.
    # -----------------------------------------------------------------------
    for task_name, task_def in (tasks_data.get("tasks") or {}).items():
        if not isinstance(task_def, dict):
            problems.append(
                f"Task '{task_name}' is not a mapping (got {type(task_def).__name__}) -- "
                "its steps could not be checked for dangling prompt_file references"
            )
            continue
        steps = task_def.get("steps")
        if steps is None:
            continue
        if not isinstance(steps, list):
            problems.append(
                f"Task '{task_name}' key 'steps' is not a list (got {type(steps).__name__}) -- "
                "its prompt_file references could not be checked"
            )
            continue
        for step_index, step in enumerate(steps):
            if not isinstance(step, dict):
                problems.append(
                    f"Task '{task_name}' step index {step_index} is not a mapping (got "
                    f"{type(step).__name__}) -- its prompt_file reference could not be checked"
                )
                continue
            if "prompt_file" not in step:
                continue
            prompt_file = step.get("prompt_file")
            step_label = f"Task '{task_name}' step '{step.get('name', '?')}'"
            if not isinstance(prompt_file, str):
                problems.append(
                    f"{step_label} has an invalid prompt_file (must be a non-empty string, "
                    f"got {type(prompt_file).__name__})"
                )
                continue
            if not prompt_file.strip():
                problems.append(f"{step_label} has an empty-or-whitespace-only prompt_file")
                continue
            if explicit_base_dir:
                step_resolved = base_dir / prompt_file
                step_searched = [str(base_dir)]
            else:
                step_resolved = settings.resolve_config_path(prompt_file)
                step_searched = [str(d) for d in settings.config_path_search_dirs()]
            if not step_resolved.exists():
                problems.append(
                    f"{step_label} prompt_file '{prompt_file}' not found "
                    f"(resolved: {step_resolved}; searched: {', '.join(step_searched)})"
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
    # Check: every policy's `denied_licenses`/`allowed_licenses` (license-
    # compliance gate), when set, is a NON-EMPTY list of non-empty strings.
    # Mirrors the `block_on_severity` check immediately above for the same
    # fail-closed-at-`config validate`-time reasoning (`policy_service.
    # get_policy` validates the same eagerly at load time). An empty list
    # is rejected, not just a non-list/bad-entry: `[]` is falsy, so the
    # orchestrator's gate-enable check (`denied_licenses or allowed_licenses`)
    # would otherwise silently treat an intentional-but-empty `allowed_licenses:
    # []` (meaning "allow nothing") as "gate disabled" instead of blocking
    # every run — the opposite of what an empty allowlist should mean.
    # -----------------------------------------------------------------------
    for policy_scope, policy_rules in _policy_entries:
        if not isinstance(policy_rules, dict):
            continue
        for field_name in ("denied_licenses", "allowed_licenses"):
            value = policy_rules.get(field_name)
            if value is None:
                continue
            if (
                not isinstance(value, list)
                or not value
                or not all(isinstance(entry, str) and entry.strip() for entry in value)
            ):
                problems.append(
                    f"Policy '{policy_scope}' has invalid {field_name} "
                    f"{value!r}. Must be a NON-EMPTY list of non-empty strings, or omit "
                    "the key to disable the gate (an empty list is ambiguous: an empty "
                    "allowlist would block all runs, an empty denylist is a no-op)."
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
    #     stage's prior context to just its declared input keys. A dangling
    #     required input is absent from the store (the producer is not
    #     allowed to invent it). If every required key is absent the stage
    #     falls back to full prior context; if some are present it runs on
    #     the incomplete keyed subset. Neither is the contract the role
    #     declared.
    # This mirrors how the CLI's `config validate` command (hivepilot/cli.py)
    # only inspects the returned `problems` list -- warnings never gate it.
    # -----------------------------------------------------------------------
    role_by_name: dict[str, dict[str, Any]] = {
        r["name"]: r for r in (roles_data.get("roles") or []) if isinstance(r, dict) and "name" in r
    }
    keyed_mode = settings.context_routing_mode == "keyed"
    for pipeline_name, pipeline in (pipelines_data.get("pipelines") or {}).items():
        # Everything ANY stage of this pipeline produces, regardless of order.
        # This is what separates the two cases the check used to conflate:
        #
        #   producer absent from the pipeline  -> a composition choice. `forage`
        #       has no CTO because it IS the pipeline without a planning phase;
        #       calling that a "dangling input" reads as a config mistake and
        #       trains the operator to ignore the message.
        #   producer present but ordered LATER -> a real ordering fault, and the
        #       severity below is right for it.
        #
        # `optional_inputs` already exists, but on the ROLE: marking
        # `technical_spec` optional there would silence it in `noxys` too, where
        # the CTO does run and the input genuinely should be present. The
        # distinction has to be per pipeline.
        produced_anywhere: set[str] = set()
        for _stage in pipeline.get("stages") or []:
            _task = (tasks_data.get("tasks") or {}).get(_stage.get("task"))
            _role = _task.get("role") if isinstance(_task, dict) else None
            _rdef = role_by_name.get(_role) if _role else None
            if _rdef:
                produced_anywhere.update(_rdef.get("outputs") or [])

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
                    stage_name = stage.get("name", "?")
                    if input_key in produced_anywhere:
                        # The pipeline HAS a producer and runs it too late.
                        message = (
                            f"Pipeline '{pipeline_name}' stage '{stage_name}' consumes "
                            f"'{input_key}', but the stage producing it runs LATER in this "
                            "pipeline -- reorder them"
                        )
                        if keyed_mode:
                            problems.append(message)
                        else:
                            warnings.warn(f"[config validate] {message}", stacklevel=2)
                    else:
                        # No stage in this pipeline produces it at all. Say what
                        # that MEANS for the run rather than labelling the config
                        # broken; a pipeline may legitimately omit the producer
                        # (`forage` has no CTO by design).
                        #
                        # But the SEVERITY still depends on the routing mode, and
                        # softening that was a mistake in the first attempt: in
                        # `keyed` mode `_route_prior_context` narrows a stage to
                        # its declared keys, so an input nobody produces is
                        # simply absent — or, if every required key is absent,
                        # the stage falls back to full prior context. Neither
                        # matches the role's contract. Absent-producer is
                        # benign only in `full` mode, where prior context is
                        # built from every prior chunk anyway.
                        message = (
                            f"Pipeline '{pipeline_name}' stage '{stage_name}' will run "
                            f"WITHOUT '{input_key}': no stage in this pipeline produces "
                            "it. Intentional if this pipeline deliberately omits that "
                            "role"
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

        # `base_dir=base_dir if explicit_base_dir else None` -- the cwd bug
        # fix: an explicit `--dir` must scan plugins/skills from THAT
        # directory, never from `Path.cwd()` (`PluginManager`'s pre-fix
        # default). Omitted (`None`), it's byte-identical to before: the
        # process-global `settings.base_dir` chain. See this function's own
        # docstring and `plugin_scan_dirs` for the full semantics.
        plugin_manager = PluginManager(base_dir=base_dir if explicit_base_dir else None)
        # EVERY root actually consulted, plugin roots and skill roots alike,
        # de-duplicated but order-preserving. An operator debugging an
        # unknown-skill error follows this list literally, so a root that is
        # searched but not named here sends them down a false trail (the
        # confirmed "config repo skills silently ignored" incident: the
        # message named only `<cwd>/plugins` and the installed-plugins dir).
        searched_roots: list[str] = []
        for directory in [*plugin_dirs, *skill_dirs]:
            text = str(directory)
            if text not in searched_roots:
                searched_roots.append(text)
        searched_dirs = ", ".join(searched_roots) or "(no plugin or skill directories)"
        for owner_label, skill_names, resolved_role in skill_refs:
            for skill_name in skill_names:
                skill = plugin_manager.get_skill(skill_name)
                if skill is None:
                    problems.append(
                        f"{owner_label} references unknown skill '{skill_name}' "
                        f"(searched: {searched_dirs})"
                    )
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

    return ValidationReport(
        problems=problems,
        config_dir=config_dir,
        plugin_dirs=plugin_dirs,
        skill_dirs=skill_dirs,
    )
