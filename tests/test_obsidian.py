"""
Smoke test for plugins/obsidian.py — satisfies the repo's TDD hook naming
convention (`plugins/obsidian.py` -> `tests/test_obsidian.py`).

The full test suite for this plugin lives in `tests/test_plugin_obsidian.py`
(per the Sprint 2 spec, mirroring `tests/test_rtk.py`'s coverage depth) —
this file only proves the module loads and `register()` returns the expected
shape.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).parent.parent
OBSIDIAN_PLUGIN_PATH = REPO_ROOT / "plugins" / "obsidian.py"


def _load_obsidian_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_obsidian_smoke", OBSIDIAN_PLUGIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_obsidian_plugin_module_loads_and_registers() -> None:
    module = _load_obsidian_module()
    hooks = module.register()

    assert "notifiers" in hooks
    assert "obsidian" in hooks["notifiers"]
    assert "on_pipeline_end" in hooks
    assert "on_error" in hooks
