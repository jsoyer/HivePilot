from __future__ import annotations

from importlib import import_module
from typing import Any, Callable

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


def load_plugins(entry: str | None = None) -> list[Callable[..., Any]]:
    """Load plugin callables from a module path or from `plugins/` directory."""
    plugins: list[Callable[..., Any]] = []
    if entry:
        module_name, attr = entry.split(":") if ":" in entry else (entry, "register")
        module = import_module(module_name)
        plugin_callable = getattr(module, attr)
        plugins.append(plugin_callable)
    else:
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
                    spec = importlib.util.spec_from_file_location(
                        f"hivepilot_plugin_{file.stem}", file
                    )
                    if spec and spec.loader:
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        if hasattr(module, "register"):
                            plugins.append(module.register)
                except Exception as exc:  # noqa: BLE001 — a broken plugin must not kill a run
                    logger.warning("plugins.load_failed", file=str(file), error=str(exc))
    logger.info("plugins.loaded", count=len(plugins))
    return plugins


class PluginManager:
    def __init__(self) -> None:
        self.plugins = load_plugins(settings.__dict__.get("plugins_entry"))
        self.hooks: dict[str, list[Any]] = {"before_step": [], "after_step": []}
        for plugin in self.plugins:
            hooks = plugin()
            for hook_name, hook_callable in hooks.items():
                self.hooks.setdefault(hook_name, []).append(hook_callable)

    def run_hook(self, hook_name: str, **kwargs: Any) -> None:
        for hook in self.hooks.get(hook_name, []):
            hook(**kwargs)
