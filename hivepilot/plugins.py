from __future__ import annotations

import importlib.metadata as metadata
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Callable, NamedTuple

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
            if runners or notifiers or secrets or health:
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
                # Register this one plugin's runners+notifiers+secrets+health
                # atomically: if any entry collides, roll back the entries THIS
                # plugin already added (to the process-global maps, or to this
                # instance's `self.health`) before re-raising, so an aborted
                # plugin never leaves orphaned, untracked registrations behind.
                applied_runners: list[str] = []
                applied_notifiers: list[str] = []
                applied_secrets: list[str] = []
                applied_health: list[str] = []
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
                except (
                    RunnerKindCollisionError,
                    NotifierKindCollisionError,
                    SecretsBackendCollisionError,
                    HealthNameCollisionError,
                ):
                    for kind in applied_runners:
                        RUNNER_MAP.pop(kind, None)
                    for notifier_name in applied_notifiers:
                        NOTIFIER_MAP.pop(notifier_name, None)
                    for secret_name in applied_secrets:
                        SECRETS_MAP.pop(secret_name, None)
                    for health_name in applied_health:
                        self.health.pop(health_name, None)
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
        `HealthStatus("error", "<ExceptionType>: <short msg>")` — the same
        never-crash guarantee every other plugin hook in this repo has.
        """
        fn = self.health.get(name)
        if fn is None:
            return HealthStatus("error", f"no health check registered for {name!r}")
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 — a health check must never crash
            logger.warning("plugins.health_check_failed", name=name, error=str(exc))
            return HealthStatus("error", f"{type(exc).__name__}: {exc}")
        return _normalize_health_result(result)

    def check_all(self) -> dict[str, HealthStatus]:
        """Run every registered health check. Never raises (see
        `run_health_check`) — safe to call unconditionally from `plugins
        list` / `plugins health` / the TUI."""
        return {name: self.run_health_check(name) for name in self.health}
