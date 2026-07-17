"""Minimal companion test file for `plugins/qwen_code.py`.

Exists ONLY to satisfy this repo's TDD pre-commit hook, which requires a
`tests/test_<stem>.py` matching the production file's basename before that
file may be created/edited. The FULL test suite for the three new Sprint 3
agent plugins (pi/qwen-code/kimi-cli) — gating, real-PluginManager
resolution, built-argv shape, qwen-code api-mode — lives in
`tests/test_new_agent_plugins.py` per the sprint's declared file
boundaries; see that file's module docstring. This file just smoke-tests
`register()`'s gating for `qwen-code` in isolation so it isn't a no-op stub.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

from hivepilot.config import settings
from hivepilot.runners.prompt_cli_runner import QwenCodeRunner

REPO_ROOT = Path(__file__).parent.parent


def _load_plugin_module():
    path = REPO_ROOT / "plugins" / "qwen_code.py"
    spec = importlib.util.spec_from_file_location("hivepilot_plugin_qwen_code_smoke", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_register_returns_qwen_code_runner_when_active(monkeypatch) -> None:
    module = _load_plugin_module()
    monkeypatch.setattr(settings, "qwen_code_enabled", True, raising=False)
    with patch.object(module.shutil, "which", return_value="/usr/local/bin/qwen"):
        hooks = module.register()
    assert hooks.get("runners") == {"qwen-code": QwenCodeRunner}


def test_register_returns_empty_when_binary_absent(monkeypatch) -> None:
    module = _load_plugin_module()
    monkeypatch.setattr(settings, "qwen_code_enabled", True, raising=False)
    with patch.object(module.shutil, "which", return_value=None):
        assert module.register() == {}
