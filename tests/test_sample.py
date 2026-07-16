"""Tests for plugins/sample.py — the example plugin, including its demo
Mirador panel contribution (Sprint 1: panel plugin type)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Loaded by file path (not `sys.path` + `import plugins.sample`) so this test
# never inserts a `plugins` package into `sys.modules` — that would leak
# across the suite and break `tests/test_plugins.py`'s
# `assert "plugins" not in sys.modules` isolation assumption. Mirrors how
# `hivepilot/plugins.py::_scan_local_plugins` itself loads local plugins.
_SAMPLE_PATH = Path(__file__).resolve().parent.parent / "plugins" / "sample.py"
_spec = importlib.util.spec_from_file_location("hivepilot_test_sample_plugin", _SAMPLE_PATH)
assert _spec and _spec.loader
sample = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sample)

_sample_fetch = sample._sample_fetch
register = sample.register


class TestSampleRegister:
    def test_register_returns_before_and_after_step_hooks(self) -> None:
        hooks = register()
        assert callable(hooks["before_step"])
        assert callable(hooks["after_step"])

    def test_register_declares_a_sample_panel(self) -> None:
        hooks = register()
        panels = hooks["panels"]
        assert len(panels) == 1
        panel = panels[0]
        assert panel["name"] == "sample_stats"
        assert panel["title"] == "Sample Stats"
        assert callable(panel["fetch"])


class TestSamplePanelFetch:
    def test_sample_fetch_returns_one_of_each_section_kind(self) -> None:
        data = _sample_fetch()
        kinds = [section["kind"] for section in data["sections"]]
        assert kinds == ["stat", "table", "text"]

    def test_sample_fetch_is_normalizable(self) -> None:
        """The demo panel's raw return value must satisfy
        `normalize_panel_data` (Sprint 1 contract) — Sprints 2/3 depend on
        being able to render it directly."""
        from hivepilot.plugins import normalize_panel_data

        normalized = normalize_panel_data(_sample_fetch())
        assert len(normalized["sections"]) == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
