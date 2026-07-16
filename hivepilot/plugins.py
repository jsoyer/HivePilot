from __future__ import annotations

import importlib.metadata as metadata
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Callable, NamedTuple, TypedDict

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

PLUGIN_ENTRY_POINT_GROUP = "hivepilot.plugins"


@dataclass(slots=True)
class PluginRecord:
    name: str
    source: str
    location: str


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


def _scan_local_plugins() -> list[tuple[Callable[..., Any], PluginRecord]]:
    """Scan `plugins/` directory for local-file plugins.

    Returns each successfully-loaded plugin's `register` callable paired with
    a `PluginRecord` describing where it came from.
    """
    found: list[tuple[Callable[..., Any], PluginRecord]] = []
    if not settings.plugins_enabled:
        return found
    plugin_dir = settings.base_dir / "plugins"
    if plugin_dir.exists():
        import importlib.util

        for file in sorted(plugin_dir.glob("*.py")):
            if file.stem.startswith("_"):
                continue
            if file.stem in settings.plugins_disabled:
                # Skip BEFORE the module is even exec'd (and therefore before
                # register() could ever be invoked) — a disabled plugin
                # contributes no runners/notifiers/hooks and has no side
                # effects from its own module body either.
                logger.info("plugins.skipped_disabled", name=file.stem, source="local-file")
                continue
            # Load by file path so it works regardless of cwd / sys.path
            # (the installed `hivepilot` binary and the Telegram bot don't have
            # the project root on sys.path → `import plugins.x` would fail).
            try:
                spec = importlib.util.spec_from_file_location(f"hivepilot_plugin_{file.stem}", file)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    if hasattr(module, "register"):
                        found.append(
                            (
                                module.register,
                                PluginRecord(
                                    name=file.stem, source="local-file", location=str(file)
                                ),
                            )
                        )
            except Exception as exc:  # noqa: BLE001 — a broken plugin must not kill a run
                logger.warning("plugins.load_failed", file=str(file), error=str(exc))
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


class PluginManager:
    def __init__(self) -> None:
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
                logger.info("plugins.skipped_disabled", name=explicit_entry, source="local-file")
            else:
                for fn in load_plugins(entry=explicit_entry):
                    local.append(
                        (
                            fn,
                            PluginRecord(
                                name=explicit_entry, source="local-file", location=explicit_entry
                            ),
                        )
                    )
        entry_point = load_entry_point_plugins()

        self.loaded: list[PluginRecord] = []
        self.hooks: dict[str, list[Any]] = {"before_step": [], "after_step": []}
        self.declared_notifiers: dict[str, Callable[[str], None]] = {}
        # name -> health-check callable, collected the same way as
        # runners/notifiers/secrets (popped out of a plugin's declared
        # hooks). Per-manager instance dict — no process-global map needed,
        # health is scoped to this PluginManager the same way `self.hooks` is.
        self.health: dict[str, Callable[..., Any]] = {}
        # name -> PanelSpec, collected the same way as health (popped out of
        # a plugin's declared hooks, collision-checked, scoped to this
        # PluginManager instance — no process-global map needed).
        self.panels: dict[str, PanelSpec] = {}
        # Back-compat: flat list of discovered callables (mirrors the pre-Sprint-2
        # `load_plugins()`-derived attribute), regardless of whether calling
        # `register()` on them below succeeds.
        self.plugins = [fn for fn, _ in local + entry_point]

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
            if runners or notifiers or secrets or health or panels:
                from hivepilot.registry import (
                    RUNNER_MAP,
                    SECRETS_MAP,
                    RunnerKindCollisionError,
                    RunnerRegistry,
                    SecretsBackendCollisionError,
                    SecretsRegistry,
                )
                from hivepilot.services.notification_service import (
                    NOTIFIER_MAP,
                    NotifierKindCollisionError,
                    NotifierRegistry,
                )

                # A kind/name collision is a hard stop and propagates uncaught
                # (unlike an isolated broken plugin, which is logged and skipped).
                # Register this one plugin's runners+notifiers+secrets+health+panels
                # atomically: if any entry collides, roll back the entries THIS
                # plugin already added (to the process-global maps, or to this
                # instance's `self.health` / `self.panels`) before re-raising, so
                # an aborted plugin never leaves orphaned, untracked registrations
                # behind.
                applied_runners: list[str] = []
                applied_notifiers: list[str] = []
                applied_secrets: list[str] = []
                applied_health: list[str] = []
                applied_panels: list[str] = []
                try:
                    for kind, cls in (runners or {}).items():
                        was_present = kind in RUNNER_MAP
                        RunnerRegistry.register(kind, cls)
                        if not was_present:
                            applied_runners.append(kind)
                    for notifier_name, notifier_fn in (notifiers or {}).items():
                        was_present = notifier_name in NOTIFIER_MAP
                        NotifierRegistry.register(notifier_name, notifier_fn)
                        if not was_present:
                            applied_notifiers.append(notifier_name)
                    for secret_name, secret_backend in (secrets or {}).items():
                        was_present = secret_name in SECRETS_MAP
                        SecretsRegistry.register(secret_name, secret_backend)
                        if not was_present:
                            applied_secrets.append(secret_name)
                    for health_name, health_fn in (health or {}).items():
                        if health_name in self.health:
                            raise HealthNameCollisionError(
                                f"health check '{health_name}' is already registered; "
                                "refusing to silently replace it"
                            )
                        self.health[health_name] = health_fn
                        applied_health.append(health_name)
                    for panel_spec in panels or []:
                        panel_name = panel_spec["name"]
                        if panel_name in self.panels:
                            raise PanelNameCollisionError(
                                f"panel '{panel_name}' is already registered; "
                                "refusing to silently replace it"
                            )
                        self.panels[panel_name] = {
                            "name": panel_name,
                            "title": panel_spec["title"],
                            "fetch": panel_spec["fetch"],
                            "min_role": panel_spec.get("min_role", "read"),
                        }
                        applied_panels.append(panel_name)
                except (
                    RunnerKindCollisionError,
                    NotifierKindCollisionError,
                    SecretsBackendCollisionError,
                    HealthNameCollisionError,
                    PanelNameCollisionError,
                ):
                    for kind in applied_runners:
                        RUNNER_MAP.pop(kind, None)
                    for notifier_name in applied_notifiers:
                        NOTIFIER_MAP.pop(notifier_name, None)
                    for secret_name in applied_secrets:
                        SECRETS_MAP.pop(secret_name, None)
                    for health_name in applied_health:
                        self.health.pop(health_name, None)
                    for panel_name in applied_panels:
                        self.panels.pop(panel_name, None)
                    raise

                if notifiers:
                    self.declared_notifiers.update(notifiers)

            for hook_name, hook_callable in hooks.items():
                self.hooks.setdefault(hook_name, []).append(hook_callable)

            self.loaded.append(record)

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
