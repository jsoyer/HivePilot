from __future__ import annotations

import importlib.metadata as metadata
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, TypedDict

from hivepilot.config import settings
from hivepilot.services import token_service
from hivepilot.utils.logging import get_logger

if TYPE_CHECKING:
    # Deferred to type-checking only: `hivepilot.graph` imports FROM this
    # module (`PanelStatSection`/`PanelTableSection`/`PanelTextSection`/
    # `normalize_panel_data`) at its own top level, so a runtime top-level
    # import here would be circular. Every real (non-type-checking) use of
    # `GraphSourceSpec`/`register_graph_source`/`GraphSourceNameCollisionError`
    # below is a LAZY, function-local import instead — same pattern the
    # `RUNNER_MAP`/`NOTIFIER_MAP`/`SECRETS_MAP` imports inside `_load_into`
    # already use for their own circular-import-prone dependencies.
    pass

logger = get_logger(__name__)

PLUGIN_ENTRY_POINT_GROUP = "hivepilot.plugins"

# The closed set of values `PluginRecord.source` may take:
# - "local-file": discovered by `_scan_local_plugins` scanning `plugins/*.py`
# - "entry-point": discovered via the `hivepilot.plugins` entry-point group
# - "explicit-entry": the single `settings.plugins_entry` module-path pin
#   (`HIVEPILOT_PLUGINS_ENTRY`) — an arbitrary `module:attr` import that
#   bypasses both discovery mechanisms above, distinct from "local-file"
#   even though it's often used to point at a file under `plugins/` (see
#   `PluginManager.__init__`)
# - "built-in": reserved for a future non-plugin baseline record; not
#   currently produced by any loader in this module
PLUGIN_RECORD_SOURCES = ("local-file", "entry-point", "explicit-entry", "built-in")


@dataclass(slots=True)
class PluginRecord:
    name: str
    source: str
    location: str
    # Contribution-type -> sorted list of names THIS plugin registered,
    # e.g. {"runners": ["hugo"], "notifiers": ["obsidian"], "hooks":
    # ["before_step"]}. Populated by `PluginManager.__init__` as each
    # plugin's `register()` result is applied — a contribution rolled back
    # due to a collision (see the atomic rollback block below) is never
    # recorded here. Defaults to an empty dict for backward compatibility
    # (e.g. hand-constructed `PluginRecord`s in tests/fixtures).
    contributions: dict[str, list[str]] = field(default_factory=dict)


# Valid `HealthStatus.status` values a health check may report.
HEALTH_STATUSES = ("ok", "degraded", "error")


class HealthStatus(NamedTuple):
    """Small typed result a plugin's `health` callable returns.

    Importable by plugins the same way they already import other hivepilot
    symbols (`from hivepilot.plugins import HealthStatus`). `status` must be
    one of `HEALTH_STATUSES`; `detail` is a one-line, human-readable string
    that must NEVER contain a secret/token value (Phase 19 discipline) —
    presence/config booleans and names only.
    """

    status: str
    detail: str


class HealthNameCollisionError(RuntimeError):
    """Raised when two plugins declare a `health` check under the same name.

    Mirrors `RunnerKindCollisionError` / `NotifierKindCollisionError` /
    `SecretsBackendCollisionError` (`hivepilot/registry.py` /
    `hivepilot/services/notification_service.py`) — a hard stop, not a
    silent last-wins overwrite, so a plugin can never shadow another
    plugin's health check unnoticed.
    """


def _normalize_health_result(result: Any) -> HealthStatus:
    """Coerce a health callable's return value into a `HealthStatus`.

    Accepts a `HealthStatus` instance, any duck-typed object exposing
    `.status`/`.detail` attributes (e.g. a plugin's own locally-defined
    namedtuple/dataclass with the same shape), or a plain
    `{"status": ..., "detail": ...}` dict (the documented no-import
    fallback). Anything else, or an invalid `status` value, normalizes to
    `HealthStatus("error", ...)` describing the problem — this function
    itself never raises.
    """
    if isinstance(result, HealthStatus):
        return result

    status: Any = None
    detail: Any = None
    if isinstance(result, dict):
        status = result.get("status")
        detail = result.get("detail")
    elif hasattr(result, "status") and hasattr(result, "detail"):
        status = result.status
        detail = result.detail
    else:
        return HealthStatus("error", f"invalid health check result type: {type(result).__name__}")

    if status not in HEALTH_STATUSES:
        return HealthStatus("error", f"invalid health status: {status!r}")
    return HealthStatus(status, str(detail) if detail is not None else "")


# Mirador panel plugin type (Sprint 1 — routing + contracts only; TUI
# rendering is Sprint 2, web is Sprint 3). A plugin contributes renderer-agnostic
# panels via `register()["panels"] = [PanelSpec, ...]`. `PanelSpec` is a PLAIN
# DICT at runtime (TypedDict is a type-checking-only construct — no dataclass
# on the importlib-plugin path, matching every other contribution type here).

# The closed set of section `kind` values a panel's PanelData may contain.
PANEL_SECTION_KINDS = ("stat", "table", "text")

# Valid `stat` section `status` values (mirrors HEALTH_STATUSES). `None` is
# also a valid status (no status badge) but is handled separately since it is
# the "unset" value, not a member of this enum.
PANEL_STAT_STATUSES = ("ok", "warn", "error")


class PanelStatSection(TypedDict):
    kind: str  # literal "stat"
    label: str
    value: str
    status: str | None


class PanelTableSection(TypedDict):
    kind: str  # literal "table"
    columns: list[str]
    rows: list[list[str]]


class PanelTextSection(TypedDict):
    kind: str  # literal "text"
    content: str


class PanelData(TypedDict):
    """Renderer-agnostic panel payload: a closed set of section kinds.

    Returned by a panel's `fetch()` callable (see `PanelSpec`) and by
    `run_panel_fetch`. Always pass through `normalize_panel_data` before
    trusting a panel's raw returned value — it validates/coerces the shape
    and never lets a malformed section escape to a renderer.

    Section content (`label`/`value`/`content`/table cells/`title`) is
    plugin-authored and UNTRUSTED: `normalize_panel_data` validates the
    *shape* only, never the text. Renderers must treat these strings as
    untrusted — the web/HTML renderer (Sprint 3) MUST escape them and never
    inject raw markup; the TUI renderer must not evaluate markup either.
    """

    sections: list[PanelStatSection | PanelTableSection | PanelTextSection]


class _PanelSpecRequired(TypedDict):
    name: str
    title: str
    fetch: Callable[[], PanelData]


class PanelSpec(_PanelSpecRequired, total=False):
    """A single panel contribution declared by `register()["panels"]`.

    `name` is a stable id, collision-checked like every other plugin
    contribution type (runner kind / notifier name / secrets backend name /
    health name). `fetch` is a no-arg callable returning `PanelData` — always
    invoke it via `run_panel_fetch`, never directly, so a raising/malformed
    panel can never crash a caller. `min_role` is optional (default "read"),
    used by the web surface in Sprint 3 to gate panel visibility.
    """

    min_role: str


class PanelNameCollisionError(RuntimeError):
    """Raised when two plugins declare a `panel` under the same name.

    Mirrors `HealthNameCollisionError` — a hard stop, not a silent last-wins
    overwrite, so a plugin can never shadow another plugin's panel unnoticed.
    """


class PanelInvalidMinRoleError(RuntimeError):
    """Raised when a plugin declares a panel with a `min_role` that is not a
    recognized role (`token_service.ROLE_RANKS` — the source of truth for
    every valid role name).

    This closes a fail-open privilege-escalation gap: `token_service.role_rank`
    returns `-1` for ANY unrecognized role, so an unrecognized `min_role`
    (typo `"Admin"`, `"superuser"`, empty string, or a non-string value like
    `123`/`None`/`[]`) would make the per-panel gate in
    `get_panel_endpoint` (`hivepilot/services/api_service.py`) compare
    `role_rank(caller.role) < role_rank(min_role)` as `0 < -1`, which is
    ALWAYS false — the 403 never fires and a meant-to-be-restricted panel is
    served to any `read` token. Rejecting an invalid `min_role` here, at
    registration time, means a panel can never even be registered with an
    unenforceable gate — fail-closed, and atomic with the other
    runner/notifier/secrets/health/panel collision errors above (the whole
    plugin's contribution rolls back, mirroring `PanelNameCollisionError`).
    """


class PanelDataError(ValueError):
    """Raised by `normalize_panel_data` when a panel's returned value does
    not match the closed `PanelData` shape (wrong top-level type, missing
    `sections`, unknown section `kind`, or a section missing/mistyping its
    required fields). Structural problems are rejected outright — only a
    stat section's `status` value is lenient (see `normalize_panel_data`),
    mirroring `_normalize_health_result`'s unknown-status fallback.
    """


def normalize_panel_data(data: Any) -> PanelData:
    """Coerce/validate a panel's returned value into the closed `PanelData` shape.

    Structurally malformed input (not a dict, missing/non-list `sections`, a
    non-dict section, an unknown section `kind`, or a section missing/mistyping
    one of its required fields) raises `PanelDataError` — callers (namely
    `run_panel_fetch`) must catch it and fall back to an error panel; this
    function itself never silently drops a whole panel's data. The one lenient
    exception is a stat section's `status`: an unrecognized value normalizes
    to `None` (no status badge) rather than rejecting the entire section,
    mirroring `_normalize_health_result`'s unknown-status fallback.
    """
    if not isinstance(data, dict):
        raise PanelDataError(f"panel data must be a dict, got {type(data).__name__}")
    sections = data.get("sections")
    if not isinstance(sections, list):
        raise PanelDataError("panel data must have a 'sections' list")

    normalized: list[PanelStatSection | PanelTableSection | PanelTextSection] = []
    for section in sections:
        if not isinstance(section, dict):
            raise PanelDataError(f"panel section must be a dict, got {type(section).__name__}")
        kind = section.get("kind")
        if kind not in PANEL_SECTION_KINDS:
            raise PanelDataError(f"invalid panel section kind: {kind!r}")

        if kind == "stat":
            label = section.get("label")
            value = section.get("value")
            if not isinstance(label, str) or not isinstance(value, str):
                raise PanelDataError("stat section requires string 'label' and 'value'")
            status = section.get("status")
            if status is not None and status not in PANEL_STAT_STATUSES:
                # Unknown-status fallback (mirrors _normalize_health_result):
                # coerce rather than reject the whole section.
                status = None
            normalized.append(
                PanelStatSection(kind="stat", label=label, value=value, status=status)
            )
        elif kind == "table":
            columns = section.get("columns")
            rows = section.get("rows")
            if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
                raise PanelDataError("table section requires a list of string 'columns'")
            if not isinstance(rows, list) or not all(
                isinstance(row, list) and all(isinstance(cell, str) for cell in row) for row in rows
            ):
                raise PanelDataError("table section requires 'rows' as a list of string lists")
            normalized.append(
                PanelTableSection(kind="table", columns=list(columns), rows=[list(r) for r in rows])
            )
        else:  # kind == "text"
            content = section.get("content")
            if not isinstance(content, str):
                raise PanelDataError("text section requires string 'content'")
            normalized.append(PanelTextSection(kind="text", content=content))

    return PanelData(sections=normalized)


# Skill plugin type (Sprint 1 — registry + contracts only; consuming skills
# in a runner's prompt/context is Sprint 2, validation is Sprint 3,
# orchestrator wiring is Sprint 4, CLI surface is Sprint 5). A plugin
# contributes skills via `register()["skills"] = [SkillSpec, ...]`. `SkillSpec`
# is a PLAIN DICT at runtime (TypedDict is a type-checking-only construct —
# no dataclass on the importlib-plugin path, matching every other
# contribution type here, e.g. `PanelSpec`).


class _SkillSpecRequired(TypedDict):
    name: str
    description: str
    provider: str
    files: dict[str, str]  # rel-path under .claude/skills/<name>/ -> content


class SkillSpec(_SkillSpecRequired, total=False):
    """A single skill contribution declared by `register()["skills"]`.

    `name` is a stable id, collision-checked like every other plugin
    contribution type (runner kind / notifier name / secrets backend name /
    health name / panel name). `files` maps a relative path under
    `.claude/skills/<name>/` to its content (e.g. `{"SKILL.md": "..."}`) —
    consuming/writing these files out is deferred to a later sprint. No
    `render()` callable is defined here; one may be added once a real skill
    needs it. `min_role` is optional and, unlike `PanelSpec`, has NO implicit
    default here — it is only validated (against `token_service.ROLE_RANKS`)
    and stored when a plugin actually declares it.
    """

    system_prompt: str  # optional text a runner may append
    applies_to: list[str]  # runner kinds this skill targets; absent = any
    min_role: str


class SkillNameCollisionError(RuntimeError):
    """Raised when two plugins declare a `skill` under the same name.

    Mirrors `PanelNameCollisionError` — a hard stop, not a silent last-wins
    overwrite, so a plugin can never shadow another plugin's skill unnoticed.
    """


class SkillInvalidMinRoleError(RuntimeError):
    """Raised when a plugin declares a skill with a `min_role` that is not a
    recognized role (`token_service.ROLE_RANKS` — the source of truth for
    every valid role name).

    Mirrors `PanelInvalidMinRoleError`'s fail-closed rationale: an
    unrecognized `min_role` must never be silently accepted (which would
    make any future rank comparison against it fail open) — rejecting it at
    registration time keeps a skill from ever being registered with an
    unenforceable gate, atomic with the other collision errors above (the
    whole plugin's contribution rolls back).
    """


# Graph-source plugin capability (Mirador Graph View PRD, Sprint 4). A
# plugin contributes Mirador graph sources via `register()["graph_sources"]
# = [GraphSourceSpec, ...]`, EXACTLY the way `"panels"` -> `list[PanelSpec]`
# works above. Unlike `PanelSpec`/`SkillSpec` (plain `TypedDict`s defined IN
# this module), `GraphSourceSpec` is a real frozen dataclass defined in
# `hivepilot/graph.py` (Sprint 1) — reused here, not redefined, to keep one
# source of truth for the graph-native contract shared with the built-in
# sources (`hivepilot/graph_sources/*.py`). A plugin constructs it via
# `from hivepilot.graph import GraphSourceSpec` in its own `register()`.
#
# Registration mechanics mirror runners/notifiers/secrets (NOT panels/
# skills' simpler wholesale-replace model): graph sources live in
# `hivepilot.graph`'s own MODULE-GLOBAL `_GRAPH_SOURCES` registry (shared
# with every built-in source, registered once at import time by
# `hivepilot/graph_sources/__init__.py`), so a `PluginManager` must track
# OWNERSHIP of the names it registers there — `self._owned_graph_source_names`,
# threaded through `_load_into`'s `owned_graph_source_names` parameter and
# `_stage_kind` exactly like `_owned_runner_kinds`/`_owned_notifier_names`/
# `_owned_secret_names` already are. `_commit` tears down this manager's
# OWN previously-owned names (via the proper `hivepilot.graph.
# unregister_graph_source`, never a built-in, never another manager's/
# plugin's name) BEFORE registering the freshly-staged set. This is what
# makes a disabled+reloaded plugin's graph source actually disappear
# (previously fail-open: nothing ever tore it down), and lets a still-
# enabled plugin's re-`exec()`d module register a brand-new
# `GraphSourceSpec` object under the same name on every `reload()` without
# self-colliding (previously a hard `GraphSourceNameCollisionError` that
# aborted the WHOLE plugin set's reload). A collision with a genuinely
# DIFFERENT plugin's name, or a built-in's, still raises
# `GraphSourceNameCollisionError` and rolls back atomically with this
# plugin's other contributions — see the `except` block below.
#
# `min_role` is NOT rejected at registration time here (unlike
# `PanelInvalidMinRoleError`/`SkillInvalidMinRoleError`): S1's own
# `_resolve_graph_min_role_rank` (`hivepilot/services/api_service.py`)
# already fails closed on an unrecognized `min_role` — treating it as
# unsatisfiable by ANY role, including admin — at the point a source is
# actually FETCHED, exactly like every built-in source's `min_role` is
# enforced. Re-validating at registration time would be redundant, not
# safer.


def _load_plugin_module(file: Path) -> Any | None:
    """Compile+exec a single local-file plugin from `file`, returning its
    module object, or `None` if it has no `register` attribute.

    Extracted from `_scan_local_plugins` (which now calls this once per
    directory) — behavior is unchanged: load by file path (so it works
    regardless of cwd / sys.path — the installed `hivepilot` binary and the
    Telegram bot don't have the project root on sys.path → `import plugins.x`
    would fail), and compile the CURRENT on-disk source directly rather than
    `spec.loader.exec_module(module)`, bypassing the `__pycache__/*.pyc` mtime
    cache so `PluginManager.reload()` (Phase 26b) always sees a file's real
    current content even across rapid same-second edits.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"hivepilot_plugin_{file.stem}", file)
    if not (spec and spec.loader):
        return None
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(file)
    source = file.read_text(encoding="utf-8")
    code = compile(source, str(file), "exec")
    exec(code, module.__dict__)  # noqa: S102 — this repo's own plugin file, by design
    return module if hasattr(module, "register") else None


def _scan_plugin_dir(
    plugin_dir: Path, *, seen_stems: set[str]
) -> list[tuple[Callable[..., Any], PluginRecord]]:
    """Scan a single directory for local-file plugins (`*.py`, non-`_`-prefixed,
    not in `settings.plugins_disabled`, and not already loaded from an earlier
    directory — see `seen_stems`). Mutates `seen_stems` in place with every
    stem this call attempts (whether it ultimately loads or is skipped as
    broken), so a later directory never re-attempts the same name.
    """
    found: list[tuple[Callable[..., Any], PluginRecord]] = []
    if not plugin_dir.exists():
        return found

    for file in sorted(plugin_dir.glob("*.py")):
        if file.stem.startswith("_"):
            continue
        if file.stem in seen_stems:
            # Dedup by module stem, first-wins: an earlier directory in scan
            # order (base_dir/plugins, then plugins_extra_dirs in order)
            # already claimed this name — e.g. a config repo's plugins/
            # overriding a shipped plugin of the same stem. Skip silently
            # (info-level log only), never a collision error: unlike a
            # runner-kind/notifier-name collision (a hard stop across two
            # DIFFERENT plugins), this is the SAME logical plugin name
            # shadowed by directory precedence, working as designed.
            logger.info(
                "plugins.skipped_duplicate_stem",
                name=file.stem,
                source="local-file",
                directory=str(plugin_dir),
            )
            continue
        seen_stems.add(file.stem)
        if file.stem in settings.plugins_disabled:
            # Skip BEFORE the module is even exec'd (and therefore before
            # register() could ever be invoked) — a disabled plugin
            # contributes no runners/notifiers/hooks and has no side
            # effects from its own module body either.
            logger.info("plugins.skipped_disabled", name=file.stem, source="local-file")
            continue
        try:
            module = _load_plugin_module(file)
            if module is not None:
                found.append(
                    (
                        module.register,
                        PluginRecord(name=file.stem, source="local-file", location=str(file)),
                    )
                )
        except Exception as exc:  # noqa: BLE001 — a broken plugin must not kill a run
            logger.warning("plugins.load_failed", file=str(file), error=str(exc))
    return found


def _scan_local_plugins() -> list[tuple[Callable[..., Any], PluginRecord]]:
    """Scan `base_dir/plugins`, then each directory in `settings.plugins_extra_dirs`
    (in order), for local-file plugins.

    `base_dir/plugins` is always scanned FIRST — a deployment that points
    `base_dir` at its own config repo (to load its own `plugins/*.py`) can
    still reach the engine's shipped `plugins/*.py` via `plugins_extra_dirs`,
    without one shadowing the other. A module stem already loaded from an
    earlier directory is skipped rather than re-loaded or raising a collision
    (dedup by stem, first-wins — `base_dir/plugins` always wins over any
    extra dir). A `plugins_extra_dirs` entry that doesn't exist on disk is
    silently skipped, same as a missing `base_dir/plugins` always has been.

    Returns each successfully-loaded plugin's `register` callable paired with
    a `PluginRecord` describing where it came from.
    """
    found: list[tuple[Callable[..., Any], PluginRecord]] = []
    if not settings.plugins_enabled:
        return found

    seen_stems: set[str] = set()
    found.extend(_scan_plugin_dir(settings.base_dir / "plugins", seen_stems=seen_stems))
    for extra_dir in settings.plugins_extra_dirs:
        found.extend(_scan_plugin_dir(extra_dir, seen_stems=seen_stems))
    return found


def load_plugins(entry: str | None = None) -> list[Callable[..., Any]]:
    """Load plugin callables from a module path or from `plugins/` directory."""
    plugins: list[Callable[..., Any]] = []
    if entry:
        module_name, attr = entry.split(":") if ":" in entry else (entry, "register")
        module = import_module(module_name)
        plugin_callable = getattr(module, attr)
        plugins.append(plugin_callable)
    else:
        plugins = [fn for fn, _ in _scan_local_plugins()]
    logger.info("plugins.loaded", count=len(plugins))
    return plugins


def load_entry_point_plugins() -> list[tuple[Callable[..., Any], PluginRecord]]:
    """Discover plugins registered under the `hivepilot.plugins` entry-point group.

    Third-party packages declare, in their OWN pyproject.toml:
        [project.entry-points."hivepilot.plugins"]
        my_plugin = "my_package:register"
    """
    found: list[tuple[Callable[..., Any], PluginRecord]] = []
    if not settings.plugins_enabled:
        return found

    try:
        eps = metadata.entry_points(group=PLUGIN_ENTRY_POINT_GROUP)
    except Exception as exc:  # noqa: BLE001 — a broken environment must not kill startup
        logger.warning("plugins.entry_points_scan_failed", error=str(exc))
        return found

    for ep in eps:
        if ep.name in settings.plugins_disabled:
            # Skip BEFORE ep.load() (and therefore before register() could
            # ever be invoked) — mirrors the local-file skip point above.
            logger.info("plugins.skipped_disabled", name=ep.name, source="entry-point")
            continue
        try:
            fn = ep.load()
        except Exception as exc:  # noqa: BLE001 — one broken plugin must not skip the rest
            logger.warning("plugins.entry_point_load_failed", entry_point=ep.name, error=str(exc))
            continue
        dist = getattr(ep, "dist", None)
        location = f"{ep.value} ({dist.name}=={dist.version})" if dist else ep.value
        found.append((fn, PluginRecord(name=ep.name, source="entry-point", location=location)))
    return found


@dataclass
class ReloadResult:
    """Result of a `PluginManager.reload()` call (Phase 26b hot-reload).

    `ok=False` means the LIVE plugin state -- every `RUNNER_MAP`/
    `NOTIFIER_MAP`/`SECRETS_MAP` entry this manager owns, plus its own
    instance dicts (`hooks`/`declared_notifiers`/`health`/`panels`/`skills`)
    -- was left COMPLETELY untouched: a broken/colliding candidate set never
    replaces a working one (staging-then-commit, see `PluginManager.reload`).
    `error` is a short, kind/tool-level message (exception type + str()) --
    never a raw traceback, and never plugin secret content, since every
    exception this can wrap is either one of this module's own collision
    errors (kind/name/class-name text only) or an import-time error whose
    message is a module path, not plugin runtime data.

    `added`/`removed`/`updated` are PLUGIN NAMES (`PluginRecord.name`), not
    individual contribution names -- `added`/`removed` are membership deltas
    against the plugin set from BEFORE this reload; `updated` is every
    plugin name present in BOTH the before and after sets. A local-file
    plugin is always re-exec'd on scan (see `_scan_local_plugins`), so it is
    always "re-registered" on every reload whether or not its source
    actually changed -- `plugins_changed_on_disk()` is what should gate
    WHETHER to call `reload()` at all, not this field.
    """

    ok: bool
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    error: str | None = None


def _snapshot_plugin_dir() -> dict[str, float]:
    """mtime snapshot of every non-underscore-prefixed `plugins/*.py` file,
    keyed by absolute path string.

    Pure `Path.glob`/`os.stat` -- no watchdog/inotify dependency, matching
    the "no new dependency" constraint. Independent of
    `settings.plugins_enabled`/`plugins_disabled`: this is a purely
    filesystem-level signal ("did the directory change"), not a load-time
    decision -- `plugins_changed_on_disk()` must answer accurately regardless
    of whether a caller is currently loading plugins at all.
    """
    plugin_dir = settings.base_dir / "plugins"
    if not plugin_dir.exists():
        return {}
    return {
        str(f): f.stat().st_mtime
        for f in sorted(plugin_dir.glob("*.py"))
        if not f.stem.startswith("_")
    }


@dataclass
class _StagedPluginState:
    """Staging area for one full plugin scan+register pass (`_load_into`).

    Populated WITHOUT mutating the process-global `RUNNER_MAP`/
    `NOTIFIER_MAP`/`SECRETS_MAP` (`runner_map`/`notifier_map`/`secrets_map`
    below hold only the entries THIS pass intends to commit) nor any live
    `PluginManager` instance's own dicts -- `PluginManager._commit` is the
    ONLY place that ever touches real state, and only once a whole pass has
    succeeded without raising (staging-then-commit).
    """

    loaded: list[PluginRecord] = field(default_factory=list)
    hooks: dict[str, list[Any]] = field(
        default_factory=lambda: {"before_step": [], "after_step": []}
    )
    declared_notifiers: dict[str, Callable[[str], None]] = field(default_factory=dict)
    health: dict[str, Callable[..., Any]] = field(default_factory=dict)
    panels: dict[str, PanelSpec] = field(default_factory=dict)
    skills: dict[str, SkillSpec] = field(default_factory=dict)
    plugins: list[Callable[..., Any]] = field(default_factory=list)
    runner_map: dict[str, Any] = field(default_factory=dict)
    notifier_map: dict[str, Callable[[str], None]] = field(default_factory=dict)
    secrets_map: dict[str, Any] = field(default_factory=dict)
    # `Any`, not `GraphSourceSpec` — mirrors `runner_map: dict[str, Any]` /
    # `secrets_map: dict[str, Any]` above: `GraphSourceSpec` is only ever
    # imported under `if TYPE_CHECKING:` at this module's top (real usage is
    # always a lazy, function-local import to avoid a circular import with
    # `hivepilot.graph` — see that block's comment), and a `@dataclass`
    # field annotation is evaluated by tooling in a way `TYPE_CHECKING`-only
    # names don't reliably resolve against.
    graph_source_map: dict[str, Any] = field(default_factory=dict)


def _stage_kind(
    kind: str,
    obj: Any,
    *,
    live_map: dict[str, Any],
    owned_kinds: frozenset[str],
    staged: dict[str, Any],
    collision_cls: type[Exception],
    label: str,
) -> None:
    """Register `obj` under `kind` into the STAGING dict `staged`, applying
    the same collision rule `RunnerRegistry.register`/`NotifierRegistry.
    register`/`SecretsRegistry.register` do against the LIVE global map --
    but without ever mutating `live_map` itself.

    A kind already present in `staged` (an earlier plugin in THIS pass
    claimed it) is a collision unless it's the exact same object. A kind
    already present in `live_map` is a collision unless it's the exact same
    object, OR `kind` is in `owned_kinds` -- a kind THIS manager itself
    registered on a PREVIOUS load/reload and is now legitimately replacing
    (the live entry is stale-but-still-there only because staging never
    mutates it; `_commit` is what actually swaps it).
    """
    current = staged.get(kind)
    if current is None and kind in live_map and kind not in owned_kinds:
        current = live_map[kind]
    if current is not None and current is not obj:
        raise collision_cls(
            f"{label} {kind!r} is already registered to "
            f"{getattr(current, '__name__', type(current).__name__)}; refusing to "
            f"silently replace it with {getattr(obj, '__name__', type(obj).__name__)}"
        )
    staged[kind] = obj


class PluginManager:
    def __init__(self) -> None:
        # Reentrancy guard for `reload()` -- see that method's docstring.
        # Never touched during `__init__` itself (which never calls
        # `reload()`), but initialized here so the attribute always exists.
        self._reload_in_progress = False
        staged = self._load_into(
            owned_runner_kinds=frozenset(),
            owned_notifier_names=frozenset(),
            owned_secret_names=frozenset(),
            owned_graph_source_names=frozenset(),
        )
        self._commit(staged)

    # ------------------------------------------------------------------
    # Hot-reload (Phase 26b)
    # ------------------------------------------------------------------

    def reload(self) -> ReloadResult:
        """Atomically re-scan+re-register this manager's plugin set.

        Staging-then-commit: `_load_into` builds an entirely new candidate
        state WITHOUT touching any live global (`RUNNER_MAP`/`NOTIFIER_MAP`/
        `SECRETS_MAP`) or this instance's own dicts. If it raises -- a kind/
        name collision, or the `settings.plugins_entry` explicit-entry pin
        raising on import (the one load path that is NOT fail-isolated, see
        `load_plugins`) -- the LIVE state is left COMPLETELY untouched and
        `ReloadResult(ok=False, error=...)` is returned. If it succeeds,
        `_commit` applies it atomically: only the `RUNNER_MAP`/
        `NOTIFIER_MAP`/`SECRETS_MAP` kinds THIS manager itself previously
        owned are removed (never a builtin, never a kind owned by some other
        manager/caller -- see `_stage_kind`'s `owned_kinds` handling), before
        the staged kinds are added; every instance dict is replaced wholesale.

        Concurrency: `_commit` is a short, ordered sequence of dict
        mutations, each individually atomic under the GIL. `reload()` is
        meant to be called from a single-threaded context only -- the
        scheduler daemon's own tick or its SIGHUP handler -- and is NEVER
        called concurrently with pipeline dispatch in this codebase. A run
        already holding a resolved runner *instance* (constructed before
        this reload) is unaffected by a later reload swapping `RUNNER_MAP`'s
        *class* entry -- it keeps executing with the instance it already has;
        only a NEW `resolve_runner_class()` lookup after commit sees the
        change.

        Reentrancy guard: Python signal handlers run on the main thread
        BETWEEN bytecode instructions, so a SIGHUP delivered WHILE a reload
        is already mid-flight (inside `_load_into`/`_commit`) could re-enter
        this method on the SAME instance. Without a guard, the OUTER call's
        now-stale local `staged`/`before_names` would resume executing AFTER
        the inner, reentrant call already committed -- its own `_commit()`
        would then stomp `self._owned_runner_kinds`/etc. with the outer
        (older) values, corrupting ownership bookkeeping for the NEXT
        reload. `self._reload_in_progress` makes a reentrant call a fast,
        logged no-op instead: the reload already in flight supersedes it.
        """
        if self._reload_in_progress:
            logger.warning("plugins.reload_skipped_reentrant")
            return ReloadResult(
                ok=False, error="reload already in progress; reentrant call skipped"
            )
        self._reload_in_progress = True
        try:
            before_names = {record.name for record in self.loaded}
            try:
                staged = self._load_into(
                    owned_runner_kinds=self._owned_runner_kinds,
                    owned_notifier_names=self._owned_notifier_names,
                    owned_secret_names=self._owned_secret_names,
                    owned_graph_source_names=self._owned_graph_source_names,
                )
            except Exception as exc:  # noqa: BLE001 — a bad candidate must never touch live state
                logger.warning("plugins.reload_failed", error=str(exc))
                return ReloadResult(ok=False, error=f"{type(exc).__name__}: {exc}")

            self._commit(staged)

            after_names = {record.name for record in self.loaded}
            result = ReloadResult(
                ok=True,
                added=sorted(after_names - before_names),
                removed=sorted(before_names - after_names),
                updated=sorted(after_names & before_names),
            )
            logger.info(
                "plugins.reloaded",
                added=result.added,
                removed=result.removed,
                updated=result.updated,
            )
            return result
        finally:
            self._reload_in_progress = False

    def plugins_changed_on_disk(self) -> bool:
        """True if `plugins/*.py` (added/removed/modified) differs from the
        snapshot taken at the last successful load/reload. Pure mtime
        comparison -- safe to poll on every scheduler tick."""
        return _snapshot_plugin_dir() != self._plugin_dir_snapshot

    def _commit(self, staged: _StagedPluginState) -> None:
        """Apply a successfully-staged pass to live state. Only ever called
        with a `staged` that `_load_into` produced WITHOUT raising -- see
        `__init__`/`reload()`. Never raises itself: every collision was
        already resolved during staging.
        """
        from hivepilot import graph as graph_module
        from hivepilot.registry import RUNNER_MAP, SECRETS_MAP
        from hivepilot.services.notification_service import NOTIFIER_MAP

        for kind in getattr(self, "_owned_runner_kinds", frozenset()):
            RUNNER_MAP.pop(kind, None)
        RUNNER_MAP.update(staged.runner_map)
        self._owned_runner_kinds: frozenset[str] = frozenset(staged.runner_map)

        for name in getattr(self, "_owned_notifier_names", frozenset()):
            NOTIFIER_MAP.pop(name, None)
        NOTIFIER_MAP.update(staged.notifier_map)
        self._owned_notifier_names: frozenset[str] = frozenset(staged.notifier_map)

        for name in getattr(self, "_owned_secret_names", frozenset()):
            SECRETS_MAP.pop(name, None)
        SECRETS_MAP.update(staged.secrets_map)
        self._owned_secret_names: frozenset[str] = frozenset(staged.secrets_map)

        # Graph sources (Sprint 4 hot-reload fix): same ownership + teardown
        # discipline as RUNNER_MAP/NOTIFIER_MAP/SECRETS_MAP above -- tear down
        # ONLY the names THIS manager itself previously registered (never a
        # built-in, never another manager's/plugin's name -- built-ins are
        # registered once at import time by `hivepilot/graph_sources/
        # __init__.py` and are never manager-owned) via the proper
        # `unregister_graph_source`, THEN register the freshly-staged set.
        # This is what makes a disabled+reloaded plugin's source actually
        # disappear (was previously fail-open: `_commit` never tore it down),
        # and lets a still-enabled plugin's re-exec'd module register a
        # brand-new `GraphSourceSpec` object under the same name on every
        # reload without self-colliding (was previously a hard `reload()`
        # failure for the WHOLE plugin set).
        for graph_name in getattr(self, "_owned_graph_source_names", frozenset()):
            graph_module.unregister_graph_source(graph_name)
        for graph_name, graph_spec in staged.graph_source_map.items():
            graph_module.register_graph_source(graph_spec)
        self._owned_graph_source_names: frozenset[str] = frozenset(staged.graph_source_map)

        self.loaded: list[PluginRecord] = staged.loaded
        self.hooks: dict[str, list[Any]] = staged.hooks
        self.declared_notifiers: dict[str, Callable[[str], None]] = staged.declared_notifiers
        self.health: dict[str, Callable[..., Any]] = staged.health
        self.panels: dict[str, PanelSpec] = staged.panels
        self.skills: dict[str, SkillSpec] = staged.skills
        self.plugins = staged.plugins
        self._plugin_dir_snapshot: dict[str, float] = _snapshot_plugin_dir()

    def _load_into(
        self,
        *,
        owned_runner_kinds: frozenset[str],
        owned_notifier_names: frozenset[str],
        owned_secret_names: frozenset[str],
        owned_graph_source_names: frozenset[str],
    ) -> _StagedPluginState:
        """Scan+register one full plugin set into a fresh `_StagedPluginState`
        -- the SINGLE load code path shared by `__init__` (called with empty
        `owned_*` sets, i.e. every live kind is a real collision, exactly the
        pre-hot-reload `__init__` behavior) and `reload()` (called with this
        manager's CURRENT ownership, so re-claiming its own previously-owned
        kinds is never a collision). Never mutates a live global or `self`;
        raises on any kind/name collision exactly like the pre-hot-reload
        `__init__` did (see `_stage_kind`), so `reload()` can catch it and
        discard the whole candidate state untouched.
        """
        local = _scan_local_plugins()
        explicit_entry = settings.__dict__.get("plugins_entry")
        # The master switch disables ALL plugin loading, including the explicit
        # `plugins_entry` pin — otherwise an operator could not silence a suspect
        # plugin wired via that path (see config.py `plugins_enabled`).
        if explicit_entry and settings.plugins_enabled:
            # A THIRD load path (alongside `_scan_local_plugins` and
            # `load_entry_point_plugins` above) — must honor `plugins_disabled`
            # too. This plugin's `PluginRecord.name` (what the TUI shows and
            # would toggle) is the full `explicit_entry` string (see
            # PluginRecord() below); an operator setting `plugins_disabled`
            # directly via config/env would more naturally use just the
            # module-name portion (before the `:register`-style attribute
            # separator), matching the short names the other two paths use —
            # accept either form.
            explicit_module_name = explicit_entry.split(":", 1)[0]
            if (
                explicit_entry in settings.plugins_disabled
                or explicit_module_name in settings.plugins_disabled
            ):
                logger.info(
                    "plugins.skipped_disabled", name=explicit_entry, source="explicit-entry"
                )
            else:
                for fn in load_plugins(entry=explicit_entry):
                    local.append(
                        (
                            fn,
                            PluginRecord(
                                name=explicit_entry,
                                source="explicit-entry",
                                location=explicit_entry,
                            ),
                        )
                    )
        entry_point = load_entry_point_plugins()

        staged = _StagedPluginState()
        # Back-compat: flat list of discovered callables (mirrors the pre-Sprint-2
        # `load_plugins()`-derived attribute), regardless of whether calling
        # `register()` on them below succeeds.
        staged.plugins = [fn for fn, _ in local + entry_point]

        for register_fn, record in local + entry_point:
            try:
                hooks = register_fn()
            except Exception as exc:  # noqa: BLE001 — a broken plugin must not kill a run
                logger.warning(
                    "plugins.register_failed",
                    plugin=record.name,
                    source=record.source,
                    error=str(exc),
                )
                continue

            runners = hooks.pop("runners", None)
            notifiers = hooks.pop("notifiers", None)
            secrets = hooks.pop("secrets", None)
            health = hooks.pop("health", None)
            panels = hooks.pop("panels", None)
            skills = hooks.pop("skills", None)
            graph_sources = hooks.pop("graph_sources", None)
            # Declared unconditionally (not just inside the `if` below) so
            # `record.contributions` can be populated from these lists further
            # down regardless of which contribution types this plugin
            # declared — a plugin contributing ONLY lifecycle hooks (no
            # runners/notifiers/secrets/health/panels/skills/graph_sources)
            # never enters the `if` block below at all, and these must still
            # exist (as empty lists) for that case.
            applied_runners: list[str] = []
            applied_notifiers: list[str] = []
            applied_secrets: list[str] = []
            applied_health: list[str] = []
            applied_panels: list[str] = []
            applied_skills: list[str] = []
            applied_graph_sources: list[str] = []
            if runners or notifiers or secrets or health or panels or skills or graph_sources:
                from hivepilot import graph as graph_module
                from hivepilot.registry import (
                    RUNNER_MAP,
                    SECRETS_MAP,
                    RunnerKindCollisionError,
                    SecretsBackendCollisionError,
                )
                from hivepilot.services.notification_service import (
                    NOTIFIER_MAP,
                    NotifierKindCollisionError,
                )

                # A kind/name collision is a hard stop and propagates uncaught
                # (unlike an isolated broken plugin, which is logged and skipped).
                # Stage this one plugin's runners+notifiers+secrets+health+panels+
                # skills+graph_sources atomically: if any entry collides, roll back
                # the entries THIS plugin already staged (into `staged.runner_map`/
                # `notifier_map`/
                # `secrets_map`/`health`/`panels`/`skills`) before re-raising, so
                # an aborted plugin never leaves orphaned, untracked staged
                # entries behind for the rest of this pass.
                try:
                    for kind, cls in (runners or {}).items():
                        was_present = kind in staged.runner_map
                        _stage_kind(
                            kind,
                            cls,
                            live_map=RUNNER_MAP,
                            owned_kinds=owned_runner_kinds,
                            staged=staged.runner_map,
                            collision_cls=RunnerKindCollisionError,
                            label="Runner kind",
                        )
                        if not was_present:
                            applied_runners.append(kind)
                    for notifier_name, notifier_fn in (notifiers or {}).items():
                        was_present = notifier_name in staged.notifier_map
                        _stage_kind(
                            notifier_name,
                            notifier_fn,
                            live_map=NOTIFIER_MAP,
                            owned_kinds=owned_notifier_names,
                            staged=staged.notifier_map,
                            collision_cls=NotifierKindCollisionError,
                            label="Notifier",
                        )
                        if not was_present:
                            applied_notifiers.append(notifier_name)
                    for secret_name, secret_backend in (secrets or {}).items():
                        was_present = secret_name in staged.secrets_map
                        _stage_kind(
                            secret_name,
                            secret_backend,
                            live_map=SECRETS_MAP,
                            owned_kinds=owned_secret_names,
                            staged=staged.secrets_map,
                            collision_cls=SecretsBackendCollisionError,
                            label="Secrets backend",
                        )
                        if not was_present:
                            applied_secrets.append(secret_name)
                    for health_name, health_fn in (health or {}).items():
                        if health_name in staged.health:
                            raise HealthNameCollisionError(
                                f"health check '{health_name}' is already registered; "
                                "refusing to silently replace it"
                            )
                        staged.health[health_name] = health_fn
                        applied_health.append(health_name)
                    for panel_spec in panels or []:
                        panel_name = panel_spec["name"]
                        if panel_name in staged.panels:
                            raise PanelNameCollisionError(
                                f"panel '{panel_name}' is already registered; "
                                "refusing to silently replace it"
                            )
                        min_role = panel_spec.get("min_role", "read")
                        # isinstance() FIRST: a non-string/non-hashable min_role
                        # (e.g. `[]`/`{}`) must be rejected here, before any
                        # `in token_service.ROLE_RANKS` membership check, so it
                        # can never raise a raw TypeError. A min_role that isn't
                        # one of token_service's recognized roles is rejected
                        # too — token_service.role_rank() returns -1 for any
                        # unrecognized role, which would make the endpoint's
                        # `role_rank(caller.role) < role_rank(min_role)` gate
                        # fail open (see PanelInvalidMinRoleError docstring).
                        if (
                            not isinstance(min_role, str)
                            or min_role not in token_service.ROLE_RANKS
                        ):
                            raise PanelInvalidMinRoleError(
                                f"panel '{panel_name}' declares invalid min_role "
                                f"{min_role!r}; must be a string, one of "
                                f"{sorted(token_service.ROLE_RANKS)}"
                            )
                        staged.panels[panel_name] = {
                            "name": panel_name,
                            "title": panel_spec["title"],
                            "fetch": panel_spec["fetch"],
                            "min_role": min_role,
                        }
                        applied_panels.append(panel_name)
                    for skill_spec in skills or []:
                        skill_name = skill_spec["name"]
                        if skill_name in staged.skills:
                            raise SkillNameCollisionError(
                                f"skill '{skill_name}' is already registered; "
                                "refusing to silently replace it"
                            )
                        skill_entry: SkillSpec = {
                            "name": skill_name,
                            "description": skill_spec["description"],
                            "provider": skill_spec["provider"],
                            "files": skill_spec["files"],
                        }
                        if "system_prompt" in skill_spec:
                            skill_entry["system_prompt"] = skill_spec["system_prompt"]
                        if "applies_to" in skill_spec:
                            skill_entry["applies_to"] = skill_spec["applies_to"]
                        # min_role is OPTIONAL for skills (unlike panels, which
                        # default to "read") — only validate/store it when the
                        # plugin actually declared one.
                        if "min_role" in skill_spec:
                            min_role = skill_spec["min_role"]
                            # isinstance() FIRST: a non-string/non-hashable
                            # min_role (e.g. `[]`/`{}`) must be rejected here,
                            # before any `in token_service.ROLE_RANKS`
                            # membership check, so it can never raise a raw
                            # TypeError. Fail-closed: never a `-1` sentinel
                            # that would invert a future rank comparison to
                            # always pass (see `SkillInvalidMinRoleError`).
                            if (
                                not isinstance(min_role, str)
                                or min_role not in token_service.ROLE_RANKS
                            ):
                                raise SkillInvalidMinRoleError(
                                    f"skill '{skill_name}' declares invalid min_role "
                                    f"{min_role!r}; must be a string, one of "
                                    f"{sorted(token_service.ROLE_RANKS)}"
                                )
                            skill_entry["min_role"] = min_role
                        staged.skills[skill_name] = skill_entry
                        applied_skills.append(skill_name)
                    for graph_spec in graph_sources or []:
                        # Staged into a LOCAL dict first (`staged.graph_source_map`),
                        # exactly like runners/notifiers/secrets above — NOT
                        # registered directly into `hivepilot.graph`'s live
                        # `_GRAPH_SOURCES` here. `_stage_kind` applies the same
                        # collision rule against that staged dict AND the live
                        # registry (bypassed for names in `owned_graph_source_names`
                        # -- this manager's own previously-registered names, which
                        # it may legitimately re-stage on `reload()` without a
                        # self-collision; see `_commit`'s teardown-then-register).
                        # A DIFFERENT plugin's/built-in's name still collides,
                        # raising `GraphSourceNameCollisionError`.
                        was_present = graph_spec.name in staged.graph_source_map
                        _stage_kind(
                            graph_spec.name,
                            graph_spec,
                            live_map=graph_module._GRAPH_SOURCES,
                            owned_kinds=owned_graph_source_names,
                            staged=staged.graph_source_map,
                            collision_cls=graph_module.GraphSourceNameCollisionError,
                            label="Graph source",
                        )
                        if not was_present:
                            applied_graph_sources.append(graph_spec.name)
                except (
                    RunnerKindCollisionError,
                    NotifierKindCollisionError,
                    SecretsBackendCollisionError,
                    HealthNameCollisionError,
                    PanelNameCollisionError,
                    PanelInvalidMinRoleError,
                    SkillNameCollisionError,
                    SkillInvalidMinRoleError,
                    graph_module.GraphSourceNameCollisionError,
                ):
                    for kind in applied_runners:
                        staged.runner_map.pop(kind, None)
                    for notifier_name in applied_notifiers:
                        staged.notifier_map.pop(notifier_name, None)
                    for secret_name in applied_secrets:
                        staged.secrets_map.pop(secret_name, None)
                    for health_name in applied_health:
                        staged.health.pop(health_name, None)
                    for panel_name in applied_panels:
                        staged.panels.pop(panel_name, None)
                    for skill_name in applied_skills:
                        staged.skills.pop(skill_name, None)
                    for graph_name in applied_graph_sources:
                        staged.graph_source_map.pop(graph_name, None)
                    raise

                if notifiers:
                    staged.declared_notifiers.update(notifiers)

            # Whatever keys remain in `hooks` after the seven contribution
            # types above were popped out are lifecycle-hook names
            # (`before_step`/`after_step`/`on_pipeline_end`/`on_error`/etc.) —
            # not collision-checked (multiple plugins may each contribute a
            # callable under the same hook name; every one runs), so no
            # rollback bookkeeping is needed for these, unlike the seven above.
            applied_hooks = sorted(hooks)
            for hook_name, hook_callable in hooks.items():
                staged.hooks.setdefault(hook_name, []).append(hook_callable)

            # Per-plugin attribution (Phase 26a): record, on THIS plugin's
            # own PluginRecord, exactly which names it contributed per
            # contribution type. Only entries that survived the atomic
            # collision-rollback above (i.e. are still in `applied_*`) are
            # credited — a plugin whose registration was rolled back never
            # reaches this line at all (the `except` block above re-raises,
            # aborting the whole `_load_into` pass), so there is nothing
            # further to guard here.
            contributions: dict[str, list[str]] = {}
            if applied_runners:
                contributions["runners"] = sorted(applied_runners)
            if applied_notifiers:
                contributions["notifiers"] = sorted(applied_notifiers)
            if applied_secrets:
                contributions["secrets"] = sorted(applied_secrets)
            if applied_health:
                contributions["health"] = sorted(applied_health)
            if applied_panels:
                contributions["panels"] = sorted(applied_panels)
            if applied_skills:
                contributions["skills"] = sorted(applied_skills)
            if applied_graph_sources:
                contributions["graph_sources"] = sorted(applied_graph_sources)
            if applied_hooks:
                contributions["hooks"] = applied_hooks
            record.contributions = contributions

            staged.loaded.append(record)

        return staged

    def run_hook(self, hook_name: str, **kwargs: Any) -> None:
        for hook in self.hooks.get(hook_name, []):
            hook(**kwargs)

    def run_health_check(self, name: str) -> HealthStatus:
        """Run a single named health check. Never raises: an exception
        raised by the callable itself is caught here and reported as
        `HealthStatus("error", "<ExceptionType>")` — the exception type
        name only, never the exception message. The full exception
        (including its message) is logged server-side; it must never be
        echoed back to callers, since this result is exposed to any
        read-role token via `GET /v1/plugins/health`. Same never-crash
        guarantee every other plugin hook in this repo has.
        """
        fn = self.health.get(name)
        if fn is None:
            return HealthStatus("error", f"no health check registered for {name!r}")
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 — a health check must never crash
            logger.warning("plugins.health_check_failed", name=name, error=str(exc))
            return HealthStatus("error", type(exc).__name__)
        return _normalize_health_result(result)

    def check_all(self) -> dict[str, HealthStatus]:
        """Run every registered health check. Never raises (see
        `run_health_check`) — safe to call unconditionally from `plugins
        list` / `plugins health` / the TUI."""
        return {name: self.run_health_check(name) for name in self.health}

    def list_panels(self) -> list[PanelSpec]:
        """Every registered panel, sorted by name — safe to call unconditionally
        from the TUI (Sprint 2) / web surface (Sprint 3)."""
        return [self.panels[name] for name in sorted(self.panels)]

    def get_panel(self, name: str) -> PanelSpec | None:
        """Look up a single registered panel by name, or `None` if unknown."""
        return self.panels.get(name)

    def list_skills(self) -> list[SkillSpec]:
        """Every registered skill, sorted by name — safe to call unconditionally
        by any future consumer (runner prompt assembly, CLI, orchestrator)."""
        return [self.skills[name] for name in sorted(self.skills)]

    def get_skill(self, name: str) -> SkillSpec | None:
        """Look up a single registered skill by name, or `None` if unknown."""
        return self.skills.get(name)

    def run_panel_fetch(self, name: str) -> PanelData:
        """Run a single named panel's `fetch()`. Never raises: an exception
        raised by `fetch()` itself, or a malformed return value (rejected by
        `normalize_panel_data`), is caught here and reported as a single
        `stat` section — `{"label": "error", "value": "<ExceptionType>",
        "status": "error"}` — the exception TYPE name only, never the
        exception message, since panel data may be exposed to any read-role
        token via the web surface (Sprint 3). The full exception (including
        its message) is logged server-side. Same never-crash guarantee
        `run_health_check` has.
        """
        spec = self.panels.get(name)
        if spec is None:
            return PanelData(
                sections=[
                    PanelStatSection(
                        kind="stat",
                        label="error",
                        value="PanelNotFound",
                        status="error",
                    )
                ]
            )
        try:
            result = spec["fetch"]()
            return normalize_panel_data(result)
        except Exception as exc:  # noqa: BLE001 — a panel fetch must never crash
            logger.warning("plugins.panel_fetch_failed", name=name, error=str(exc))
            return PanelData(
                sections=[
                    PanelStatSection(
                        kind="stat",
                        label="error",
                        value=type(exc).__name__,
                        status="error",
                    )
                ]
            )
