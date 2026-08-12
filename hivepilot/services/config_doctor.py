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

The ``config-doctor-session-incidents`` sprint added three more checks from a
separate, later production-debugging session (numbered independently from
the incidents above to avoid collision):

  * ``check_dangling_instruction_files`` — session-incident #1 (HIGHEST
    VALUE): ``ProjectConfig.claude_md``/``.instruction_files`` named a
    repository instructions file ABSENT from the repo, and nothing reported
    it for months -- every agent ran without the governance context its own
    prompt asserted was provided inline. Reuses
    ``hivepilot.services.repo_instructions``'s own resolution rather than
    reimplementing it.
  * ``check_shared_obsidian_vault`` — session-incident #2: ``Settings.
    obsidian_vault`` is a single GLOBAL path, but several projects/pipelines
    commonly coexist on one machine. Informational only -- a known engine
    limitation, not a misconfiguration.
  * ``check_vault_git_state`` — session-incident #3: ``obsidian_service.py``
    has no git capability at all; on the operator's box the vault sat 67
    files uncommitted for 6 days. Local git state only, no network calls.

The propose→ratify→dispatch PRD (sprint S5) added one more:

  * ``check_partition_readiness`` — two ways a partition setup is
    CONFIGURED yet silently cannot do what the operator believes: the
    default ``claude_max_concurrency: 1`` turns "N parallel agents" into
    one agent N times, and a missing ``max_partition_cost_usd`` makes the
    ratification gate refuse every partition for that project (fail-closed,
    never unbounded) at the moment a human presses dispatch.

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
from datetime import datetime, timedelta, timezone
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
    # Defaults to a RELATIVE `runs/logs`, and produced the most copies of any
    # field here: five `runs/logs/hivepilot.log` on the production host, one
    # per directory a command was ever typed from, with only the one under `/`
    # live. The check that names cwd-relative paths did not list it.
    ("logs_dir", "logs_dir", "HIVEPILOT_LOGS_DIR", False),
)

# Paths that are NOT `Settings` attributes and so cannot appear in the table
# above, but resolve through `base_dir` exactly like the ones that are. Each is
# (label, resolver) -- the resolver takes no arguments and reads live settings,
# so a monkeypatched `base_dir` moves it, as an operator's pin does.
#
# `.hivepilot/feedback` is here because it was invisible to this check for the
# same reason it was hardest to fix: it was a module-level constant in
# `knowledge_service`, keyed by nothing the doctor could enumerate. It is the
# log the agent prompt's "last five feedback entries" is built from.
_DERIVED_PATHS: tuple[tuple[str, Callable[[], Path]], ...] = (
    ("feedback_dir", lambda: settings.resolve_path(Path(".hivepilot") / "feedback")),
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
    for label, resolve in _DERIVED_PATHS:
        lines.append(f"{label:<17}: {resolve()}")
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

    # The derived paths have no per-field override to check -- only the
    # `HIVEPILOT_BASE_DIR` pin moves them, so that pin is the whole condition.
    if not _base_dir_pinned():
        for label, resolve in _DERIVED_PATHS:
            findings.append(
                _finding(
                    "warning",
                    "cwd_relative_path",
                    f"'{label}' resolves relative to the process's cwd at startup "
                    f"(base_dir={settings.base_dir!s}, no HIVEPILOT_BASE_DIR pin) "
                    f"-> {resolve()}",
                    "a service started at cwd=/ and a CLI run from an operator's home "
                    "directory resolve this to two DIFFERENT files -- and this one feeds "
                    "the 'last five feedback entries' block of every agent prompt, so the "
                    "divergence reaches the model rather than only the disk",
                    "set HIVEPILOT_BASE_DIR=<absolute path> in the shared env every "
                    "service sources",
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
        "otel_ingest",  # OTLP metric route on the API, not a plugin
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
# Check: retry_queue abnormal backlog (fix/retry-queue-drain incident)
#
# 197 `groomer-scan` retries sat PENDING and past-due in retry_queue for 7
# days: `retry_service.enqueue()` (the plain exponential-backoff path
# `schedule_service.run_entry()` uses on an ordinary task failure) writes
# rows with `context IS NULL`; the scheduler daemon's only reader
# (`scheduler_daemon._process_deferred_rows`) filtered `context IS NOT
# NULL` -- a guard written exclusively for the quota-deferred subtype, so a
# context-less row was never drained by anything. `hivepilot schedule
# health` printed the raw pending count the whole time but never flagged it
# as abnormal -- exactly the invisible-degradation class `config doctor`
# exists to catch.
#
# Deliberately conservative (the check_enabled_plugins_loaded 17-false-
# positives lesson applies here too): a handful of rows still waiting out
# their own backoff window is completely normal and produces NO finding.
# Only rows overdue by more than `settings.retry_queue_stale_after_hours`
# count as "stuck" -- backoff delays here are minutes, not hours, so ANY
# row overdue that long in a system where the daemon is actually ticking
# means the drain genuinely isn't reaching it.
# ---------------------------------------------------------------------------


def check_retry_queue_backlog() -> list[DoctorFinding]:

    from hivepilot.services import db, state_service
    from hivepilot.utils.display_time import _parse_stored

    state_service.init_db()
    with db.connect() as conn:
        rows = conn.execute(db.ph("SELECT * FROM retry_queue WHERE status='pending'")).fetchall()

    if not rows:
        return []

    now = datetime.now(timezone.utc)
    threshold = timedelta(hours=settings.retry_queue_stale_after_hours)

    stuck: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for row in rows:
        row_dict = dict(row)
        raw_next = row_dict.get("next_retry_at")
        parsed = _parse_stored(raw_next) if raw_next else None
        if parsed is None:
            unknown.append(row_dict)
            continue
        if now - parsed > threshold:
            stuck.append(row_dict)

    findings: list[DoctorFinding] = []

    if unknown:
        ids = ", ".join(str(r["id"]) for r in unknown[:10])
        more = "..." if len(unknown) > 10 else ""
        findings.append(
            _finding(
                "error",
                "retry_queue_unparseable_timestamp",
                f"{len(unknown)} retry_queue row(s) have a next_retry_at that could not be "
                f"parsed (ids: {ids}{more})",
                "a row whose due-ness cannot be determined is never picked up by the drain "
                "(fail-closed) -- it sits pending forever with no other signal",
                "inspect the row directly (`hivepilot schedule retry-list --status pending`) "
                "and fix or delete its next_retry_at value",
            )
        )

    if stuck:
        sample = stuck[0]
        oldest_overdue = max(now - _parse_stored(r["next_retry_at"]) for r in stuck)
        oldest_days, oldest_rem = divmod(int(oldest_overdue.total_seconds()), 86400)
        oldest_hours = oldest_rem // 3600
        severity = "error" if len(stuck) >= settings.retry_queue_backlog_error_count else "warning"
        findings.append(
            _finding(
                severity,
                "retry_queue_backlog",
                f"{len(stuck)} retry_queue row(s) are pending and overdue by more than "
                f"{settings.retry_queue_stale_after_hours}h (oldest overdue: {oldest_days}d "
                f"{oldest_hours}h; e.g. task={sample.get('task')!r} "
                f"error={str(sample.get('error'))[:120]!r})",
                "the scheduler daemon drains the retry queue at most one row per tick -- a "
                "row still overdue by hours means either the daemon isn't running, or "
                "something is preventing the drain from ever reaching it (this is exactly "
                "how 197 groomer-scan retries sat pending for 7 days with zero signal)",
                "run `hivepilot schedule health` for the live count, `hivepilot schedule "
                "retry-list --status pending` to inspect the rows, and confirm the scheduler "
                "daemon (`hivepilot schedule daemon`) is actually running",
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
    """ERROR for two-or-more roles whose ``display_name`` would collide on
    the SAME alias the Telegram agent registry actually derives, plus a
    role whose ``display_name`` is empty/whitespace-only, or non-blank but
    sanitises to an empty alias.

    Real incident: five roles (``designer_console``, ``designer_extension``,
    ``designer_vscode``, ``designer_agent``, ``design_reviewer``) all
    carried ``display_name: "Margaux"``. The registry's alias-claim logic
    (``telegram_bot._build_agent_registry``, Phase 4) already detected this
    collision and logged a ``telegram.agent_registry.alias_collision``
    warning at startup -- but nobody reads startup logs, so the collision
    survived in production until a doctor run surfaced it by accident. The
    FIRST version of this check made things worse, not better: it compared
    whole ``display_name`` strings (e.g. "Margaux" vs "Margaux (Console)"
    look distinct that way), which disagreed with the registry's real
    first-token-based derivation and reported ZERO collisions while the
    engine logged four -- a check that disagrees with the mechanism it
    guards actively certifies a broken state.

    Deliberately imports and REPLAYS ``telegram_bot._display_name_alias_
    claims`` -- the exact same claim-attempt function Phase 4 of
    ``_build_agent_registry`` uses -- rather than reimplementing any
    normalisation or priority rule locally. This is what makes it
    structurally impossible for this check to disagree with the live
    registry again: whatever the registry's derivation rule is, THIS is it.

    Also flags:
      * a whitespace-only ``display_name`` (e.g. ``"   "``) separately from
        a plain empty one: it is truthy but ``"   ".split()[0]`` would raise
        ``IndexError`` if reached un-guarded by the registry.
      * a non-blank ``display_name`` that sanitises to an empty string
        (e.g. ``"!!!"``) -- the registry's ``_claim`` silently no-ops on an
        empty alias (see its early return), so this role gets NO
        display-name-derived alias at all, with zero warning at startup.
        The recurring "empty/absent treated as no constraint" bug class
        this repo tracks: an empty derived alias must be a finding, never
        silently skipped.
    """
    from hivepilot.services.telegram_bot import _display_name_alias_claims, _sanitise_alias

    findings: list[DoctorFinding] = []
    roles_data, load_findings = _load_yaml_checked(_doctor_path("roles.yaml", config_dir))
    findings.extend(load_findings)

    roles_section = roles_data.get("roles")
    if roles_section is None:
        return findings

    pairs, entry_findings = _iter_role_display_names(roles_section, "'roles.yaml' key 'roles'")
    findings.extend(entry_findings)

    display_names: dict[str, str] = {}
    blank_roles: list[str] = []
    unusable_roles: list[str] = []

    for role_key, display_name in pairs:
        if not isinstance(display_name, str):
            continue  # None (unset) or a non-string value: not this check's concern
        if not display_name.strip():
            blank_roles.append(role_key)
            continue
        if not _sanitise_alias(display_name):
            # A display_name made entirely of non-alphanumeric characters
            # (e.g. "!!!") sanitises to "" -- distinct from "blank" (no
            # IndexError crash risk) but still leaves the role with NO
            # working display-name-derived alias, silently.
            unusable_roles.append(role_key)
            continue
        display_names[role_key] = display_name

    # Replay the SAME claim-attempt sequence the live registry's Phase 4
    # applies -- never a reimplementation, so this can never silently
    # disagree with what actually gets claimed/logged at runtime.
    claimed: dict[str, str] = {}
    attempts: dict[str, list[str]] = {}
    for alias, role_key in _display_name_alias_claims(display_names):
        seen = attempts.setdefault(alias, [])
        if role_key not in seen:
            seen.append(role_key)
        claimed.setdefault(alias, role_key)

    for alias, role_keys in sorted(attempts.items()):
        if len(role_keys) < 2:
            continue
        sorted_keys = sorted(role_keys)
        count = len(sorted_keys)
        winner = claimed[alias]
        losers = [k for k in sorted_keys if k != winner]
        findings.append(
            _finding(
                "error",
                "duplicate_role_display_name",
                f"{count} roles derive the same Telegram alias '{alias}' from their "
                f"display_name: {', '.join(sorted_keys)}",
                "the Telegram agent registry derives its addressing alias from "
                f"display_name -- {count - 1} of these {count} roles "
                f"({', '.join(losers)}) become unaddressable by name in chat channels; "
                f"mentions resolve to only one of them (currently '{winner}', "
                "deterministic claim order), and "
                "the engine only logs this as a 'telegram.agent_registry.alias_collision' "
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

    for role_key in sorted(unusable_roles):
        findings.append(
            _finding(
                "error",
                "unusable_role_display_name",
                f"Role '{role_key}' has a display_name that sanitises to an empty alias "
                "once accents/punctuation are stripped",
                "the Telegram agent registry's `_claim` silently no-ops on an empty "
                "alias -- this role gets NO display-name-derived alias at all and is "
                "only addressable via its role key or a curated alias, with zero "
                "warning at startup",
                f"set a display_name for role '{role_key}' that contains at least one "
                "letter or digit, or remove the display_name key entirely to use the "
                "role key as-is",
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
# Check: dangling claude_md / instruction_files references (incident #1,
# HIGHEST VALUE from the session that produced this module's second batch of
# checks). `ProjectConfig.claude_md` / `.instruction_files` name repository
# instruction files inlined into every agent's prompt (see
# `hivepilot.services.repo_instructions`). On the operator's box,
# `claude_md: CLAUDE.md` pointed at a file ABSENT from the repo, and nothing
# reported it -- for months every agent ran without the governance context
# the prompts asserted was provided inline.
#
# Reuses `repo_instructions.declared_instruction_files` /
# `.resolve_instruction_file_path` verbatim -- a check that disagrees with
# the runtime's own resolution is worse than no check at all.
# ---------------------------------------------------------------------------


def check_dangling_instruction_files(config_dir: Path | None) -> list[DoctorFinding]:
    from hivepilot.services.repo_instructions import (
        declared_instruction_files,
        resolve_instruction_file_path,
    )

    projects_data, findings = _load_yaml_checked(_doctor_path("projects.yaml", config_dir))
    projects_section, section_findings = _checked_container(
        projects_data, "projects", "'projects.yaml' key 'projects'"
    )
    findings.extend(section_findings)

    for project_name, project in projects_section.items():
        if not isinstance(project, dict):
            findings.append(
                _finding(
                    "error",
                    "malformed_project_entry",
                    f"Project '{project_name}' is not a mapping (got "
                    f"{type(project).__name__}) -- not checked",
                    "a project entry that isn't a mapping was previously skipped with "
                    "zero output, silently disabling its dangling-instruction-file check",
                    f"fix the '{project_name}' entry in projects.yaml to be a mapping",
                )
            )
            continue

        claude_md = project.get("claude_md")
        instruction_files = project.get("instruction_files")

        if claude_md is not None and not isinstance(claude_md, str):
            findings.append(
                _finding(
                    "error",
                    "invalid_instruction_file_declaration",
                    f"Project '{project_name}' claude_md must be a string, got "
                    f"{type(claude_md).__name__} -- not checked",
                    "a non-string claude_md cannot be resolved by "
                    "repo_instructions.resolve_instruction_file_path -- ProjectConfig "
                    "would reject this at real load time, but a raw YAML edit can still "
                    "produce it",
                    f"fix the 'claude_md' entry under project '{project_name}' in "
                    "projects.yaml to be a string filename",
                )
            )
            claude_md = None
        if instruction_files is not None and not isinstance(instruction_files, list):
            findings.append(
                _finding(
                    "error",
                    "invalid_instruction_file_declaration",
                    f"Project '{project_name}' instruction_files must be a list, got "
                    f"{type(instruction_files).__name__} -- not checked",
                    "a non-list instruction_files cannot be walked by "
                    "repo_instructions.declared_instruction_files",
                    f"fix the 'instruction_files' entry under project '{project_name}' in "
                    "projects.yaml to be a list of string filenames",
                )
            )
            instruction_files = None
        elif isinstance(instruction_files, list) and not all(
            isinstance(entry, str) for entry in instruction_files
        ):
            findings.append(
                _finding(
                    "error",
                    "invalid_instruction_file_declaration",
                    f"Project '{project_name}' instruction_files contains a non-string "
                    "entry -- not checked",
                    "a non-string entry cannot be resolved as a filename",
                    f"fix the 'instruction_files' list under project '{project_name}' in "
                    "projects.yaml so every entry is a string filename",
                )
            )
            instruction_files = None

        declared = declared_instruction_files(claude_md, instruction_files)
        if not declared:
            continue

        raw_path = project.get("path")
        if not raw_path or not isinstance(raw_path, str):
            findings.append(
                _finding(
                    "error",
                    "project_missing_path",
                    f"Project '{project_name}' declares instruction files "
                    f"({', '.join(declared)}) but has no valid 'path' to resolve them "
                    "against",
                    "repo_instructions resolves every declared file relative to the "
                    "project's path -- without one, resolution cannot be checked at all",
                    f"add a valid 'path' to project '{project_name}' in projects.yaml",
                )
            )
            continue

        project_path = Path(raw_path).expanduser()
        for declared_name in declared:
            resolved = resolve_instruction_file_path(project_path, declared_name)
            if not resolved.is_file():
                findings.append(
                    _finding(
                        "error",
                        "dangling_instruction_file",
                        f"Project '{project_name}' declares instruction file "
                        f"'{declared_name}' which does not resolve to a real file "
                        f"(resolved: {resolved}; searched directory: {resolved.parent})",
                        "a declared repository-instructions file that cannot be found is "
                        "silently absent from every agent's prompt -- the prompt still "
                        "asserts governance context was provided inline, but it never "
                        "was, and nothing reported this on the operator's box for months",
                        f"create '{declared_name}' at {resolved} (searched directory: "
                        f"{resolved.parent}), or fix/remove the reference in "
                        f"project '{project_name}''s projects.yaml entry",
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# Check: every project falls back to the single GLOBAL `Settings.
# obsidian_vault` while several projects/pipelines commonly coexist on one
# machine (incident #2).
#
# This USED to be reported as a permanent engine limitation with "no config
# fix today". The per-project-vault PRD added `ProjectConfig.obsidian_vault`,
# so there IS a fix now -- and a doctor that keeps reporting a solved
# limitation is noise (this module has already been through a
# 17-false-positives episode). Two changes:
#   * the finding now names the actual fix (`obsidian_vault:` in
#     projects.yaml), and
#   * it STOPS FIRING entirely once any project declares an override -- the
#     operator has demonstrably taken control of the routing.
# It also reports an override value that would refuse to load (empty or
# relative), which would otherwise surface only as a projects.yaml load
# failure with no pointer to the vault key.
# ---------------------------------------------------------------------------


def _raw_vault_override(entry: object) -> object | None:
    """The raw `obsidian_vault` value from a projects.yaml entry, if present."""
    if not isinstance(entry, dict):
        return None
    if "obsidian_vault" not in entry:
        return None
    return entry["obsidian_vault"]


def check_shared_obsidian_vault(config_dir: Path | None) -> list[DoctorFinding]:
    if not settings.obsidian_enabled:
        return []

    projects_data, findings = _load_yaml_checked(_doctor_path("projects.yaml", config_dir))
    projects_section, section_findings = _checked_container(
        projects_data, "projects", "'projects.yaml' key 'projects'"
    )
    findings.extend(section_findings)

    # An override that `ProjectConfig.validate_obsidian_vault` rejects makes
    # the WHOLE projects.yaml fail to load, so name the offending key here.
    # Deterministic: mirrors the validator's two rejection rules exactly, so
    # it can never be a false positive.
    overrides: dict[str, object] = {}
    for project_name, entry in projects_section.items():
        raw = _raw_vault_override(entry)
        if raw is None:
            continue
        overrides[str(project_name)] = raw
        text = str(raw).strip()
        if not text:
            findings.append(
                _finding(
                    "error",
                    "project_vault_override_invalid",
                    f"project '{project_name}' declares an EMPTY `obsidian_vault:` override",
                    "an empty override is never interpreted as 'use the global vault' -- "
                    "that would silently route this project's artifacts back into the very "
                    "vault you were moving away from. projects.yaml will refuse to load",
                    "remove the `obsidian_vault:` key entirely to inherit "
                    "HIVEPILOT_OBSIDIAN_VAULT, or set a real absolute path",
                )
            )
        elif not Path(text).expanduser().is_absolute():
            findings.append(
                _finding(
                    "error",
                    "project_vault_override_invalid",
                    f"project '{project_name}' declares a RELATIVE `obsidian_vault:` "
                    f"override ({text!r})",
                    "a relative vault path resolves against whatever working directory the "
                    "daemon happens to have, so the same config writes artifacts to a "
                    "different place depending on how HivePilot was started. projects.yaml "
                    "will refuse to load",
                    f"use an absolute path (or one starting with '~/') instead of {text!r}",
                )
            )

    if overrides:
        # The operator is routing vaults per project -- the shared-vault
        # limitation no longer applies to this deployment. Stay quiet.
        return findings

    project_count = len(projects_section)
    if project_count < 2:
        return findings

    findings.append(
        _finding(
            "info",
            "shared_obsidian_vault",
            f"{project_count} projects are configured and none declares an "
            "`obsidian_vault:` override -- every project's HivePilot artifacts land in "
            "the SAME vault (the global Settings.obsidian_vault)",
            "the global vault is a machine-wide default. Several pipelines on the same "
            "host (e.g. your own HivePilot work vs. a product pipeline) normally want "
            "different vaults; sharing one is fine if that is what you intended, but it "
            "is a default rather than a decision",
            "set `obsidian_vault: /absolute/path/to/vault` on the projects that need "
            "their own destination in projects.yaml (must be absolute, must already "
            "exist -- HivePilot never creates it). Projects without the key keep using "
            "HIVEPILOT_OBSIDIAN_VAULT. This finding stops appearing once any project "
            "declares an override",
        )
    )
    return findings


# ---------------------------------------------------------------------------
# Check: vault writes are never committed (incident #3). `obsidian_service.py`
# has NO git capability at all -- on the operator's box the vault sat 67
# files uncommitted for 6 days: the "brain" recorded nothing durably unless a
# human remembered to commit. Local git state only (no fetch/network calls),
# consistent with this doctor's offline discipline.
# ---------------------------------------------------------------------------


def check_vault_git_state() -> list[DoctorFinding]:
    vault_path = settings.resolve_path(settings.obsidian_vault)
    if not vault_path.is_dir():
        return []  # vault not created yet -- nothing written, nothing to report

    from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo

    try:
        repo = Repo(vault_path, search_parent_directories=True)
    except (InvalidGitRepositoryError, NoSuchPathError):
        return [
            _finding(
                "info",
                "vault_not_git_repo",
                f"The Obsidian vault at {vault_path} is not a git repository",
                "every artifact HivePilot writes to this vault exists ONLY on this host "
                "until a human turns it into a git repo and commits -- on the operator's "
                "box, 67 files sat uncommitted for 6 days because nothing records vault "
                "writes durably by default",
                "run `git init` in the vault (and add a remote) if you want vault writes "
                "to be durable/shareable, or ignore this if the vault is deliberately "
                "local-only",
            )
        ]
    except Exception as exc:  # noqa: BLE001 -- "could not inspect this" must be a
        # finding, never silence or a crash: mirrors this module's governing rule.
        return [
            _finding(
                "error",
                "vault_git_state_check_failed",
                f"Could not inspect the git state of the vault at {vault_path}: "
                f"{type(exc).__name__}",
                "a broken git checkout at the vault path would otherwise silently "
                "disable this check with no output at all",
                f"inspect the vault's git state manually, e.g. `git -C {vault_path} status`",
            )
        ]

    try:
        porcelain = repo.git.status("--porcelain")
    except Exception as exc:  # noqa: BLE001 -- same "never silent" rule as above
        return [
            _finding(
                "error",
                "vault_git_state_check_failed",
                f"Could not read git status for the vault at {vault_path}: {type(exc).__name__}",
                "an unreadable git status silently disables the uncommitted-artifacts "
                "check unless reported explicitly",
                f"inspect the vault's git state manually, e.g. `git -C {vault_path} status`",
            )
        ]

    findings: list[DoctorFinding] = []
    uncommitted = len([line for line in porcelain.splitlines() if line.strip()])
    if uncommitted:
        findings.append(
            _finding(
                "warning",
                "vault_uncommitted_artifacts",
                f"{uncommitted} artifact(s) written to the Obsidian vault are "
                "uncommitted -- they exist only on this host",
                "HivePilot's obsidian_service has no git capability at all: nothing "
                "commits a vault write unless a human remembers to. On the operator's "
                "box this reached 67 uncommitted files across 6 days before anyone "
                "noticed",
                f"run `git -C {vault_path} add -A && git -C {vault_path} commit`, or "
                "enable settings.auto_commit_vault (HIVEPILOT_AUTO_COMMIT_VAULT) so "
                "pipeline runs commit automatically",
            )
        )

    try:
        ahead_raw = repo.git.rev_list("@{u}..HEAD", "--count").strip()
        ahead = int(ahead_raw) if ahead_raw else 0
    except GitCommandError:
        # No upstream configured for the current branch -- a deliberately
        # local-only vault, not a problem this check should report: there is
        # nowhere to push these commits TO.
        ahead = 0
    if ahead:
        findings.append(
            _finding(
                "warning",
                "vault_unpushed_commits",
                f"{ahead} commit(s) in the Obsidian vault are committed locally but never pushed",
                "a commit that only exists on this host is one disk failure away from "
                "being the operator's only durable record of what HivePilot did",
                f"run `git -C {vault_path} push`",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Check: display-timestamps-local incident -- an operator was told a run
# "failed this morning at 09:08" for an event that happened at 11:08 local
# time, because every human-facing surface rendered the stored UTC value
# with no conversion and no marker. `hivepilot.utils.display_time` is the
# fix; this check catches the two ways that fix itself can be misconfigured
# WITHOUT reproducing the noise-flood incident from
# `check_enabled_plugins_loaded`'s docstring (17 false positives out of 19
# findings) -- it only reports a REAL discrepancy:
#   * an explicit `display_timezone` override that doesn't resolve to a
#     real IANA zone (a typo) -- an ERROR, since every render call silently
#     falls back to the system zone instead of what the operator asked for.
#   * no override AND the host's system timezone couldn't be detected
#     either -- a WARNING, since rendering then falls back to UTC while
#     still looking like local time (the exact class of bug this fixes).
# A correctly configured system (explicit valid override, OR no override
# with a detectable system zone) is completely silent.
# ---------------------------------------------------------------------------


def check_display_timezone() -> list[DoctorFinding]:
    from hivepilot.utils import display_time

    override = settings.display_timezone
    if override:
        try:
            display_time.ZoneInfo(override)
        except (display_time.ZoneInfoNotFoundError, ValueError):
            return [
                _finding(
                    "error",
                    "invalid_display_timezone",
                    f"HIVEPILOT_DISPLAY_TIMEZONE={override!r} is not a valid IANA timezone name",
                    "every human-facing surface (Telegram/Slack/Discord/Signal chat "
                    "replies, the NL concierge, CLI tables) silently falls back to the "
                    "detected system timezone instead of the one you configured -- "
                    "rendered times will be wrong by whatever offset separates the two, "
                    "with no error visible anywhere except this check",
                    "set HIVEPILOT_DISPLAY_TIMEZONE to a real IANA zone name, e.g. "
                    "'Europe/Paris' (see the IANA tz database for the full list)",
                )
            ]
        return []

    if display_time.detect_system_zone_name() is not None:
        return []

    return [
        _finding(
            "warning",
            "display_timezone_fallback_utc",
            "No HIVEPILOT_DISPLAY_TIMEZONE is set, and this host's system timezone "
            "could not be detected (no TZ env var, /etc/timezone, or resolvable "
            "/etc/localtime symlink) -- every human-facing surface falls back to "
            "rendering UTC",
            "a UTC-fallback render still carries a 'UTC' marker, so it will not "
            "silently be MISREAD as local time -- but every timestamp shown to the "
            "operator will still be offset from local time until this is fixed, "
            "exactly the incident this check exists to catch early",
            "set HIVEPILOT_DISPLAY_TIMEZONE explicitly to the operator's IANA zone "
            "name, e.g. 'Europe/Paris', or fix the host's system timezone "
            "configuration (tzdata/setup-timezone)",
        )
    ]


# ---------------------------------------------------------------------------
# check_cost_accounting (usage-capture-modelusage fix) -- `budget_daily_usd`,
# the autopilot admission check, and the partition-dispatch cost gate all
# read `analytics_service.cost_summary`'s totals (via `autopilot_queue.
# spent_today_usd`); a ceiling computed from an understated cost protects
# nothing. This check has TWO independent triggers:
#
#   1. An ABNORMAL SHARE of steps with a recorded model have no cost signal
#      at all (no self-reported `cost_usd` and no price-map match) --
#      symptomatic of a parsing bug (the incident this fix addresses) or a
#      price map that's fallen behind reality.
#   2. A SPECIFIC model id has NEVER once been priced -- surfaced even when
#      it's a small share of overall traffic, since a whole model silently
#      contributing $0.0 forever is a real, actionable gap regardless of
#      its volume.
#
# Anti-noise (a real production lesson -- an earlier check, #4b, reported 17
# false positives out of 19 and the operator stopped reading it):
#   - The "unknown" bucket (NULL model -- e.g. a shell/non-agent runner that
#     never had a cost concept in the first place) is EXCLUDED entirely from
#     both triggers -- that's a legitimate "cost doesn't apply" case, never
#     an anomaly.
#   - Both triggers are sample-size-gated (a brand-new install with a
#     handful of steps must never fire).
#   - A model that's absent from the static price map but self-reports
#     `cost_usd` on every step is NOT flagged by trigger 2 -- the price map
#     is only a fallback, so an absent entry is harmless as long as nothing
#     ever needs it.
#
# Window scoping (cost-check-window fix, 2026-07-28): this check's own
# introduction (the block above) hard-coded the SYMPTOM this fix addresses
# without accounting for its own history -- the fix itself only landed on
# 2026-07-27 (PR #352, commit 5c2f5324). Before it, `steps.model` persisted
# a bare CLI alias and NO tokens (base or cache) were ever captured at all
# (see `claude_runner._parse_usage_envelope`'s history) -- those rows can
# NEVER be priced (`cost_backfill.py` requires at least one of input/output
# tokens to recompute anything). Counting them toward the unpriced-share
# ratio measures "did the old, already-fixed bug exist" (permanently true,
# forever) instead of "is the CURRENT instrumentation healthy" -- on a real
# production box this reported 36/48 (75%) unpriced and would have stayed
# wrong for a full 30 days after the fix landed.
#
# Both triggers below are therefore scoped to steps recorded AT OR AFTER
# `_resolve_cost_instrumentation_boundary()` -- see `_MODELUSAGE_FIX_
# LANDED_AT`'s own comment for why that boundary is a FIXED instant, never
# derived from a step's own shape. Steps before it are reported separately,
# as an `info` line stating the real excluded count (never hidden) --
# see `cost_accounting_pre_instrumentation_steps` below.
# ---------------------------------------------------------------------------

_COST_ACCOUNTING_MIN_TOTAL_SAMPLE = 10
_COST_ACCOUNTING_UNPRICED_SHARE_THRESHOLD = 0.15
_COST_ACCOUNTING_MIN_MODEL_SAMPLE = 5

# `analytics_service._TS_FORMAT` -- the exact lexical shape `steps.timestamp`
# is stored/compared in (sqlite `CURRENT_TIMESTAMP`, naive UTC). Duplicated
# as a literal (not imported) because this module already mirrors other
# modules' private helpers/constants locally rather than depending on their
# underscored names across files (see `_walk_xdg_rank`'s docstring for the
# same rationale) -- this format has been stable since Phase 24b and is
# exercised by both modules' own test suites, so drift would be caught fast.
_COST_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# The usage-capture-modelusage fix (PR #352, commit 5c2f5324, merged
# 2026-07-27) is the ONLY thing that makes a step's cost signal possible at
# all -- before it, `steps.model` persisted a bare CLI alias and NO tokens
# were ever captured (see the module comment above `check_cost_accounting`).
# Every step recorded before this fix reached a box is, by construction,
# unpriceable forever -- `cost_backfill.py` cannot recover a row that never
# had any tokens at all.
#
# This is a FIXED, code-owned instant -- deliberately NEVER derived from the
# SHAPE of the rows it grades (e.g. "the earliest row with a canonical model
# id" or "the earliest row with non-null cache tokens"). A shape-derived
# boundary is recomputed every run: a FUTURE regression that reintroduces
# the exact same symptom (bare alias, zero tokens) would, by that same
# shape-based rule, look indistinguishable from "instrumentation hasn't
# started yet" -- silently sliding the boundary forward and hiding the very
# regression this check exists to catch. A fixed historical instant cannot
# be moved by anything a runner writes after the fact -- a regression next
# month produces POST-boundary rows, which this check will still measure
# and flag (see `TestCheckCostAccountingBoundary::
# test_regression_after_boundary_is_still_caught`).
#
# Rejected alternatives:
#   * "the earliest step with a canonical model id / non-null cache-token
#     columns" -- exactly the shape-derived rule above; rejected for the
#     reason above.
#   * "a recorded schema-migration timestamp" -- this fix's own migration
#     (the `cache_read_tokens`/`cache_creation_tokens` columns added in
#     `state_service.init_db`) is genuinely the right EVENT to anchor on,
#     but this codebase has no migration-applied-at ledger: every migration
#     here is a bare idempotent `ALTER TABLE ... ADD COLUMN` with no record
#     of WHEN it first ran. Retrofitting one now could only be backfilled,
#     on a box that already upgraded before the ledger existed (this
#     operator's box included), from the data's own shape -- reintroducing
#     exactly the gaming risk being avoided. Not worth the new schema
#     surface for a boundary the code already knows by construction.
#   * an ALWAYS-required operator-set config value with no built-in default
#     -- correct in principle (purely human-supplied, never inferred), but
#     it would mean this check reports nothing useful on ANY box until an
#     operator manually configures it -- the opposite of "fixes the
#     operator's box automatically". Used below as an ESCAPE HATCH instead
#     (`cost_instrumentation_since`) for an operator who deployed this fix
#     (or a LATER fix for a similar regression) at a materially different
#     time -- but nobody has to touch it for the common case.
_MODELUSAGE_FIX_LANDED_AT = "2026-07-27 00:00:00"  # UTC; PR #352 / 5c2f5324


def _parse_cost_boundary_ts(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_cost_instrumentation_boundary() -> tuple[datetime | None, list[DoctorFinding]]:
    """Return ``(boundary, findings)``. ``boundary`` is the tz-aware UTC
    instant before which cost signal is never expected to exist. ``None``
    only when an EXPLICIT ``cost_instrumentation_since`` override could not
    be parsed -- paired with an `error` finding: this module's fail-closed
    rule is "I could not inspect this" must be a finding, never silence, and
    silently falling back to the built-in default here would mask an
    operator's deliberate re-pin (e.g. after a later fix) behind a value
    they no longer control. The built-in default always parses, so absence
    of an override can never reach this failure path.
    """
    override = getattr(settings, "cost_instrumentation_since", None)
    if not override:
        return _parse_cost_boundary_ts(_MODELUSAGE_FIX_LANDED_AT), []

    parsed = _parse_cost_boundary_ts(override)
    if parsed is None:
        return None, [
            _finding(
                "error",
                "cost_instrumentation_boundary_unparseable",
                f"Setting 'cost_instrumentation_since' ({override!r}) could not be parsed "
                "as a timestamp -- the cost-accounting unpriced-share check could not run",
                "this setting is the boundary before which cost signal is never expected; "
                "an unparseable override must never be silently ignored in favour of the "
                "built-in default, which the operator has explicitly chosen to override",
                "set HIVEPILOT_COST_INSTRUMENTATION_SINCE to an ISO-8601 timestamp (e.g. "
                "'2026-07-27T00:00:00+00:00'), or unset it to use the built-in default",
            )
        ]
    return parsed, []


def check_cost_accounting() -> list[DoctorFinding]:
    """Flag an abnormal share of unpriced steps, or a recorded model id that
    has never once been priced -- see the module-level comment block above
    for the full trigger/anti-noise rationale. Both triggers are scoped to
    steps recorded AT OR AFTER `_resolve_cost_instrumentation_boundary()`,
    intersected with the same 30-day window the cost dashboard shows
    (`analytics_service.cost_summary`'s default) -- steps excluded by the
    boundary are reported separately via an `info` line, never hidden.
    """
    from hivepilot.services import analytics_service
    from hivepilot.services.pricing import _effective_price_map

    boundary, findings = _resolve_cost_instrumentation_boundary()
    if boundary is None:
        return findings  # fail-closed: cannot scope the ratio without a boundary

    window_start = datetime.now(timezone.utc) - timedelta(days=30)
    effective_since = max(window_start, boundary).strftime(_COST_TS_FORMAT)

    summary_30d = analytics_service.cost_summary(days=30)
    known_30d = [row for row in summary_30d["by_model"] if row["model"] != "unknown"]
    total_30d = sum(row["total_steps"] for row in known_30d)

    summary = analytics_service.cost_summary(days=None, since=effective_since)
    known_models = [row for row in summary["by_model"] if row["model"] != "unknown"]
    total_known_steps = sum(row["total_steps"] for row in known_models)

    excluded = total_30d - total_known_steps
    if excluded > 0:
        findings.append(
            _finding(
                "info",
                "cost_accounting_pre_instrumentation_steps",
                f"{excluded} step(s) with a recorded model in the last 30 days predate the "
                "cost-instrumentation fix and are excluded from the unpriced-share ratio "
                "below -- they have no tokens captured at all and can never be priced",
                "these rows permanently understate any historical cost total that includes "
                "them (dashboard, budget_daily_usd, autopilot admission) for a known, "
                "already-fixed reason -- surfaced here so that is explained rather than "
                "left looking like an ongoing accounting problem",
                "no action needed for these specific rows -- `hivepilot costs backfill "
                "--dry-run` confirms they have no tokens to recompute from",
            )
        )

    total_unpriced = sum(row["unpriced_steps"] for row in known_models)
    if total_known_steps >= _COST_ACCOUNTING_MIN_TOTAL_SAMPLE:
        share = total_unpriced / total_known_steps
        if share > _COST_ACCOUNTING_UNPRICED_SHARE_THRESHOLD:
            unpriced_models = sorted(
                row["model"] for row in known_models if row["unpriced_steps"] > 0
            )
            findings.append(
                _finding(
                    "warning",
                    "cost_accounting_unpriced_share",
                    f"{total_unpriced}/{total_known_steps} ({share:.0%}) of steps with a "
                    "recorded model since the cost-instrumentation fix landed have NO cost "
                    f"signal at all (no self-reported cost, no price-map match) -- affected "
                    f"model(s): {', '.join(unpriced_models)}",
                    "`budget_daily_usd`, the autopilot admission check, and the "
                    "partition-dispatch cost gate all read this same total (`analytics_"
                    "service.cost_summary` via `autopilot_queue.spent_today_usd`) -- a "
                    "ceiling computed from an understated cost protects nothing",
                    "add pricing for the listed model(s) via HIVEPILOT_LLM_PRICE_MAP, "
                    "confirm the recorded model id is a real canonical id (not a bare CLI "
                    "alias or a stale one), or run `hivepilot costs backfill --dry-run` to "
                    "see how many historical rows a price-map fix would recover",
                )
            )

    price_map = _effective_price_map()
    never_priced = sorted(
        row["model"]
        for row in known_models
        if row["total_steps"] >= _COST_ACCOUNTING_MIN_MODEL_SAMPLE
        and row["unpriced_steps"] == row["total_steps"]
        and row["model"] not in price_map
    )
    if never_priced:
        findings.append(
            _finding(
                "warning",
                "cost_accounting_model_missing_from_price_map",
                f"model id(s) {', '.join(never_priced)} have never had a single priced step "
                "since the cost-instrumentation fix landed and are absent from the "
                "effective price map",
                "every step for these models silently contributes $0.0 to every cost total "
                "(dashboard, budget_daily_usd, autopilot admission, partition-dispatch cost "
                "gate) -- indistinguishable from 'genuinely free' unless you already know to "
                "check unpriced_steps",
                "add an entry for each listed model to HIVEPILOT_LLM_PRICE_MAP (see "
                "hivepilot.services.pricing.DEFAULT_PRICE_MAP for the {input, output, "
                "cache_read, cache_write} shape), or confirm the runner's CLI is expected "
                "to self-report its own cost instead",
            )
        )

    return findings


# ---------------------------------------------------------------------------
# check_partition_readiness -- propose->ratify->dispatch PRD, spec section 9
# (S5). Both findings below describe a partition setup that is CONFIGURED but
# silently cannot do what the operator believes it does:
#
#   1. `claude_max_concurrency` defaults to 1 (`config.py`), and
#      `runner_throttle` caps every `claude`-kind runner at it -- so a plan
#      that says "3 parallel agents" is one agent three times. The ratify
#      view already shows the computed effective number at the moment of
#      dispatch; this check surfaces the same truth BEFORE a plan is written
#      around a parallelism the host will not deliver.
#   2. `max_partition_cost_usd` is the partition admission ceiling and is
#      fail-closed (`autopilot_policy`: absent / null / non-numeric / bool /
#      zero / negative all resolve to `None`, which
#      `partition_service._check_policy` turns into "no partition may be
#      ratified for it"). Without it, the feature looks configured and
#      refuses every single plan -- and the refusal only appears after a
#      human has already reviewed a partition and pressed dispatch.
#
# Anti-noise contract (incident #4b: 17 false positives out of 19 findings
# and the operator stopped reading the report):
#   - A project that is NOT partition-capable is never a finding. "Partition-
#     capable" means the operator explicitly wrote at least one of the three
#     partition policy keys somewhere that applies to that project -- an
#     ordinary project that has simply never heard of partitions is not a
#     misconfiguration.
#   - Each check emits ONE finding naming every affected project, never one
#     finding per project.
# ---------------------------------------------------------------------------

# The three per-project keys `autopilot_policy.AutopilotPolicy` adds for
# partitions. Presence of ANY of them (in the project block or the inherited
# `default` block) is what "the operator intends to use partitions here"
# means -- there is no separate `partitions_enabled` flag to read.
_PARTITION_POLICY_KEYS = (
    "outward_actions",
    "max_partition_cost_usd",
    "max_task_wall_clock_seconds",
)


def _partition_capable_projects(
    config_dir: Path | None,
) -> tuple[dict[str, dict[str, Any]], list[DoctorFinding]]:
    """Return ``{project_name: merged policy block}`` for every project that
    is partition-capable, plus any findings raised while inspecting the
    config.

    The merge order (``policies.default`` then ``policies.projects.<name>``,
    project wins) is deliberately the same one
    ``autopilot_policy.get_autopilot_policy`` uses, so this check can never
    call a project healthy that the real gate would refuse.

    Every "I could not inspect this" path yields a finding rather than
    silence: an unparseable file, a non-mapping ``policies:``/``projects:``
    container, and a policy scope that is not a mapping.
    """
    findings: list[DoctorFinding] = []

    projects_data, projects_findings = _load_yaml_checked(_doctor_path("projects.yaml", config_dir))
    findings.extend(projects_findings)
    known_projects, known_findings = _checked_container(
        projects_data, "projects", "'projects.yaml' key 'projects'"
    )
    findings.extend(known_findings)

    policies_data, policies_findings = _load_yaml_checked(_doctor_path("policies.yaml", config_dir))
    findings.extend(policies_findings)
    policies_section, section_findings = _checked_container(
        policies_data, "policies", "'policies.yaml' key 'policies'"
    )
    findings.extend(section_findings)

    default_block = policies_section.get("default") or {}
    if not isinstance(default_block, dict):
        findings.append(
            _finding(
                "error",
                "malformed_policy_entry",
                "Policy 'default' is not a mapping (got "
                f"{type(default_block).__name__}) -- partition readiness not checked",
                "every project inherits the default block, so a malformed one silently "
                "removes the partition keys from EVERY project's effective policy",
                "fix the 'default' entry in policies.yaml to be a mapping",
            )
        )
        default_block = {}

    scoped, scoped_findings = _checked_container(
        policies_section, "projects", "'policies.yaml' key 'policies.projects'"
    )
    findings.extend(scoped_findings)

    capable: dict[str, dict[str, Any]] = {}
    for project_name in sorted(known_projects):
        project_block = scoped.get(project_name) or {}
        if not isinstance(project_block, dict):
            findings.append(
                _finding(
                    "error",
                    "malformed_policy_entry",
                    f"Policy '{project_name}' is not a mapping (got "
                    f"{type(project_block).__name__}) -- partition readiness not checked "
                    "for it",
                    "a policy scope that isn't a mapping is skipped with zero output, "
                    "silently hiding whether that project is partition-capable at all",
                    f"fix the '{project_name}' entry in policies.yaml to be a mapping",
                )
            )
            continue
        merged = {**default_block, **project_block}
        if any(key in merged for key in _PARTITION_POLICY_KEYS):
            capable[project_name] = merged

    return capable, findings


def check_partition_readiness(config_dir: Path | None = None) -> list[DoctorFinding]:
    """Two actionable checks over partition-capable projects -- see the
    comment block above for the incident each one maps to and for the
    anti-noise contract they are both written against."""
    # The gate's OWN fail-closed coercion, imported rather than
    # re-implemented: a local copy would drift, and the two rules that make
    # this check correct (a `bool` is not a number even though it is an
    # `int`; zero and negative resolve to "deny", never to "unbounded") are
    # exactly the ones a re-implementation would get wrong.
    from hivepilot.services.autopilot_policy import _positive_float

    capable, findings = _partition_capable_projects(config_dir)
    if not capable:
        return findings

    missing_ceiling = sorted(
        name
        for name, merged in capable.items()
        if _positive_float(merged.get("max_partition_cost_usd")) is None
    )
    if missing_ceiling:
        named = ", ".join(missing_ceiling)
        findings.append(
            _finding(
                "error",
                "partition_missing_cost_ceiling",
                f"project(s) {named} are partition-capable (policies.yaml sets at least one "
                f"of {', '.join(_PARTITION_POLICY_KEYS)}) but have no positive "
                "max_partition_cost_usd -- the ratification gate refuses every partition "
                "naming them",
                "max_partition_cost_usd is the partition admission ceiling and is "
                "fail-closed: absent, null, non-numeric, zero and negative all resolve to "
                "'no partition may be ratified for this project', never to an unbounded "
                "ceiling -- so the feature looks configured and silently denies, and the "
                "denial only surfaces after a human has reviewed a plan and pressed "
                "dispatch",
                "set a positive max_partition_cost_usd under policies.projects.<name> (or "
                "policies.default) in policies.yaml; the same scope also needs a positive "
                "budget_daily_usd and max_task_wall_clock_seconds, which are fail-closed "
                "the same way. If these projects are not meant to be partition-capable, "
                "remove their partition keys instead",
            )
        )

    runner_cap = getattr(settings, "claude_max_concurrency", 1)
    try:
        runner_cap = int(runner_cap)
    except (TypeError, ValueError):
        runner_cap = 1
    if runner_cap <= 1:
        named = ", ".join(sorted(capable))
        findings.append(
            _finding(
                "warning",
                "partition_parallelism_capped_at_one",
                f"claude_max_concurrency={runner_cap} while project(s) {named} are "
                "partition-capable -- a partition asking for N parallel agents runs one "
                "agent N times on this host",
                "runner_throttle caps every claude-kind runner at "
                "settings.claude_max_concurrency (default 1), so 'max_parallel: 3' is not "
                "3x faster, it is the same work serialised -- and each task's "
                "wall_clock_seconds ceiling was almost certainly sized for the parallel "
                "case, so the later waves are the ones that get killed",
                "raise HIVEPILOT_CLAUDE_MAX_CONCURRENCY to the number of concurrent claude "
                "sessions this host can genuinely afford, or keep partitions honest by "
                "planning them with policy.max_parallel: 1 so the plan states what "
                "actually happens",
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

    # Liveness: what actually RUNS, as opposed to what is configured. Lives in
    # its own module (this one is long enough) and is imported here rather than
    # at module scope because it imports back into this one for `_finding`.
    #
    # Only for the LIVE deployment. An explicit `config_dir` means "validate
    # this directory's config", and the state DB, vault, hooks and topic
    # registry of the process doing the validating say nothing about it --
    # they would report the auditor's own environment as findings against
    # someone else's config.
    if config_dir is None:
        from hivepilot.services import doctor_liveness

        findings.extend(_run_check("state_db_liveness", doctor_liveness.check_state_db_liveness))
        findings.extend(_run_check("vault_liveness", doctor_liveness.check_vault_liveness))
        findings.extend(_run_check("cache_amortisation", doctor_liveness.check_cache_amortisation))
        findings.extend(_run_check("lessons_learn", doctor_liveness.check_lessons_learn))
        findings.extend(
            _run_check(
                "registered_hooks",
                lambda: doctor_liveness.check_registered_hooks(plugin_manager),
            )
        )
        findings.extend(
            _run_check(
                "plugins_written_vs_installed",
                lambda: doctor_liveness.check_plugins_written_vs_installed(plugin_manager),
            )
        )
        findings.extend(_run_check("orphan_topic_keys", doctor_liveness.check_orphan_topic_keys))
        findings.extend(_run_check("agent_privilege", doctor_liveness.check_agent_privilege))

        # Which agent CLI is installed, and at what version. Offline: the
        # registry lookup lives behind `hivepilot agents versions
        # --check-latest`, because a health command that needs the internet
        # stops being run on the box that has no outbound access.
        from hivepilot.services import agent_versions

        findings.extend(_run_check("agent_cli_versions", agent_versions.check_agent_cli_versions))

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
    findings.extend(
        _run_check(
            "check_dangling_instruction_files",
            lambda: check_dangling_instruction_files(config_dir),
        )
    )
    findings.extend(
        _run_check(
            "check_shared_obsidian_vault",
            lambda: check_shared_obsidian_vault(config_dir),
        )
    )
    findings.extend(_run_check("check_vault_git_state", check_vault_git_state))
    findings.extend(_run_check("check_display_timezone", check_display_timezone))
    findings.extend(_run_check("check_retry_queue_backlog", check_retry_queue_backlog))
    findings.extend(_run_check("check_cost_accounting", check_cost_accounting))
    findings.extend(
        _run_check(
            "check_partition_readiness",
            lambda: check_partition_readiness(config_dir),
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
