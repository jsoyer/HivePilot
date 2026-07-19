"""Guards that the HTTP API imports with optional TUI deps (textual) absent — as on CI.

Regression: importing persist_plugins_disabled from the Textual-coupled
plugin_manager pulled `textual` into api_service's import chain, cascading
ModuleNotFoundError across every API test on CI (textual not installed there).
"""

from __future__ import annotations

import importlib
import sys


class _BlockTextual:
    def find_spec(self, name, path=None, target=None):
        if name == "textual" or name.startswith("textual."):
            raise ModuleNotFoundError(f"No module named '{name}'")
        return None


def _reimport_without_textual(modname):
    finder = _BlockTextual()
    sys.meta_path.insert(0, finder)
    # drop cached copies so the block takes effect on re-import
    saved = {
        k: v
        for k, v in sys.modules.items()
        if k == modname or k.startswith(modname + ".") or k == "textual" or k.startswith("textual.")
    }
    for k in list(saved):
        del sys.modules[k]
    try:
        return importlib.import_module(modname)
    finally:
        sys.meta_path.remove(finder)
        # restore original module objects to avoid polluting other tests
        for k in [k for k in sys.modules if k == modname or k.startswith(modname + ".")]:
            del sys.modules[k]
        sys.modules.update(saved)


def test_api_service_imports_without_textual():
    mod = _reimport_without_textual("hivepilot.services.api_service")
    paths = {getattr(r, "path", None) for r in mod.app.routes}
    assert "/plugins/{name}/toggle" in paths


def test_plugin_persist_imports_without_textual():
    mod = _reimport_without_textual("hivepilot.ui.plugin_persist")
    assert callable(mod.persist_plugins_disabled)
