"""Tests for the Mirador `panel` plugin type (Sprint 1: Python core).

Covers: registration/discovery, collision -> atomic rollback (consistent
with the existing runner/notifier/secrets/health collision tests in
`tests/test_plugins.py`), disabled/kill-switch skip, `normalize_panel_data`
validation/coercion, and `run_panel_fetch`'s never-raise + no-secret-leak
guarantee.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Stub optional deps before importing (mirrors tests/test_plugins.py).
_STUBS = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
]

for _mod in _STUBS:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


class TestPanelRegistration:
    def test_local_plugin_panel_is_collected(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "with_panel.py").write_text(
            "def _fetch():\n"
            "    return {'sections': [{'kind': 'text', 'content': 'hi'}]}\n"
            "def register():\n"
            "    return {'panels': [{'name': 'p1', 'title': 'P1', 'fetch': _fetch}]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()

        assert "p1" in pm.panels
        panel = pm.get_panel("p1")
        assert panel is not None
        assert panel["title"] == "P1"
        assert panel["min_role"] == "read"  # default
        assert callable(panel["fetch"])

    def test_list_panels_is_sorted_by_name(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "two_panels.py").write_text(
            "def _fetch():\n    return {'sections': []}\n"
            "def register():\n"
            "    return {'panels': ["
            "        {'name': 'zzz', 'title': 'Z', 'fetch': _fetch},"
            "        {'name': 'aaa', 'title': 'A', 'fetch': _fetch},"
            "    ]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()

        names = [p["name"] for p in pm.list_panels()]
        assert names == ["aaa", "zzz"]

    def test_get_panel_returns_none_for_unknown_name(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = plugins_mod.PluginManager()

        assert pm.get_panel("does-not-exist") is None

    def test_panel_honors_explicit_min_role(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "admin_panel.py").write_text(
            "def _fetch():\n    return {'sections': []}\n"
            "def register():\n"
            "    return {'panels': [{'name': 'admin', 'title': 'Admin', "
            "'fetch': _fetch, 'min_role': 'admin'}]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()

        assert pm.get_panel("admin")["min_role"] == "admin"


class TestPanelCollisionRollback:
    def test_duplicate_panel_name_across_plugins_collides(self, tmp_path, monkeypatch) -> None:
        """Two plugins declaring the SAME panel name is a hard-stop
        collision, consistent with runners/notifiers/secrets/health."""
        from hivepilot import plugins as plugins_mod
        from hivepilot.plugins import PanelNameCollisionError

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "a_first.py").write_text(
            "def _fetch():\n    return {'sections': []}\n"
            "def register():\n"
            "    return {'panels': [{'name': 'shared', 'title': 'A', 'fetch': _fetch}]}\n",
            encoding="utf-8",
        )
        (pdir / "b_second.py").write_text(
            "def _fetch():\n    return {'sections': []}\n"
            "def register():\n"
            "    return {'panels': [{'name': 'shared', 'title': 'B', 'fetch': _fetch}]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(PanelNameCollisionError):
            plugins_mod.PluginManager()

    def test_mixed_type_collision_rolls_back_runner_and_notifier_when_panel_collides(
        self, tmp_path, monkeypatch
    ) -> None:
        """Cross-type atomicity when `panels` is the failing member: plugin A
        registers panel name 'dup' first; plugin B declares a runner AND a
        notifier AND a colliding panel name 'dup' — the whole plugin B
        contribution (runner + notifier) must be rolled back, not just the
        panel entry. Mirrors the equivalent health-collision test in
        `tests/test_plugins.py`.
        """
        from hivepilot import plugins as plugins_mod
        from hivepilot.plugins import PanelNameCollisionError
        from hivepilot.registry import RUNNER_MAP
        from hivepilot.services.notification_service import NOTIFIER_MAP

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        # 'a_' sorts before 'b_' — plugin A's 'dup' panel registers
        # successfully before plugin B is even attempted.
        (pdir / "a_owner.py").write_text(
            "def _fetch():\n    return {'sections': []}\n"
            "def register():\n"
            "    return {'panels': [{'name': 'dup', 'title': 'A', 'fetch': _fetch}]}\n",
            encoding="utf-8",
        )
        (pdir / "b_mixed.py").write_text(
            """
class BRunner:
    def __init__(self, definition, settings):
        pass

    def run(self, payload):
        return None


def _b_notifier(msg):
    return None


def _b_fetch():
    return {'sections': []}


def register():
    # 'b-kind' runner and 'b-notif' notifier register first, then the
    # 'dup' panel name collides with plugin A's already-registered one.
    return {
        "runners": {"b-kind": BRunner},
        "notifiers": {"b-notif": _b_notifier},
        "panels": [{"name": "dup", "title": "B", "fetch": _b_fetch}],
    }
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(PanelNameCollisionError):
            plugins_mod.PluginManager()

        assert "b-kind" not in RUNNER_MAP
        assert "b-notif" not in NOTIFIER_MAP

    def test_collision_rolls_back_that_plugins_earlier_panel_registrations(
        self, tmp_path, monkeypatch
    ) -> None:
        """A single plugin declaring two panels where the SECOND collides
        with an already-registered one must not leave the FIRST orphaned:
        registration of one plugin's panels is atomic."""
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "a_owner.py").write_text(
            "def _fetch():\n    return {'sections': []}\n"
            "def register():\n"
            "    return {'panels': [{'name': 'taken', 'title': 'A', 'fetch': _fetch}]}\n",
            encoding="utf-8",
        )
        (pdir / "b_partial.py").write_text(
            "def _fetch():\n    return {'sections': []}\n"
            "def register():\n"
            "    return {'panels': ["
            "        {'name': 'fresh', 'title': 'B1', 'fetch': _fetch},"
            "        {'name': 'taken', 'title': 'B2', 'fetch': _fetch},"
            "    ]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(plugins_mod.PanelNameCollisionError):
            plugins_mod.PluginManager()


class TestPanelDisabledSkip:
    def test_disabled_plugin_contributes_no_panel(self, tmp_path, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "off.py").write_text(
            "def _fetch():\n    return {'sections': []}\n"
            "def register():\n"
            "    return {'panels': [{'name': 'off_panel', 'title': 'Off', 'fetch': _fetch}]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_disabled", ["off"], raising=False)

        pm = plugins_mod.PluginManager()

        assert "off_panel" not in pm.panels

    def test_plugins_enabled_false_contributes_no_panels_at_all(
        self, tmp_path, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "kill.py").write_text(
            "def _fetch():\n    return {'sections': []}\n"
            "def register():\n"
            "    return {'panels': [{'name': 'kill_panel', 'title': 'K', 'fetch': _fetch}]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_enabled", False, raising=False)

        pm = plugins_mod.PluginManager()

        assert pm.panels == {}


class TestNormalizePanelData:
    def test_accepts_valid_data_with_all_section_kinds(self) -> None:
        from hivepilot.plugins import normalize_panel_data

        raw = {
            "sections": [
                {"kind": "stat", "label": "l", "value": "v", "status": "ok"},
                {"kind": "table", "columns": ["a"], "rows": [["1"]]},
                {"kind": "text", "content": "hi"},
            ]
        }
        result = normalize_panel_data(raw)
        assert len(result["sections"]) == 3

    def test_accepts_stat_section_with_none_status(self) -> None:
        from hivepilot.plugins import normalize_panel_data

        raw = {"sections": [{"kind": "stat", "label": "l", "value": "v", "status": None}]}
        result = normalize_panel_data(raw)
        assert result["sections"][0]["status"] is None

    def test_unknown_stat_status_is_coerced_to_none_not_rejected(self) -> None:
        from hivepilot.plugins import normalize_panel_data

        raw = {"sections": [{"kind": "stat", "label": "l", "value": "v", "status": "bogus"}]}
        result = normalize_panel_data(raw)
        assert result["sections"][0]["status"] is None

    def test_rejects_non_dict_top_level(self) -> None:
        from hivepilot.plugins import PanelDataError, normalize_panel_data

        with pytest.raises(PanelDataError):
            normalize_panel_data("not a dict")

    def test_rejects_missing_sections_list(self) -> None:
        from hivepilot.plugins import PanelDataError, normalize_panel_data

        with pytest.raises(PanelDataError):
            normalize_panel_data({})

    def test_rejects_unknown_section_kind(self) -> None:
        from hivepilot.plugins import PanelDataError, normalize_panel_data

        with pytest.raises(PanelDataError):
            normalize_panel_data({"sections": [{"kind": "bogus-kind"}]})

    def test_rejects_stat_section_missing_required_fields(self) -> None:
        from hivepilot.plugins import PanelDataError, normalize_panel_data

        with pytest.raises(PanelDataError):
            normalize_panel_data({"sections": [{"kind": "stat", "label": "only-label"}]})

    def test_rejects_table_section_with_non_string_cell(self) -> None:
        from hivepilot.plugins import PanelDataError, normalize_panel_data

        with pytest.raises(PanelDataError):
            normalize_panel_data({"sections": [{"kind": "table", "columns": ["a"], "rows": [[1]]}]})

    def test_rejects_text_section_with_non_string_content(self) -> None:
        from hivepilot.plugins import PanelDataError, normalize_panel_data

        with pytest.raises(PanelDataError):
            normalize_panel_data({"sections": [{"kind": "text", "content": 123}]})


class TestRunPanelFetch:
    def test_run_panel_fetch_returns_normalized_data_on_success(
        self, tmp_path, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "good.py").write_text(
            "def _fetch():\n"
            "    return {'sections': [{'kind': 'text', 'content': 'hi'}]}\n"
            "def register():\n"
            "    return {'panels': [{'name': 'good', 'title': 'G', 'fetch': _fetch}]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = plugins_mod.PluginManager()

        result = pm.run_panel_fetch("good")

        assert result["sections"] == [{"kind": "text", "content": "hi"}]

    def test_run_panel_fetch_on_raising_panel_never_raises_no_secret_leak(
        self, tmp_path, monkeypatch
    ) -> None:
        """A raising fetch() returns an error PanelData with ONLY the
        exception TYPE name — the exception message (which could carry a
        secret/token value, per Phase 19 discipline) must never be echoed
        back to callers."""
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "boom.py").write_text(
            "def _fetch():\n"
            "    raise RuntimeError('super-secret-token-abc123')\n"
            "def register():\n"
            "    return {'panels': [{'name': 'boom', 'title': 'B', 'fetch': _fetch}]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = plugins_mod.PluginManager()

        result = pm.run_panel_fetch("boom")  # must not raise

        serialized = str(result)
        assert "super-secret-token-abc123" not in serialized
        assert "RuntimeError" in serialized
        stat = result["sections"][0]
        assert stat["status"] == "error"

    def test_run_panel_fetch_on_malformed_return_value_never_raises(
        self, tmp_path, monkeypatch
    ) -> None:
        """A fetch() that returns a structurally-invalid PanelData (rejected
        by `normalize_panel_data`) must also fall back to the error panel,
        not propagate `PanelDataError` to the caller."""
        from hivepilot import plugins as plugins_mod

        pdir = tmp_path / "plugins"
        pdir.mkdir()
        (pdir / "malformed.py").write_text(
            "def _fetch():\n    return {'not': 'a valid panel shape'}\n"
            "def register():\n"
            "    return {'panels': [{'name': 'malformed', 'title': 'M', 'fetch': _fetch}]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = plugins_mod.PluginManager()

        result = pm.run_panel_fetch("malformed")  # must not raise

        stat = result["sections"][0]
        assert stat["status"] == "error"
        assert "PanelDataError" in stat["value"]

    def test_run_panel_fetch_on_unknown_name_returns_error_panel(
        self, tmp_path, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = plugins_mod.PluginManager()

        result = pm.run_panel_fetch("does-not-exist")

        assert result["sections"][0]["status"] == "error"


class TestSamplePanelIntegration:
    def test_sample_plugin_panel_loads_via_plugin_manager(self, monkeypatch) -> None:
        """The repo's own `plugins/sample.py` panel is discoverable and
        fetchable through the real PluginManager (no tmp_path override —
        uses the actual `plugins/` directory), matching how Sprints 2/3 will
        consume it."""
        from hivepilot import plugins as plugins_mod

        pm = plugins_mod.PluginManager()

        panel = pm.get_panel("sample_stats")
        assert panel is not None
        assert panel["title"] == "Sample Stats"

        data = pm.run_panel_fetch("sample_stats")
        kinds = [s["kind"] for s in data["sections"]]
        assert kinds == ["stat", "table", "text"]
