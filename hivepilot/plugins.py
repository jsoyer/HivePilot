from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any, Callable, List

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
            for file in plugin_dir.glob("*.py"):
                module_name = f"plugins.{file.stem}"
                module = import_module(module_name)
                if hasattr(module, "register"):
                    plugins.append(module.register)
    logger.info("plugins.loaded", count=len(plugins))
    return plugins


class PluginManager:
    def __init__(self) -> None:
        self.plugins = load_plugins(settings.__dict__.get("plugins_entry"))
        self.hooks = {"before_step": [], "after_step": []}
        for plugin in self.plugins:
            hooks = plugin()
            for hook_name, hook_callable in hooks.items():
                self.hooks.setdefault(hook_name, []).append(hook_callable)

    def run_hook(self, hook_name: str, **kwargs: Any) -> None:
        for hook in self.hooks.get(hook_name, []):
            hook(**kwargs)
