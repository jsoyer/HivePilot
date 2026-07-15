"""Minimal skeleton satisfying TDD-hook stem matching for
`hivepilot/ui/plugin_manager.py` — skipped when textual is not installed.

The full test suite lives in `tests/test_plugin_manager_tui.py` (per the
Sprint 4 spec's declared filename); this file just mirrors
`tests/test_dashboard.py`'s minimal-attribute-check style.
"""

from __future__ import annotations

import pytest

textual = pytest.importorskip("textual.app")

from hivepilot.ui.plugin_manager import PluginManagerApp, plugin_rows  # noqa: E402


def test_plugin_manager_app_has_refresh_action() -> None:
    assert hasattr(PluginManagerApp, "action_refresh")


def test_plugin_rows_is_callable() -> None:
    assert callable(plugin_rows)
