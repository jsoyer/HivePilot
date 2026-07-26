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
    plugins dir; the only symptom was an empty dashboard panel).
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from hivepilot.config import settings
from hivepilot.services.config_provenance import all_keys, is_secret_field
from hivepilot.services.config_validation import validate_config
from hivepilot.services.plugin_installer import KNOWN_EXAMPLE_PLUGINS
from hivepilot.services.secret_refs import find_secret_refs

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
    assert severity in _SEVERITIES, f"invalid severity {severity!r}"
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


def _load_yaml_or_empty(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except yaml.YAMLError:
        return {}


# ---------------------------------------------------------------------------
# Check: resolved absolute paths + cwd-relative warning (incident #1)
# ---------------------------------------------------------------------------

# (label, raw-path-getter, override-env-var, xdg-chain-aware)
# xdg_chain-aware fields are resolved via settings.resolve_config_path (the
# same XDG -> config_repo -> base_dir chain `config sync` writes to); the
# others (state_db, obsidian_vault) have NO xdg-aware loader anywhere in the
# codebase and always resolve via settings.resolve_path -- i.e. base_dir.
_PATH_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("state_db", "state_db", "HIVEPILOT_STATE_DB", False),
    ("prompts_dir", "prompts", "HIVEPILOT_PROMPTS_DIR", True),
    ("obsidian_vault", "obsidian_vault", "HIVEPILOT_OBSIDIAN_VAULT", False),
)


def _base_dir_pinned() -> bool:
    """True if the operator explicitly pinned ``base_dir`` via env (the
    documented fix for incident #1) rather than letting it silently default
    to ``Path.cwd()`` at process start -- which differs between a service
    started at ``cwd=/`` and a CLI invocation from an operator's home dir."""
    return bool(os.environ.get("HIVEPILOT_BASE_DIR"))


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
    for label, filename, _env, xdg_aware in _PATH_FIELDS:
        if xdg_aware:
            resolved, _rank = _walk_xdg_rank(filename)
        else:
            resolved = settings.resolve_path(Path(filename))
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
    for label, filename, override_env, xdg_aware in _PATH_FIELDS:
        override_value = os.environ.get(override_env)
        if override_value and Path(override_value).expanduser().is_absolute():
            continue  # explicitly pinned for this field -- never cwd-relative
        if xdg_aware:
            _resolved, rank = _walk_xdg_rank(filename)
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


def check_enabled_plugins_loaded(plugin_manager: Any) -> list[DoctorFinding]:
    from hivepilot.registry import _BUILTIN_RUNNERS

    builtin_runner_stems = frozenset(_BUILTIN_RUNNERS)
    loaded_names = {record.name for record in plugin_manager.loaded}

    findings: list[DoctorFinding] = []
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
        expected = settings.resolve_path(Path("plugins") / f"{stem}.py")
        installed_dir = settings.xdg_data_home / "plugins" / f"{stem}.py"
        install_hint = (
            f"`hivepilot plugins install {stem}`"
            if stem in KNOWN_EXAMPLE_PLUGINS
            else "a plugin file"
        )
        findings.append(
            _finding(
                "error",
                "plugin_enabled_not_loaded",
                f"'{stem}' is enabled ({key}=true) but no such plugin is loaded",
                "an *_enabled flag with no matching loaded plugin silently degrades to a "
                "missing feature (e.g. an empty dashboard panel) with no error anywhere "
                "in normal operation",
                f"add the plugin file at {expected} or {installed_dir} (fetch it with "
                f"{install_hint}), or set {key}=false",
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
    schedules_data = _load_yaml_or_empty(_doctor_path("schedules.yaml", config_dir))
    projects_data = _load_yaml_or_empty(_doctor_path("projects.yaml", config_dir))
    tasks_data = _load_yaml_or_empty(_doctor_path("tasks.yaml", config_dir))

    project_names: set[str] = set((projects_data.get("projects") or {}).keys())
    task_names: set[str] = set((tasks_data.get("tasks") or {}).keys())

    findings: list[DoctorFinding] = []
    for schedule_name, schedule in (schedules_data.get("schedules") or {}).items():
        if not isinstance(schedule, dict):
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
    policies_data = _load_yaml_or_empty(_doctor_path("policies.yaml", config_dir))
    roles_data = _load_yaml_or_empty(_doctor_path("roles.yaml", config_dir))

    role_names: set[str] = {
        role["name"]
        for role in (roles_data.get("roles") or [])
        if isinstance(role, dict) and "name" in role
    }
    alias_to_role = _alias_to_role_map()

    policies = policies_data.get("policies") or {}
    entries: list[tuple[str, dict[str, Any]]] = [("default", policies.get("default") or {})]
    entries.extend((policies.get("projects") or {}).items())

    findings: list[DoctorFinding] = []
    for scope, rules in entries:
        if not isinstance(rules, dict):
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


def _check_only_modules_dangling(config_dir: Path | None) -> list[DoctorFinding]:
    pipelines_data = _load_yaml_or_empty(_doctor_path("pipelines.yaml", config_dir))
    projects_data = _load_yaml_or_empty(_doctor_path("projects.yaml", config_dir))

    all_modules: set[str] = set()
    for project in (projects_data.get("projects") or {}).values():
        if isinstance(project, dict):
            all_modules.update((project.get("modules") or {}).keys())

    findings: list[DoctorFinding] = []
    for pipeline_name, pipeline in (pipelines_data.get("pipelines") or {}).items():
        for stage in pipeline.get("stages") or []:
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
    for problem in validate_config(base_dir=config_dir):
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

    # (a) secret-typed Settings fields resolved to an EMPTY string -- the
    # recurring "empty means unset" fail-open class: code that checks
    # `if settings.X` treats "" as falsy/unset, but code that checks
    # `is not None` treats "" as configured and proceeds with a blank
    # credential. Never prints the value (there is nothing to redact -- an
    # empty string IS the finding).
    for key in all_keys():
        if not is_secret_field(key):
            continue
        raw = getattr(settings, key, None)
        if isinstance(raw, str) and raw == "":
            findings.append(
                _finding(
                    "error",
                    "empty_secret_setting",
                    f"Setting '{key}' is set to an EMPTY string",
                    "an empty secret value is a common fail-open: some call sites treat '' "
                    "as unset (falsy), others treat it as configured-but-blank and proceed",
                    f"unset HIVEPILOT_{key.upper()} entirely so '{key}' defaults to None, or "
                    "supply a real value",
                )
            )

    # (b) ${secret:NAME} references with no matching catalog entry, per
    # project (the catalog is `projects.yaml`'s per-project `secrets:` map --
    # see hivepilot/services/secret_refs.py). Presence-only: never resolves
    # the reference (no network/side effects from a read-only health check).
    projects_data = _load_yaml_or_empty(_doctor_path("projects.yaml", config_dir))
    for project_name, project in (projects_data.get("projects") or {}).items():
        if not isinstance(project, dict):
            continue
        catalog = project.get("secrets") or {}
        env = project.get("env") or {}
        if not isinstance(env, dict):
            continue
        for env_key, env_value in env.items():
            if not isinstance(env_value, str):
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
    return findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_doctor(config_dir: Path | None = None) -> list[DoctorFinding]:
    """Run every check and return the combined findings list (empty means a
    clean config: `hivepilot config doctor` prints "OK" and exits 0)."""
    from hivepilot.plugins import PluginManager

    findings: list[DoctorFinding] = []
    findings.extend(check_cwd_relative_paths())
    findings.extend(check_sync_drift())

    plugin_manager = PluginManager()
    findings.extend(check_enabled_plugins_loaded(plugin_manager))
    findings.extend(check_plugin_health(plugin_manager))

    findings.extend(check_dangling_references(config_dir))
    findings.extend(check_secrets_sanity(config_dir))
    return findings


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
        import_error = f"{type(exc).__name__}: {exc}"

    try:
        version = importlib_metadata.version(distribution)
        pip_installed: bool | None = True
    except importlib_metadata.PackageNotFoundError:
        version = None
        pip_installed = False

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
        detail = f"NOT importable ({import_error}); pip: '{distribution}' not installed"

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
