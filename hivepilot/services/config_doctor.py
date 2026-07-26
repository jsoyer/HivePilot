"""``hivepilot config doctor`` — a single actionable health report over the
active HivePilot configuration + plugin surface.

Every check below maps to a real production incident (see the sprint spec
that introduced this file); the mapping is repeated on each check function
so the connection between "why does this check exist" and "what incident
would this have caught" stays discoverable from the code itself:

  * ``describe_resolved_paths`` / ``check_cwd_relative_paths`` — incident #1
    (cwd-relative ``state.db`` differed between a service at ``cwd=/`` and a
    CLI run from an operator's home directory; the topics registry had the
    same bug and is now fixed via ``xdg_data_home``, verified here too).
  * ``check_sync_drift`` — incident #3 (editing a config-repo clone doesn't
    do anything until ``hivepilot config sync`` copies it into the ACTIVE
    XDG config; ``hivepilot validate`` without ``--dir`` reads the active
    config, not the clone, which used to mislead operators).
  * ``check_enabled_plugins_loaded`` — incident #4 (``mem0``/``headroom``
    were enabled via env flags with the plugin FILES missing from the
    plugins dir; the only symptom was an empty dashboard panel). Incident
    #4b (signal-to-noise follow-up, same check): against a real production
    box this reported 19 ERRORs, 17 of which were flags that default to
    ``True`` as a PERMISSION GATE (never touched by the operator) rather
    than an explicit opt-in -- see ``_is_setting_explicit``. Only a flag
    that was EXPLICITLY configured (env var / ``.env`` file / init kwarg)
    still yields an ERROR; an untouched default-``True`` flag is folded
    into a single aggregated ``info`` line instead.
  * ``check_plugin_health`` — incident #5 (a plugin's own ``health()``
    check catches "not installed" / broken-dependency states, but nothing
    surfaced them outside ``plugins health`` -- folded into the single
    doctor report here).
  * ``check_dangling_references`` (+ ``_check_schedules_dangling`` /
    ``_check_role_overrides_dangling`` / ``_check_only_modules_dangling``)
    — incident #7 (a removed project left behind in ``policies.yaml``; a
    stage's ``only_tags``/``only_modules`` referencing something no group/
    project defines) and incident #6 (a role key that is only a Telegram
    command alias, e.g. ``"cos"`` instead of the real key
    ``chief_of_staff`` -- ``get_role()`` looks up the real key only).
  * ``check_secrets_sanity`` — incident #7's secrets cousin: a
    ``${secret:NAME}`` reference with no catalog entry, and the recurring
    "empty string treated as configured" fail-open class for secret-typed
    Settings fields.
  * ``check_role_display_name_collisions`` — incident #8 (five roles all
    carried ``display_name: "Margaux"``; the Telegram agent registry
    derives its addressing alias from ``display_name``, so four of the
    five became unaddressable by name in chat channels -- the engine
    already logs a ``telegram.agent_registry.alias_collision`` warning at
    startup, but nobody reads startup logs).

``run_doctor()`` is the single entry point ``hivepilot config doctor``
(``hivepilot/cli.py``) calls; every other function here is independently
unit-testable and returns a list of ``DoctorFinding`` (empty means "no
problem found for this check").

The second half of this module (``verify_plugins`` and friends) powers
``hivepilot plugins verify`` — incident #5's dedicated command.
"""

from __future__ import annotations

import importlib
import importlib.metadata as importlib_metadata
import os
import platform
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from hivepilot.config import settings
from hivepilot.services.config_provenance import all_keys, is_secret_field
from hivepilot.services.config_validation import validate_config
from hivepilot.services.plugin_installer import KNOWN_EXAMPLE_PLUGINS
from hivepilot.services.secret_refs import find_secret_refs, has_secret_ref

# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

_SEVERITIES = ("info", "warning", "error")


@dataclass(frozen=True)
class DoctorFinding:
    """One health-report entry: WHAT is wrong, WHY it matters, and the exact
    command/edit to fix it. ``severity`` is one of ``_SEVERITIES``; only
    ``"error"`` findings make ``hivepilot config doctor`` exit non-zero."""

    severity: str
    check: str
    message: str
    why: str
    fix: str

    def render(self) -> str:
        badge = {"error": "ERROR", "warning": "WARN ", "info": "INFO "}[self.severity]
        return (
            f"[{badge}] ({self.check}) {self.message}\n"
            f"         why: {self.why}\n"
            f"         fix: {self.fix}"
        )


def _finding(severity: str, check: str, message: str, why: str, fix: str) -> DoctorFinding:
    if severity not in _SEVERITIES:
        # `assert` is stripped under `python -O`; a bad severity must be
        # caught here, not silently let a bogus badge crash `render()`'s
        # dict lookup later (L1).
        raise ValueError(f"invalid severity {severity!r}")
    return DoctorFinding(severity=severity, check=check, message=message, why=why, fix=fix)


# ---------------------------------------------------------------------------
# Shared raw-YAML loading -- mirrors config_validation.py's explicit-base_dir
# vs resolve_config_path split, kept local (not imported) so `validate`'s
# existing checks/messages stay completely untouched (additive only).
# ---------------------------------------------------------------------------


def _doctor_path(filename: str, config_dir: Path | None) -> Path:
    if config_dir is not None:
        return config_dir / filename
    return settings.resolve_config_path(filename)


def _load_yaml_checked(path: Path) -> tuple[dict[str, Any], list[DoctorFinding]]:
    """Load *path* as YAML, returning ``({}, [])`` only when the file is
    simply ABSENT (an already-covered case elsewhere -- e.g.
    ``validate_config``'s ``required_files`` check).

    Never collapse an UNPARSEABLE file (H3) or a parseable-but-non-mapping
    root (M1) into a silent ``{}`` the way the old ``_load_yaml_or_empty``
    did: that made a broken ``schedules.yaml`` (which isn't in
    ``validate_config``'s ``required_files``) produce a clean "OK -- no
    issues found" instead of a diagnostic. The governing rule for this whole
    module is that "I could not inspect this" must be an emitted finding,
    never silence -- so both failure modes return an `error` finding
    alongside the empty dict for the caller to fold into its own list.
    """
    if not path.exists():
        return {}, []
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        return {}, [
            _finding(
                "error",
                "unparseable_config_yaml",
                f"'{path.name}' could not be parsed as YAML ({type(exc).__name__}) -- "
                "no checks could run against it",
                "a YAML syntax error in a config file silently disables EVERY doctor "
                "check that reads it -- schedules.yaml in particular is not in "
                "validate_config's required_files, so nothing else covers it: a broken "
                "schedules.yaml used to yield zero findings and a clean exit",
                f"run `hivepilot validate` for the exact parse error, then fix the YAML "
                f"syntax in {path.name}",
            )
        ]
    if data is None:
        return {}, []
    if not isinstance(data, dict):
        return {}, [
            _finding(
                "error",
                "invalid_config_yaml_root",
                f"'{path.name}' parses as YAML but its root is a "
                f"{type(data).__name__}, not a mapping -- no checks could run against it",
                "every check in this module expects a top-level mapping (e.g. "
                "`projects: {...}`); a list/scalar root would otherwise raise "
                "AttributeError on the first `.get(...)` call and kill the whole report",
                f"fix {path.name} so its top-level document is a mapping",
            )
        ]
    return data, []


def _checked_container(
    data: dict[str, Any], key: str, where: str
) -> tuple[dict[str, Any], list[DoctorFinding]]:
    """Return ``(data.get(key) or {}, [])`` when that value is a mapping (or
    absent); otherwise return ``({}, [finding])`` instead of letting the
    caller's `.keys()`/`.items()`/`.values()` call raise `AttributeError` and
    silently discard every OTHER finding already computed in the same doctor
    run.

    N1 (2nd Opus review, PR #334): `_load_yaml_checked` only guarantees a
    mapping ROOT -- the next level down (e.g. ``projects:``, ``tasks:``,
    ``schedules:``, ``policies:``, ``pipelines:``, a project's ``modules:``)
    was assumed to be a mapping with no check at all. A ``projects.yaml``
    written as a LIST of projects is realistic: ``roles.yaml`` genuinely IS
    written as a list elsewhere in this same config schema.

    *where* is a fully-formed, human-readable location string (e.g.
    ``"'projects.yaml' key 'projects'"``) so the emitted message/fix read
    naturally without this helper needing to know the caller's file layout.
    """
    value = data.get(key)
    if value is None:
        return {}, []
    if not isinstance(value, dict):
        return {}, [
            _finding(
                "error",
                "invalid_config_section",
                f"{where} must be a mapping, got {type(value).__name__} -- not checked",
                "a list/scalar where a mapping is expected raises AttributeError on the "
                "first .keys()/.items()/.values() call downstream, aborting every OTHER "
                "finding already computed in the same doctor run",
                f"fix {where} so it is a mapping",
            )
        ]
    return value, []


# ---------------------------------------------------------------------------
# Check: resolved absolute paths + cwd-relative warning (incident #1)
# ---------------------------------------------------------------------------

# (label, settings-ATTRIBUTE-name, override-env-var, xdg-chain-aware)
# The second element MUST be the real `Settings` attribute name, resolved via
# `getattr(settings, attr)` below -- NEVER a hardcoded filename string (H1: a
# hardcoded "state_db" silently diverged from the real default `Path
# ("state.db")`, and "obsidian_vault" from the real default
# `Path("obsidian-vault")`, making this command print a state-db path that
# disagreed with the pre-existing `hivepilot doctor` command). xdg_chain-aware
# fields are resolved via the same XDG -> config_repo -> base_dir chain
# `config sync` writes to (mirrored locally in `_walk_xdg_rank`); the others
# (state_db, obsidian_vault) have NO xdg-aware loader anywhere in the
# codebase and always resolve via settings.resolve_path -- i.e. base_dir.
_PATH_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("state_db", "state_db", "HIVEPILOT_STATE_DB", False),
    ("prompts_dir", "prompts_dir", "HIVEPILOT_PROMPTS_DIR", True),
    ("obsidian_vault", "obsidian_vault", "HIVEPILOT_OBSIDIAN_VAULT", False),
)


def _base_dir_pinned() -> bool:
    """True if the operator explicitly pinned ``base_dir`` via env (the
    documented fix for incident #1) rather than letting it silently default
    to ``Path.cwd()`` at process start -- which differs between a service
    started at ``cwd=/`` and a CLI invocation from an operator's home dir.

    H2: a merely-non-empty ``HIVEPILOT_BASE_DIR`` is NOT enough -- ``
    resolve_path`` is ``(self.base_dir / path).expanduser().resolve()``, so a
    RELATIVE ``HIVEPILOT_BASE_DIR`` (e.g. ``.``) still anchors every path to
    the process's cwd; it must be an absolute path to actually pin anything.
    This mirrors the per-field override check a few lines below, which
    already required ``.is_absolute()``."""
    raw = os.environ.get("HIVEPILOT_BASE_DIR", "").strip()
    return bool(raw) and Path(raw).expanduser().is_absolute()


def _walk_xdg_rank(filename: str) -> tuple[Path, int]:
    """Mirror `config_provenance._walk_provenance`'s tier walk (kept local to
    avoid depending on that module's underscored helper across files)."""
    xdg_candidate = settings.xdg_config_home / filename
    if xdg_candidate.exists():
        return xdg_candidate, 1
    local_repo = settings._config_repo_local_path()
    if local_repo is not None:
        candidate = local_repo / filename
        if candidate.exists():
            return candidate, 2
    return settings.resolve_path(Path(filename)), 3


def describe_resolved_paths() -> list[str]:
    """Always-printed, human-readable ``label: absolute/path`` lines for the
    paths incident #1 concerns -- never gated on a problem existing (this is
    the "print explicitly" half of the check, independent of the warning)."""
    from hivepilot.services.notification_service import _topics_registry_path

    lines = [
        f"config dir       : {settings.xdg_config_home}",
        f"config repo clone: {settings.xdg_data_home / 'config-repo'}",
    ]
    for label, settings_attr, _env, xdg_aware in _PATH_FIELDS:
        raw_path = getattr(settings, settings_attr)
        if xdg_aware:
            resolved, _rank = _walk_xdg_rank(str(raw_path))
        else:
            resolved = settings.resolve_path(raw_path)
        lines.append(f"{label:<17}: {resolved}")
    lines.append(f"topics registry  : {_topics_registry_path()}")
    return lines


def check_cwd_relative_paths() -> list[DoctorFinding]:
    """WARN for every resolved path in `_PATH_FIELDS` that would differ
    between two processes started with different working directories (the
    exact incident #1 failure mode) -- i.e. it resolves through the
    `base_dir` fallback tier, `base_dir` was never explicitly pinned via
    `HIVEPILOT_BASE_DIR`, and no absolute per-field override is set."""
    findings: list[DoctorFinding] = []
    for label, settings_attr, override_env, xdg_aware in _PATH_FIELDS:
        override_value = os.environ.get(override_env)
        if override_value and Path(override_value).expanduser().is_absolute():
            continue  # explicitly pinned for this field -- never cwd-relative
        if xdg_aware:
            raw_path = getattr(settings, settings_attr)
            _resolved, rank = _walk_xdg_rank(str(raw_path))
            if rank in (1, 2):
                continue  # anchored to XDG or the config repo clone, not cwd
        if _base_dir_pinned():
            continue
        findings.append(
            _finding(
                "warning",
                "cwd_relative_path",
                f"'{label}' resolves relative to the process's cwd at startup "
                f"(base_dir={settings.base_dir!s}, no {override_env} override, no "
                "HIVEPILOT_BASE_DIR pin)",
                "a service started at cwd=/ and a CLI run from an operator's home "
                "directory resolve this to two DIFFERENT files -- this is exactly how "
                "an approval created by a CLI-launched pipeline became invisible to "
                "the Telegram bot (incident #1)",
                f"set HIVEPILOT_BASE_DIR=<absolute path> in the shared env every "
                f"service sources, or set {override_env}=<absolute path> for this "
                "field specifically",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Check: active (XDG) config vs config-repo clone drift (incident #3)
# ---------------------------------------------------------------------------


def check_sync_drift() -> list[DoctorFinding]:
    if not settings.config_repo:
        return []

    from hivepilot.services import config_service

    clone_dir = config_service._config_dir()
    if not clone_dir.exists():
        return []

    diffs: list[str] = []
    for name in sorted(config_service.CONFIG_FILES):
        src_file = clone_dir / name
        if not src_file.exists():
            continue
        dst_file = settings.xdg_config_home / name
        if not dst_file.exists() or src_file.read_bytes() != dst_file.read_bytes():
            diffs.append(name)

    for dir_name in sorted(config_service.CONFIG_DIRS):
        src_dir = clone_dir / dir_name
        if not src_dir.exists():
            continue
        for src_file in sorted(src_dir.rglob("*")):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(src_dir)
            dst_file = settings.xdg_config_home / dir_name / rel
            if not dst_file.exists() or src_file.read_bytes() != dst_file.read_bytes():
                diffs.append(f"{dir_name}/{rel}")

    if not diffs:
        return []

    shown = ", ".join(diffs[:10]) + ("..." if len(diffs) > 10 else "")
    return [
        _finding(
            "error",
            "config_repo_out_of_sync",
            f"The config-repo clone differs from the ACTIVE config in {len(diffs)} "
            f"file(s): {shown}",
            "`hivepilot validate` (no --dir) and every runtime loader read the ACTIVE "
            "config (XDG_CONFIG_HOME), never the repo clone directly -- editing the "
            "clone alone does nothing until it's synced",
            "run `hivepilot config sync` to apply the clone's changes to the active config",
        )
    ]


# ---------------------------------------------------------------------------
# Check: enabled-but-missing plugins (incident #4)
# ---------------------------------------------------------------------------

# Settings fields ending "_enabled" that gate something OTHER than a
# `plugins/<stem>.py` local-file plugin: the three true built-in agent
# runners (checked dynamically below, via hivepilot.registry._BUILTIN_RUNNERS
# intersected with "*_enabled" fields, so a future builtin needs no edit
# here) plus a short, manually-curated list of feature flags that happen to
# share the "*_enabled" naming convention without being a plugin at all.
_NON_PLUGIN_ENABLED_FLAG_EXCEPTIONS = frozenset(
    {
        "plugins",  # master plugin-loading switch, not a plugin itself
        "obsidian_recall",  # sub-flag of the `obsidian` plugin, not its own file
        "chatops_concierge",  # built-in NL feature flag, not a plugin
        "stage_cache",  # SQLite stage-memoization feature flag, not a plugin
    }
)


def _is_setting_explicit(settings_obj: Any, key: str) -> bool | None:
    """Whether Settings field *key* was EXPLICITLY provided (an env var, the
    `.env` config file, or an init kwarg) as opposed to resolved purely from
    its class default.

    `config_provenance.resolve_with_provenance` was investigated first (it
    already powers `hivepilot config list`'s provenance column). Its
    `xdg_rank`/`source_path` are only computed for *file-backed* fields --
    those whose name ends in `_file`, resolved through the XDG ->
    config_repo -> base_dir chain (see `config_provenance._is_file_backed`).
    A boolean `*_enabled` flag is not file-backed, so `resolve_with_provenance`
    always reports `xdg_rank=0` for it regardless of whether the operator
    ever touched it -- it cannot answer this question for this class of
    field, so it is not used here.

    Pydantic v2 `BaseSettings` already tracks exactly the right thing:
    `model_fields_set` on an instance contains only the field names an
    actual source (env var, `.env` file, or an init kwarg) supplied. A field
    resolved purely via the class default is never added to it -- this is a
    definitive, built-in answer, not a heuristic, and needs no new
    bookkeeping. (Directly setting an attribute on the instance, as some
    tests/callers do, also lands it in `model_fields_set` -- pydantic
    treats any assignment as an explicit set, which is the correct
    behaviour here too.)

    Returns ``True`` (explicit), ``False`` (left at the class default), or
    ``None`` when this cannot be determined at all (e.g. *settings_obj* is
    not a real pydantic model). Callers MUST treat ``None`` as "report it
    anyway" (at most degraded to a warning, never silence) -- this codebase
    has repeatedly shipped the bug class of an empty/unknown value being
    treated as "no constraint" and failing open.
    """
    fields_set = getattr(settings_obj, "model_fields_set", None)
    if isinstance(fields_set, (set, frozenset)):
        return key in fields_set

    # Fallback (the primary mechanism above is unavailable): compare the
    # live value against the field's declared default. This is a heuristic,
    # not definitive -- an operator who explicitly set a flag to the SAME
    # value as its default is indistinguishable from "never touched" this
    # way. A mismatch is still unambiguous evidence of an explicit override
    # (`True`); a match is genuinely ambiguous and must NOT be reported as a
    # confirmed default (`False`) -- return `None` (unknown) so the caller
    # degrades to a warning instead of going silent.
    try:
        field_default = type(settings_obj).model_fields[key].default
    except (AttributeError, KeyError, TypeError):
        return None
    live_value = getattr(settings_obj, key, None)
    if live_value != field_default:
        return True
    return None


def check_enabled_plugins_loaded(plugin_manager: Any) -> list[DoctorFinding]:
    from hivepilot.registry import _BUILTIN_RUNNERS

    builtin_runner_stems = frozenset(_BUILTIN_RUNNERS)
    loaded_names = {record.name for record in plugin_manager.loaded}

    findings: list[DoctorFinding] = []
    default_enabled_not_loaded: list[str] = []
    for key in all_keys():
        if not key.endswith("_enabled"):
            continue
        stem = key[: -len("_enabled")]
        if stem in _NON_PLUGIN_ENABLED_FLAG_EXCEPTIONS or stem in builtin_runner_stems:
            continue
        if not getattr(settings, key, False):
            continue
        if stem in loaded_names:
            continue

        explicit = _is_setting_explicit(settings, key)
        if explicit is False:
            # Left at its class default (True for every flag this branch
            # can reach -- see the _NON_PLUGIN_ENABLED_FLAG_EXCEPTIONS/
            # builtin skip and the `not getattr(...)` skip for False flags
            # above): a PERMISSION GATE ("activate this plugin IF its
            # file/binary is present"), not an assertion that the operator
            # opted in. Reporting each one as its own ERROR produced 17
            # false positives against a real production config (herdr/
            # infisical/kms/onepassword/gemini/codex/cursor/hugo/tmux/
            # bitwarden/vaultwarden/opencode/ollama/pi/qwen_code/kimi_cli/
            # antigravity) and buried the 2 real findings under noise --
            # aggregate instead of dropping silent (signal-to-noise IS the
            # feature).
            default_enabled_not_loaded.append(stem)
            continue

        # explicit is True (definitively opted in) or None (provenance
        # unknown). Never let "unknown" collapse to silence: degrade to a
        # warning instead -- the operator may well have opted in and we
        # simply couldn't prove it.
        severity = "error" if explicit else "warning"
        expected = settings.resolve_path(Path("plugins") / f"{stem}.py")
        installed_dir = settings.xdg_data_home / "plugins" / f"{stem}.py"
        install_hint = (
            f"`hivepilot plugins install {stem}`"
            if stem in KNOWN_EXAMPLE_PLUGINS
            else "a plugin file"
        )
        findings.append(
            _finding(
                severity,
                "plugin_enabled_not_loaded",
                f"'{stem}' is enabled ({key}=true) but no such plugin is loaded",
                "an *_enabled flag with no matching loaded plugin silently degrades to a "
                "missing feature (e.g. an empty dashboard panel) with no error anywhere "
                "in normal operation",
                f"add the plugin file at {expected} or {installed_dir} (fetch it with "
                f"{install_hint}), or set {key}=false",
            )
        )

    if default_enabled_not_loaded:
        names = ", ".join(sorted(default_enabled_not_loaded))
        findings.append(
            _finding(
                "info",
                "default_enabled_plugin_not_installed",
                f"{len(default_enabled_not_loaded)} default-enabled optional plugin(s) are "
                f"not installed: {names}",
                "these `*_enabled` flags default to True as a permission gate (activate the "
                "plugin IF its file/binary is present), not an assertion that the operator "
                "opted in -- treating each one as its own ERROR produced 17 false positives "
                "against a real production config and buried the 2 real findings",
                "this is normal for plugins you have not installed; install one with "
                "`hivepilot plugins install <name>` if you want it, otherwise ignore this line",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Check: plugin dependency health surfaced (incident #5)
# ---------------------------------------------------------------------------


def check_plugin_health(plugin_manager: Any) -> list[DoctorFinding]:
    findings: list[DoctorFinding] = []
    results = plugin_manager.check_all()
    for name in sorted(results):
        status, detail = results[name]
        if status == "ok":
            continue
        severity = "error" if status == "error" else "warning"
        spec = KNOWN_EXAMPLE_PLUGINS.get(name)
        if spec is not None and spec.prereq_kind == "pip":
            fix = f"install the missing dependency: {spec.prereq_detail}"
        elif spec is not None and spec.prereq_kind == "binary":
            fix = f"install {spec.prereq_detail}"
        else:
            fix = f"see the plugin's own health detail: {detail}"
        findings.append(
            _finding(
                severity,
                "plugin_health",
                f"plugin '{name}' health check reports {status}: {detail}",
                "a plugin whose dependency is broken silently degrades (e.g. a recall/store "
                "hook becomes a no-op) with no visible error during a normal pipeline run",
                fix,
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Check: dangling references (incident #7, plus #6's alias variant)
# ---------------------------------------------------------------------------


def _check_schedules_dangling(config_dir: Path | None) -> list[DoctorFinding]:
    schedules_data, findings = _load_yaml_checked(_doctor_path("schedules.yaml", config_dir))
    projects_data, project_findings = _load_yaml_checked(_doctor_path("projects.yaml", config_dir))
    tasks_data, task_findings = _load_yaml_checked(_doctor_path("tasks.yaml", config_dir))
    findings.extend(project_findings)
    findings.extend(task_findings)

    # N1: guard the SECOND-level container too -- `_load_yaml_checked` only
    # guarantees a mapping ROOT; `projects:`/`tasks:`/`schedules:` were
    # assumed to be mappings with no check, and a list there (realistic --
    # `roles.yaml` genuinely IS a list) used to raise AttributeError on
    # `.keys()`/`.items()` and crash the whole doctor report.
    projects_section, projects_section_findings = _checked_container(
        projects_data, "projects", "'projects.yaml' key 'projects'"
    )
    findings.extend(projects_section_findings)
    tasks_section, tasks_section_findings = _checked_container(
        tasks_data, "tasks", "'tasks.yaml' key 'tasks'"
    )
    findings.extend(tasks_section_findings)
    schedules_section, schedules_section_findings = _checked_container(
        schedules_data, "schedules", "'schedules.yaml' key 'schedules'"
    )
    findings.extend(schedules_section_findings)

    project_names: set[str] = set(projects_section.keys())
    task_names: set[str] = set(tasks_section.keys())

    for schedule_name, schedule in schedules_section.items():
        if not isinstance(schedule, dict):
            # M3: a schedule entry that isn't a mapping (e.g. a YAML typo
            # like `nightly: "my-task"` instead of a mapping) used to be
            # skipped with ZERO output -- surface it instead.
            findings.append(
                _finding(
                    "error",
                    "malformed_schedule_entry",
                    f"Schedule '{schedule_name}' is not a mapping (got "
                    f"{type(schedule).__name__}) -- not checked",
                    "a schedule entry that isn't a mapping was previously skipped with "
                    "zero output, silently disabling the dangling-task/dangling-project "
                    "checks for it",
                    f"fix the '{schedule_name}' entry in schedules.yaml to be a mapping "
                    "with 'task'/'projects' keys",
                )
            )
            continue
        task_ref = schedule.get("task")
        if task_ref and task_ref not in task_names:
            findings.append(
                _finding(
                    "error",
                    "dangling_schedule_task",
                    f"Schedule '{schedule_name}' references unknown task '{task_ref}'",
                    "a schedule with a dangling task only fails when the scheduler tick "
                    "actually fires it -- silent until then",
                    f"add task '{task_ref}' to tasks.yaml, or fix/remove schedules.yaml "
                    f"entry '{schedule_name}'",
                )
            )
        for project_ref in schedule.get("projects") or []:
            if project_ref not in project_names:
                findings.append(
                    _finding(
                        "error",
                        "dangling_schedule_project",
                        f"Schedule '{schedule_name}' references unknown project '{project_ref}'",
                        "a schedule targeting a removed/renamed project silently fails at "
                        "run time instead of at config-check time",
                        f"add project '{project_ref}' to projects.yaml, or fix/remove it "
                        f"from schedules.yaml entry '{schedule_name}'",
                    )
                )
    return findings


def _alias_to_role_map() -> dict[str, str]:
    """Invert `telegram_bot._CURATED_ALIASES` (alias -> real role key) --
    the authoritative source of which short names are Telegram command
    aliases, not real `roles.yaml` keys (incident #6: `"cos"` is an alias
    for `"chief_of_staff"`, and `get_role("cos")` always raises)."""
    from hivepilot.services.telegram_bot import _CURATED_ALIASES

    mapping: dict[str, str] = {}
    for role_key, aliases in _CURATED_ALIASES.items():
        for alias in aliases:
            if alias != role_key:
                mapping.setdefault(alias, role_key)
    return mapping


def _check_role_overrides_dangling(config_dir: Path | None) -> list[DoctorFinding]:
    policies_data, findings = _load_yaml_checked(_doctor_path("policies.yaml", config_dir))
    roles_data, roles_findings = _load_yaml_checked(_doctor_path("roles.yaml", config_dir))
    findings.extend(roles_findings)

    # N6: a role entry that isn't a mapping with a 'name' key used to just
    # vanish from `role_names` via the comprehension's `isinstance` filter --
    # silently making a role_overrides reference to the REAL role (which
    # never actually got parsed) look dangling for the WRONG reason.
    role_names: set[str] = set()
    for index, role in enumerate(roles_data.get("roles") or []):
        if isinstance(role, dict) and "name" in role:
            role_names.add(role["name"])
            continue
        findings.append(
            _finding(
                "error",
                "malformed_role_entry",
                f"roles.yaml entry #{index} is not a mapping with a 'name' key (got "
                f"{type(role).__name__}) -- not checked",
                "a role entry that isn't a mapping with a 'name' key silently vanishes "
                "from the known-roles set, making every role_overrides reference to the "
                "REAL role (which never actually got parsed) look dangling for the wrong "
                "reason",
                f"fix roles.yaml entry #{index} in the 'roles:' list to be a mapping with "
                "a 'name' key",
            )
        )

    alias_to_role = _alias_to_role_map()

    # N1: guard `policies:` itself and its nested `projects:` sub-map -- a
    # list at either level used to raise AttributeError on `.get("default")`
    # / `.items()` and crash the whole doctor report.
    policies_section, policies_section_findings = _checked_container(
        policies_data, "policies", "'policies.yaml' key 'policies'"
    )
    findings.extend(policies_section_findings)

    entries: list[tuple[str, Any]] = [("default", policies_section.get("default") or {})]
    scoped_policies, scoped_policies_findings = _checked_container(
        policies_section, "projects", "'policies.yaml' key 'policies.projects'"
    )
    findings.extend(scoped_policies_findings)
    entries.extend(scoped_policies.items())

    for scope, rules in entries:
        if not isinstance(rules, dict):
            # M3: a policy scope that isn't a mapping used to be skipped with
            # ZERO output for every rule under it (role_overrides included).
            findings.append(
                _finding(
                    "error",
                    "malformed_policy_entry",
                    f"Policy '{scope}' is not a mapping (got {type(rules).__name__}) -- "
                    "not checked",
                    "a policy scope that isn't a mapping was previously skipped with zero "
                    "output, silently disabling every rule under it (role_overrides, "
                    "block_on_severity, denied/allowed_licenses)",
                    f"fix the '{scope}' entry in policies.yaml to be a mapping",
                )
            )
            continue
        for role_ref in rules.get("role_overrides") or {}:
            if role_ref in role_names:
                continue
            if role_ref in alias_to_role:
                real_key = alias_to_role[role_ref]
                findings.append(
                    _finding(
                        "error",
                        "role_alias_used_as_role_key",
                        f"Policy '{scope}' role_overrides key '{role_ref}' is a command "
                        f"alias, not a role key",
                        "get_role() looks up ROLES by its real key only -- an alias always "
                        "raises KeyError, silently disabling the override (this is exactly "
                        "the `human_challenge`/`cos` vs `chief_of_staff` bug)",
                        f"replace '{role_ref}' with the real role key '{real_key}' in "
                        "policies.yaml",
                    )
                )
            else:
                findings.append(
                    _finding(
                        "error",
                        "dangling_role_override",
                        f"Policy '{scope}' role_overrides references unknown role '{role_ref}'",
                        "an override for a role that doesn't exist in roles.yaml is silently "
                        "never applied",
                        f"add role '{role_ref}' to roles.yaml, or fix/remove the "
                        f"role_overrides entry in policies.yaml",
                    )
                )
    return findings


def _iter_role_display_names(
    roles_section: Any, where: str
) -> tuple[list[tuple[str, Any]], list[DoctorFinding]]:
    """Yield ``(role_key, display_name_value)`` pairs from *roles_section*
    -- the value of roles.yaml's top-level ``roles:`` key.

    Handles both shapes actually seen for this key: the canonical LIST of
    role mappings each carrying a ``name`` (the only shape
    ``hivepilot.roles._load_roles_strict`` accepts -- see
    ``docs/CONFIGURATION.md``'s "roles.yaml -- a LIST under `roles:`"), and
    a MAPPING keyed by role name (a plausible hand-edited alternative even
    though the loader doesn't accept it -- the key IS the role name). An
    entry that isn't itself a mapping (list case) or whose value isn't a
    mapping (mapping case) yields a ``malformed_role_entry`` finding instead
    of being silently skipped or crashing -- this module's "I could not
    inspect this must be a finding, never silence" rule applies here too.
    """
    pairs: list[tuple[str, Any]] = []
    findings: list[DoctorFinding] = []

    if isinstance(roles_section, list):
        for index, entry in enumerate(roles_section):
            if not isinstance(entry, dict) or "name" not in entry:
                findings.append(
                    _finding(
                        "error",
                        "malformed_role_entry",
                        f"{where} entry #{index} is not a mapping with a 'name' key "
                        f"(got {type(entry).__name__}) -- not checked for display_name "
                        "collisions",
                        "a role entry that isn't a mapping with a 'name' key cannot be "
                        "identified by its role key in a display_name collision report",
                        f"fix {where} entry #{index} to be a mapping with a 'name' key",
                    )
                )
                continue
            pairs.append((entry["name"], entry.get("display_name")))
    elif isinstance(roles_section, dict):
        for role_key, entry in roles_section.items():
            if not isinstance(entry, dict):
                findings.append(
                    _finding(
                        "error",
                        "malformed_role_entry",
                        f"{where} entry '{role_key}' is not a mapping (got "
                        f"{type(entry).__name__}) -- not checked for display_name collisions",
                        "a role entry that isn't a mapping cannot be inspected for its "
                        "display_name",
                        f"fix {where} entry '{role_key}' in roles.yaml to be a mapping",
                    )
                )
                continue
            pairs.append((role_key, entry.get("display_name")))
    else:
        findings.append(
            _finding(
                "error",
                "invalid_config_section",
                f"{where} must be a list or a mapping, got {type(roles_section).__name__} "
                "-- display_name collisions could not be checked",
                "a role roster that is neither a list nor a mapping cannot be inspected "
                "for display_name collisions",
                f"fix {where} to be a list of role mappings (see docs/CONFIGURATION.md)",
            )
        )
    return pairs, findings


def check_role_display_name_collisions(config_dir: Path | None) -> list[DoctorFinding]:
    """ERROR for two-or-more roles whose ``display_name`` collides once
    normalised the same way the Telegram agent registry derives its
    addressing alias, plus a role whose ``display_name`` is empty or
    whitespace-only.

    Real incident: five roles (``designer_console``, ``designer_extension``,
    ``designer_vscode``, ``designer_agent``, ``design_reviewer``) all
    carried ``display_name: "Margaux"``. The registry's alias-claim logic
    (``telegram_bot._build_agent_registry``, Phase 4: ``_sanitise_alias(role.
    display_name.split()[0])``) already detects this collision and logs a
    ``telegram.agent_registry.alias_collision`` warning at startup -- but
    nobody reads startup logs, so the collision survived in production
    until a doctor run surfaced it by accident.

    Deliberately imports and reuses ``telegram_bot._sanitise_alias`` rather
    than reimplementing case/accent folding: ``hivepilot.roles.Role`` (see
    ``hivepilot/roles.py``) has no separate ``alias``/``aliases`` field --
    the alias IS ``display_name``, derived. Guessing a different
    normalisation (e.g. a plain ``.lower()``) would let this check disagree
    with what the registry actually does at runtime (e.g. miss an
    accent-only collision like "Aliénor" vs "Alienor").

    Also flags a whitespace-only ``display_name`` (e.g. ``"   "``)
    separately from a plain empty one: it is truthy but
    ``"   ".split()[0]`` raises ``IndexError``, which would crash
    ``_build_agent_registry()`` at import time -- not merely leave that one
    role unaddressable.
    """
    from hivepilot.services.telegram_bot import _sanitise_alias

    findings: list[DoctorFinding] = []
    roles_data, load_findings = _load_yaml_checked(_doctor_path("roles.yaml", config_dir))
    findings.extend(load_findings)

    roles_section = roles_data.get("roles")
    if roles_section is None:
        return findings

    pairs, entry_findings = _iter_role_display_names(roles_section, "'roles.yaml' key 'roles'")
    findings.extend(entry_findings)

    groups: dict[str, list[str]] = {}
    blank_roles: list[str] = []

    for role_key, display_name in pairs:
        if not isinstance(display_name, str):
            continue  # None (unset) or a non-string value: not this check's concern
        if not display_name.strip():
            blank_roles.append(role_key)
            continue
        normalised = _sanitise_alias(display_name)
        if not normalised:
            # A display_name made entirely of non-alphanumeric characters
            # (e.g. "!!!") sanitises to "" -- the real registry's `_claim`
            # early-returns on a falsy alias (no crash, no derived alias at
            # all), the same practical outcome as having no display_name.
            # Not grouped as a collision and not treated as "blank" (no
            # crash risk) -- deliberately left uncovered, see PR notes.
            continue
        groups.setdefault(normalised, []).append(role_key)

    for normalised, role_keys in sorted(groups.items()):
        if len(role_keys) < 2:
            continue
        sorted_keys = sorted(role_keys)
        count = len(sorted_keys)
        findings.append(
            _finding(
                "error",
                "duplicate_role_display_name",
                f"{count} roles share the same display_name (normalised: '{normalised}'): "
                f"{', '.join(sorted_keys)}",
                "the Telegram agent registry derives its addressing alias from "
                f"display_name -- {count - 1} of these {count} roles become unaddressable "
                "by name in chat channels; mentions resolve to only one of them "
                "(whichever wins the registry's deterministic claim order), and the "
                "engine only logs this as a 'telegram.agent_registry.alias_collision' "
                "warning at startup, which nobody reads",
                f"give each of {', '.join(sorted_keys)} a distinct display_name in roles.yaml",
            )
        )

    for role_key in sorted(blank_roles):
        findings.append(
            _finding(
                "error",
                "blank_role_display_name",
                f"Role '{role_key}' has an empty-or-whitespace-only display_name",
                "a whitespace-only display_name is truthy but has no tokens: "
                "display_name.split()[0] raises IndexError, crashing Telegram "
                "agent-registry construction outright; a purely empty display_name "
                "silently falls back to the raw role key with no human-friendly alias "
                "at all -- either way this role has no working name-based address",
                f"set a non-blank display_name for role '{role_key}' in roles.yaml, or "
                "remove the display_name key entirely to use the role key as-is",
            )
        )

    return findings


def _check_only_modules_dangling(config_dir: Path | None) -> list[DoctorFinding]:
    pipelines_data, findings = _load_yaml_checked(_doctor_path("pipelines.yaml", config_dir))
    projects_data, project_findings = _load_yaml_checked(_doctor_path("projects.yaml", config_dir))
    findings.extend(project_findings)

    # N1: guard `projects:` and `pipelines:` themselves -- a list at either
    # level used to raise AttributeError on `.values()`/`.items()` and crash
    # the whole doctor report.
    projects_section, projects_section_findings = _checked_container(
        projects_data, "projects", "'projects.yaml' key 'projects'"
    )
    findings.extend(projects_section_findings)

    all_modules: set[str] = set()
    for project_name, project in projects_section.items():
        if not isinstance(project, dict):
            # N6: a non-mapping project entry used to just be skipped here,
            # contributing zero modules to the known-modules set and making
            # every genuinely-valid only_modules reference to that project
            # look dangling for the WRONG reason.
            findings.append(
                _finding(
                    "error",
                    "malformed_project_entry",
                    f"Project '{project_name}' is not a mapping (got "
                    f"{type(project).__name__}) -- not checked",
                    "a project entry that isn't a mapping silently contributed zero "
                    "modules to the known-modules set, making every genuinely-valid "
                    "only_modules reference to that project look dangling for the wrong "
                    "reason",
                    f"fix the '{project_name}' entry in projects.yaml to be a mapping",
                )
            )
            continue
        # N1: `project.get("modules")` itself must also be guarded -- a list
        # there used to raise AttributeError on `.keys()`.
        modules, modules_findings = _checked_container(
            project, "modules", f"'projects.yaml' project '{project_name}' key 'modules'"
        )
        findings.extend(modules_findings)
        all_modules.update(modules.keys())

    pipelines_section, pipelines_section_findings = _checked_container(
        pipelines_data, "pipelines", "'pipelines.yaml' key 'pipelines'"
    )
    findings.extend(pipelines_section_findings)

    for pipeline_name, pipeline in pipelines_section.items():
        if not isinstance(pipeline, dict):
            # M2/M3: its sibling `_check_schedules_dangling` already guards
            # this; unguarded here, a scalar pipeline entry raised
            # AttributeError on `.get("stages")` and crashed the whole
            # doctor report instead of reporting just this one problem.
            findings.append(
                _finding(
                    "error",
                    "malformed_pipeline_entry",
                    f"Pipeline '{pipeline_name}' is not a mapping (got "
                    f"{type(pipeline).__name__}) -- not checked",
                    "a pipeline entry that isn't a mapping crashes this check with "
                    "AttributeError unless guarded, aborting every OTHER finding "
                    "already computed in the same doctor run",
                    f"fix the '{pipeline_name}' entry in pipelines.yaml to be a mapping "
                    "with a 'stages' list",
                )
            )
            continue
        for stage in pipeline.get("stages") or []:
            if not isinstance(stage, dict):
                findings.append(
                    _finding(
                        "error",
                        "malformed_stage_entry",
                        f"Pipeline '{pipeline_name}' has a stage entry that is not a "
                        f"mapping (got {type(stage).__name__}) -- not checked",
                        "a stage entry that isn't a mapping crashes this check with "
                        "AttributeError unless guarded",
                        f"fix the malformed stage under pipeline '{pipeline_name}' in "
                        "pipelines.yaml",
                    )
                )
                continue
            for module_ref in stage.get("only_modules") or []:
                if module_ref not in all_modules:
                    findings.append(
                        _finding(
                            "error",
                            "dangling_only_module",
                            f"Pipeline '{pipeline_name}' stage '{stage.get('name', '?')}' "
                            f"only_modules references '{module_ref}' which is not defined "
                            "in any project's modules map",
                            "a stage scoped to a module no project declares can never match "
                            "any run's targets, silently skipping that stage every time",
                            f"add '{module_ref}' to the target project's modules: map in "
                            "projects.yaml, or remove it from only_modules",
                        )
                    )
    return findings


def check_dangling_references(config_dir: Path | None) -> list[DoctorFinding]:
    findings: list[DoctorFinding] = []
    try:
        problems = validate_config(base_dir=config_dir)
    except Exception as exc:  # noqa: BLE001 -- M4: validate_config re-raises a YAML
        # parse error as ValueError; letting it propagate would LOSE every
        # finding already computed elsewhere in this doctor run, not just
        # skip this one check. Never interpolate str(exc) here (may embed
        # file contents) -- name the exception TYPE only, matching
        # plugins.py's `run_health_check` discipline.
        findings.append(
            _finding(
                "error",
                "dangling_reference_check_failed",
                f"`validate_config()` raised {type(exc).__name__} -- dangling-reference "
                "checks could not run",
                "a broken config file (e.g. unparseable YAML) makes validate_config() "
                "raise instead of returning problems, which would otherwise silently "
                "discard every OTHER finding this doctor run already computed",
                "run `hivepilot validate"
                + (f" --dir {config_dir}" if config_dir else "")
                + "` directly to see the exact parse error, then fix the offending file",
            )
        )
    else:
        for problem in problems:
            findings.append(
                _finding(
                    "error",
                    "dangling_reference",
                    problem,
                    "a stale/typo'd reference is silently ignored until the exact run path "
                    "that touches it fails, deep inside the engine",
                    "`hivepilot validate"
                    + (f" --dir {config_dir}" if config_dir else "")
                    + "` lists every dangling reference of this kind; edit the named file to "
                    "fix or remove it",
                )
            )
    findings.extend(_check_schedules_dangling(config_dir))
    findings.extend(_check_role_overrides_dangling(config_dir))
    findings.extend(_check_only_modules_dangling(config_dir))
    return findings


# ---------------------------------------------------------------------------
# Check: secrets sanity (incident #7's secrets cousin)
# ---------------------------------------------------------------------------


def check_secrets_sanity(config_dir: Path | None) -> list[DoctorFinding]:
    findings: list[DoctorFinding] = []

    # (a) secret-typed Settings fields resolved to an EMPTY-OR-WHITESPACE-
    # ONLY string -- the recurring "empty means unset" fail-open class: code
    # that checks `if settings.X` treats '' as falsy/unset, but code that
    # checks `is not None` treats '' as configured and proceeds with a blank
    # credential. `forges/provider.py`, `swarm_service.py`, and
    # `config_provenance.py` all guard with `.strip()`, not just `== ""`
    # (M7) -- a whitespace-only secret value passes `raw == ""` but is just
    # as blank in practice. Never prints the value (there is nothing to
    # redact -- an empty string IS the finding).
    for key in all_keys():
        if not is_secret_field(key):
            continue
        raw = getattr(settings, key, None)
        if isinstance(raw, str) and not raw.strip():
            findings.append(
                _finding(
                    "error",
                    "empty_secret_setting",
                    f"Setting '{key}' is set to an empty-or-whitespace-only string",
                    "an empty/blank secret value is a common fail-open: some call sites "
                    "treat '' as unset (falsy), others treat it as configured-but-blank "
                    "and proceed",
                    f"unset HIVEPILOT_{key.upper()} entirely so '{key}' defaults to None, or "
                    "supply a real value",
                )
            )

    # (b) ${secret:NAME} references with no matching catalog entry, per
    # project (the catalog is `projects.yaml`'s per-project `secrets:` map --
    # see hivepilot/services/secret_refs.py). Presence-only: never resolves
    # the reference (no network/side effects from a read-only health check).
    projects_data, project_findings = _load_yaml_checked(_doctor_path("projects.yaml", config_dir))
    findings.extend(project_findings)
    # N1: guard `projects:` itself -- a list there used to raise
    # AttributeError on `.items()` and crash the whole doctor report.
    projects_section, projects_section_findings = _checked_container(
        projects_data, "projects", "'projects.yaml' key 'projects'"
    )
    findings.extend(projects_section_findings)
    for project_name, project in projects_section.items():
        if not isinstance(project, dict):
            # M3: a project entry that isn't a mapping used to be skipped
            # with ZERO output for its secrets/env sanity checks.
            findings.append(
                _finding(
                    "error",
                    "malformed_project_entry",
                    f"Project '{project_name}' is not a mapping (got "
                    f"{type(project).__name__}) -- not checked",
                    "a project entry that isn't a mapping was previously skipped with "
                    "zero output, silently disabling its dangling-secret-ref check",
                    f"fix the '{project_name}' entry in projects.yaml to be a mapping",
                )
            )
            continue
        catalog = project.get("secrets") or {}
        if not isinstance(catalog, dict):
            # N5: unguarded, `ref_name not in catalog` against a scalar (e.g.
            # a string) silently degrades to SUBSTRING matching -- a
            # fail-open where a reference named 'a' would incorrectly appear
            # resolved against the catalog value "abc".
            findings.append(
                _finding(
                    "error",
                    "malformed_project_secrets",
                    f"Project '{project_name}' secrets is not a mapping (got "
                    f"{type(catalog).__name__}) -- not checked",
                    "a secrets catalog that isn't a mapping silently degrades "
                    "`ref_name not in catalog` to SUBSTRING matching against a scalar, "
                    "causing an unrelated but similarly-named ${secret:NAME} reference "
                    "to incorrectly appear resolved -- a fail-open, not just a crash",
                    f"fix the 'secrets' block under project '{project_name}' in "
                    "projects.yaml to be a mapping",
                )
            )
            continue
        env = project.get("env") or {}
        if not isinstance(env, dict):
            findings.append(
                _finding(
                    "error",
                    "malformed_project_env",
                    f"Project '{project_name}' env is not a mapping (got "
                    f"{type(env).__name__}) -- not checked",
                    "an env block that isn't a mapping was previously skipped with zero "
                    "output for its dangling ${secret:NAME} reference check",
                    f"fix the 'env' block under project '{project_name}' in projects.yaml "
                    "to be a mapping",
                )
            )
            continue
        for env_key, env_value in env.items():
            if not isinstance(env_value, str):
                # N6: a non-string env value cannot contain a ${secret:NAME}
                # reference by definition, but silently skipping it means a
                # config typo (e.g. an unintentionally-unquoted value) that
                # SHOULD have been a string never gets flagged -- info-level
                # only, since non-string env values (bools/ints) are a
                # legitimate, common pattern.
                findings.append(
                    _finding(
                        "info",
                        "non_string_project_env_value",
                        f"Project '{project_name}' env['{env_key}'] is not a string (got "
                        f"{type(env_value).__name__}) -- not scanned for ${{secret:NAME}} "
                        "references",
                        "a non-string env value cannot contain a secret reference, but "
                        "skipping it silently means a value that should have been a "
                        "quoted string referencing a secret is never flagged",
                        f"if project '{project_name}' env['{env_key}'] should reference a "
                        "secret via ${secret:NAME}, ensure the YAML value is quoted as a "
                        "string",
                    )
                )
                continue
            for ref_name in find_secret_refs(env_value):
                if ref_name not in catalog:
                    findings.append(
                        _finding(
                            "error",
                            "dangling_secret_ref",
                            f"Project '{project_name}' env['{env_key}'] references "
                            f"${{secret:{ref_name}}} which has no catalog entry",
                            "an unresolvable ${secret:NAME} reference aborts the step at "
                            "step-assembly time under the default 'closed' fail mode",
                            f"add a '{ref_name}:' entry to project '{project_name}''s "
                            "secrets: catalog in projects.yaml, or fix the reference",
                        )
                    )

    # (c) M6: settings.swarm_key's own ${secret:NAME} reference (if any)
    # must also resolve against swarm_secrets. `swarm_service.
    # resolve_swarm_signing_key` degrades an unresolvable reference to
    # `None` -- signing/verification silently disabled, not an error --
    # rather than raising, exactly the "silent until it matters" state this
    # doctor exists to surface ahead of time.
    swarm_key = settings.swarm_key
    if isinstance(swarm_key, str) and has_secret_ref(swarm_key):
        swarm_catalog = settings.swarm_secrets or {}
        for ref_name in find_secret_refs(swarm_key):
            if ref_name not in swarm_catalog:
                findings.append(
                    _finding(
                        "error",
                        "dangling_swarm_secret_ref",
                        f"settings.swarm_key references ${{secret:{ref_name}}} which has "
                        "no entry in swarm_secrets",
                        "an unresolvable ${secret:NAME} reference in swarm_key makes "
                        "resolve_swarm_signing_key() degrade to None -- swarm event "
                        "signing/verification is silently disabled until a peer rejects "
                        "an unsigned event",
                        f"add a '{ref_name}:' entry to settings.swarm_secrets "
                        "(HIVEPILOT_SWARM_SECRETS), or fix the reference in swarm_key",
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _run_check(name: str, func: Callable[[], list[DoctorFinding]]) -> list[DoctorFinding]:
    """Run one check in isolation: a single check crashing must NEVER
    discard every OTHER check's already-computed findings in the same
    doctor run.

    Systemic backstop (2nd Opus review, PR #334, N1) layered on TOP of the
    targeted per-site mapping guards elsewhere in this module -- defense in
    depth against any failure mode not yet anticipated by those guards.
    Never interpolate `str(exc)` (a YAML parser's exception string can embed
    a credential from the offending line): name the exception TYPE only,
    matching `plugins.py::run_health_check`'s discipline.
    """
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 -- see docstring
        return [
            _finding(
                "error",
                "check_crashed",
                f"doctor check '{name}' raised {type(exc).__name__} while running -- its "
                "findings could not be computed",
                "one check crashing must never silently discard every OTHER check's "
                "already-computed findings in the same doctor run",
                "re-run the individual check function directly, or `hivepilot validate`, "
                "to see the exact error, then fix the offending config file",
            )
        ]


def _dedupe_findings(findings: list[DoctorFinding]) -> list[DoctorFinding]:
    """Collapse byte-identical findings emitted by more than one check in
    the same doctor run (2nd Opus review, PR #334, N2): `projects.yaml`
    alone is loaded independently by three different checks, so one
    unparseable file used to emit `unparseable_config_yaml` three times.
    `DoctorFinding` is a frozen, hashable dataclass, so exact duplicates can
    be identified by value alone -- order-preserving, first occurrence
    wins."""
    seen: set[DoctorFinding] = set()
    deduped: list[DoctorFinding] = []
    for finding in findings:
        if finding in seen:
            continue
        seen.add(finding)
        deduped.append(finding)
    return deduped


def run_doctor(config_dir: Path | None = None) -> list[DoctorFinding]:
    """Run every check and return the combined findings list (empty means a
    clean config: `hivepilot config doctor` prints "OK" and exits 0).

    NOT side-effect-free (L7): this constructs a real `PluginManager()`,
    which scans the plugins dir/entry points, compiles and `exec()`s every
    local plugin file it finds, and (via `check_plugin_health`) runs each
    plugin's own `health()` callable -- the exact same process-global
    side effects `plugins list`/`plugins health` have. Callers embedding
    this in an automated/scheduled context should treat it the same as
    those commands, not as a pure read.
    """
    from hivepilot.plugins import PluginManager

    findings: list[DoctorFinding] = []
    findings.extend(_run_check("check_cwd_relative_paths", check_cwd_relative_paths))
    findings.extend(_run_check("check_sync_drift", check_sync_drift))

    plugin_manager = PluginManager()
    findings.extend(
        _run_check(
            "check_enabled_plugins_loaded",
            lambda: check_enabled_plugins_loaded(plugin_manager),
        )
    )
    findings.extend(_run_check("check_plugin_health", lambda: check_plugin_health(plugin_manager)))

    findings.extend(
        _run_check("check_dangling_references", lambda: check_dangling_references(config_dir))
    )
    findings.extend(_run_check("check_secrets_sanity", lambda: check_secrets_sanity(config_dir)))
    findings.extend(
        _run_check(
            "check_role_display_name_collisions",
            lambda: check_role_display_name_collisions(config_dir),
        )
    )
    return _dedupe_findings(findings)


# ---------------------------------------------------------------------------
# `hivepilot plugins verify` -- incident #5 (a package-name collision +
# a musl trap: `pip list` said "installed" while the import failed, and a
# same-named-but-different package silently satisfied the import).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginVerifyResult:
    """One plugin's prerequisite truth, distinguishing "pip believes it's
    installed" from "the code can actually import it" -- the exact
    distinction incident #5 needed (`pip list` said installed, the import
    still failed)."""

    name: str
    prereq_kind: str  # "pip" | "binary" | "config" | "unknown"
    present_per_declaration: bool | None  # pip: `importlib.metadata` truth; binary: shutil.which
    importable: bool | None  # pip only: a REAL `importlib.import_module` attempt
    mismatch: str | None  # non-None when pip-truth and import-truth disagree
    detail: str


def verify_badge(result: PluginVerifyResult) -> str:
    """Three-state badge for `plugins verify`'s output line (M5).

    A plugin that is neither importable NOR pip-installed (``mismatch`` is
    None because pip-truth and import-truth AGREE -- both say "absent") must
    never render as a plain "ok": an operator scanning the badge column
    would conclude a genuinely missing dependency works. Likewise a binary
    probe that reports "NOT FOUND on PATH". ``MISMATCH`` still wins when
    pip-truth and import-truth actively disagree (the incident #5 namesake-
    collision / broken-install case)."""
    if result.mismatch:
        return "MISMATCH"
    if result.prereq_kind == "pip" and result.importable is False:
        return "MISSING"
    if result.prereq_kind == "binary" and result.present_per_declaration is False:
        return "MISSING"
    return "ok"


# plugin name -> (import module name, expected PyPI distribution name).
# Deliberately curated (not every KNOWN_EXAMPLE_PLUGINS pip-kind entry): only
# plugins with a SINGLE unambiguous import path get a real import + a
# distribution cross-check here -- see `verify_plugins`'s docstring for what
# is deliberately NOT covered (onepassword/kms have multiple SDK modes).
_PIP_IMPORT_PROBES: dict[str, tuple[str, str]] = {
    "mem0": ("mem0", "mem0ai"),
    "headroom": ("headroom", "headroom-ai"),
    "infisical": ("infisical_sdk", "infisicalsdk"),
}

# plugin name -> binary it shells out to (matches every "binary"-kind entry
# in KNOWN_EXAMPLE_PLUGINS).
_BINARY_PROBES: dict[str, str] = {
    "rtk": "rtk",
    "herdr": "herdr",
    "hugo": "hugo",
    "gh": "gh",
    "tmux": "tmux",
    "bitwarden": "bw",
    "vaultwarden": "bw",
}


def platform_tag() -> str:
    """Report enough platform detail for an operator to reason about wheel
    availability (musl vs glibc) without making any network call -- incident
    #5's second half: `headroom-ai` publishes no musllinux wheel, so a pip
    install on Alpine attempts a slow or failing native source build."""
    system = platform.system()
    machine = platform.machine()
    if system != "Linux":
        return f"{system}-{machine}"
    libc_name, libc_version = platform.libc_ver()
    if libc_name:
        return f"linux-{machine} (glibc {libc_version})"
    if Path("/etc/alpine-release").exists():
        return (
            f"linux-{machine} (musl / Alpine — many PyPI packages ship no musllinux "
            "wheel and pip will attempt a slow or failing source build)"
        )
    return (
        f"linux-{machine} (libc undetermined — could be musl; verify manually if pip install hangs)"
    )


def _verify_pip_plugin(name: str, import_name: str, distribution: str) -> PluginVerifyResult:
    try:
        importlib.import_module(import_name)
        importable = True
        import_error: str | None = None
    except Exception as exc:  # noqa: BLE001 — reporting the truth, never raising
        importable = False
        # L2: name the exception TYPE only, never interpolate str(exc) from
        # an arbitrary third-party import -- matches the discipline
        # `plugins.py::run_health_check` already applies (exception details
        # can embed paths/env values from the failing package's own code).
        import_error = type(exc).__name__

    metadata_error: str | None = None
    try:
        version = importlib_metadata.version(distribution)
        pip_installed: bool | None = True
    except importlib_metadata.PackageNotFoundError:
        version = None
        pip_installed = False
    except Exception as exc:  # noqa: BLE001 — L3: corrupt dist-info metadata (or any
        # other importlib.metadata failure) must not crash `plugins verify`
        # outright; only `PackageNotFoundError` was caught before.
        version = None
        pip_installed = False
        metadata_error = type(exc).__name__

    mismatch: str | None = None
    if importable and not pip_installed:
        mismatch = (
            f"'{import_name}' imports successfully, but pip does NOT report the expected "
            f"'{distribution}' distribution as installed -- a same-named different package "
            "may be providing this import (namesake collision)"
        )
    elif not importable and pip_installed:
        mismatch = (
            f"pip reports '{distribution}' {version} installed, but `import {import_name}` "
            f"fails ({import_error}) -- the installation may be broken, e.g. a missing "
            f"native wheel for this platform ({platform_tag()})"
        )

    if mismatch:
        detail = mismatch
    elif importable:
        detail = f"importable; pip: '{distribution}' {version} installed"
    else:
        pip_detail = (
            f"pip metadata lookup errored ({metadata_error})"
            if metadata_error
            else f"pip: '{distribution}' not installed"
        )
        detail = f"NOT importable ({import_error}); {pip_detail}"

    return PluginVerifyResult(
        name=name,
        prereq_kind="pip",
        present_per_declaration=pip_installed,
        importable=importable,
        mismatch=mismatch,
        detail=detail,
    )


def _verify_binary_plugin(name: str, binary: str) -> PluginVerifyResult:
    found = shutil.which(binary) is not None
    return PluginVerifyResult(
        name=name,
        prereq_kind="binary",
        present_per_declaration=found,
        importable=None,
        mismatch=None,
        detail=f"binary '{binary}': {'found on PATH' if found else 'NOT FOUND on PATH'}",
    )


def verify_plugins() -> list[PluginVerifyResult]:
    """For every plugin in the curated `KNOWN_EXAMPLE_PLUGINS` registry,
    attempt the ACTUAL import/binary check its code needs and report the
    truth -- `pip list`/`shutil.which` alone is not proof (incident #5).

    Deliberately NOT a real import/distribution check for `onepassword` and
    `kms`: both have multiple mutually-exclusive SDK modes selected by
    config (`op_connect_host` vs a direct service-account token; `aws` vs
    `gcp` vs `azure` for kms) -- picking one to probe unconditionally would
    misreport the other modes as broken. Both are still listed with
    `prereq_kind="pip"` and an honest "not automatically verified" detail
    rather than silently omitted.
    """
    results: list[PluginVerifyResult] = []
    for name in sorted(KNOWN_EXAMPLE_PLUGINS):
        spec = KNOWN_EXAMPLE_PLUGINS[name]
        if name in _PIP_IMPORT_PROBES:
            import_name, distribution = _PIP_IMPORT_PROBES[name]
            results.append(_verify_pip_plugin(name, import_name, distribution))
        elif name in _BINARY_PROBES:
            results.append(_verify_binary_plugin(name, _BINARY_PROBES[name]))
        elif spec.prereq_kind == "pip":
            results.append(
                PluginVerifyResult(
                    name=name,
                    prereq_kind="pip",
                    present_per_declaration=None,
                    importable=None,
                    mismatch=None,
                    detail=(
                        f"multi-mode dependency ({spec.prereq_detail}); not automatically "
                        "verified by `plugins verify` -- check manually"
                    ),
                )
            )
        elif spec.prereq_kind == "config":
            results.append(
                PluginVerifyResult(
                    name=name,
                    prereq_kind="config",
                    present_per_declaration=None,
                    importable=None,
                    mismatch=None,
                    detail=f"config-gated ({spec.prereq_detail}); no import/binary to verify",
                )
            )
        else:
            results.append(
                PluginVerifyResult(
                    name=name,
                    prereq_kind="unknown",
                    present_per_declaration=None,
                    importable=None,
                    mismatch=None,
                    detail="no automated prerequisite check available for this plugin",
                )
            )
    return results
