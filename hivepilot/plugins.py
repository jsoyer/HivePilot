from __future__ import annotations

import importlib.metadata as metadata
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Callable

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

PLUGIN_ENTRY_POINT_GROUP = "hivepilot.plugins"


@dataclass(slots=True)
class PluginRecord:
    name: str
    source: str
    location: str


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
        if explicit_entry:
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
            if runners:
                from hivepilot.registry import RunnerRegistry

                for kind, cls in runners.items():
                    # RunnerKindCollisionError propagates uncaught — a kind
                    # collision is a hard stop, unlike an isolated broken plugin.
                    RunnerRegistry.register(kind, cls)

            notifiers = hooks.pop("notifiers", None)
            if notifiers:
                self.declared_notifiers.update(notifiers)

            for hook_name, hook_callable in hooks.items():
                self.hooks.setdefault(hook_name, []).append(hook_callable)

            self.loaded.append(record)

    def run_hook(self, hook_name: str, **kwargs: Any) -> None:
        for hook in self.hooks.get(hook_name, []):
            hook(**kwargs)
