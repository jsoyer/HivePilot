"""Tests for the built-in `plugins` graph source (Mirador Graph View PRD,
Sprint 1) — `hivepilot/graph_sources/plugins_source.py`.

Seeds a REAL `PluginManager` from a temp `plugins/` directory contributing
one of each collision-checked contribution type (runner/notifier/secret/
panel/skill) plus a lifecycle hook — mirrors
`tests/test_mirador_contract.py`'s `seeded_panel_plugin_manager` fixture's
seeding technique, extended to every contribution category this graph
source renders. Function-scoped (rebuilt fresh per test): `tests/
conftest.py`'s autouse `_isolate_runner_and_notifier_maps` fixture resets
`RUNNER_MAP`/`NOTIFIER_MAP`/`SECRETS_MAP` to their pristine, builtins-only
baseline after EVERY test, so each test's fresh `PluginManager()` always
registers into a clean map — no cross-test collision risk despite
`hivepilot/plugins.py`'s `_scan_local_plugins` re-execing the plugin source
(fresh class objects) on every construction.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hivepilot import graph as graph_module
from hivepilot.graph_sources.plugins_source import PLUGINS_GRAPH_SOURCE, _build_graph, _node_detail

_PLUGIN_SOURCE = """
class _GsFakeRunner:
    def __init__(self, definition, settings):
        self.definition = definition
        self.settings = settings

    def run(self, payload):
        pass


class _GsFakeSecretsBackend:
    def resolve(self, ref, settings):
        return "unused-in-this-test"


def _gs_fetch():
    return {"sections": [{"kind": "text", "content": "hello"}]}


def _gs_before_step(**kwargs):
    pass


def register():
    return {
        "runners": {"gs_contract_runner": _GsFakeRunner},
        "notifiers": {"gs_contract_notifier": lambda msg: None},
        "secrets": {"gs_contract_secret": _GsFakeSecretsBackend()},
        "panels": [
            {"name": "gs_contract_panel", "title": "Contract Panel", "fetch": _gs_fetch}
        ],
        "skills": [
            {
                "name": "gs_contract_skill",
                "description": "d",
                "provider": "p",
                "files": {"SKILL.md": "x"},
            }
        ],
        "before_step": _gs_before_step,
    }
"""


@pytest.fixture()
def seeded_graph_plugin_manager(tmp_path, monkeypatch):
    from hivepilot import plugins as plugins_mod

    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "gs_contract_plugin.py").write_text(_PLUGIN_SOURCE, encoding="utf-8")

    monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
    return plugins_mod.PluginManager()


@pytest.fixture()
def patched_orchestrator(monkeypatch, seeded_graph_plugin_manager):
    from hivepilot.services import api_service

    monkeypatch.setattr(
        api_service,
        "_get_orchestrator",
        lambda: SimpleNamespace(plugins=seeded_graph_plugin_manager),
    )
    return seeded_graph_plugin_manager


@pytest.fixture()
def ctx():
    return graph_module.GraphContext(tenant="default", role="read")


# ---------------------------------------------------------------------------
# Built-in registration
# ---------------------------------------------------------------------------


class TestPluginsGraphSourceRegistration:
    def test_registered_under_plugins_name(self):
        import hivepilot.graph_sources  # noqa: F401 - side-effect import

        assert graph_module.get_graph_source("plugins") is PLUGINS_GRAPH_SOURCE

    def test_spec_shape(self):
        assert PLUGINS_GRAPH_SOURCE.name == "plugins"
        assert PLUGINS_GRAPH_SOURCE.title == "Plugins"
        assert PLUGINS_GRAPH_SOURCE.min_role == "read"
        assert PLUGINS_GRAPH_SOURCE.node_detail is not None


# ---------------------------------------------------------------------------
# _build_graph
# ---------------------------------------------------------------------------


class TestBuildGraph:
    def test_role_nodes_present(self, ctx, patched_orchestrator):
        data = _build_graph(ctx)
        assert data.source == "plugins"
        role_ids = {n.id for n in data.nodes if n.kind == "role"}
        assert "role:developer" in role_ids

    def test_role_edge_to_runner(self, ctx, patched_orchestrator):
        data = _build_graph(ctx)
        uses_edges = [e for e in data.edges if e.kind == "uses"]
        assert any(e.source == "role:developer" and e.target == "runner:claude" for e in uses_edges)

    def test_at_least_one_node_per_present_plugin_category(self, ctx, patched_orchestrator):
        data = _build_graph(ctx)
        node_kinds = {n.kind for n in data.nodes}
        for expected_kind in ("plugin", "runner", "notifier", "secret", "panel", "skill", "hook"):
            assert expected_kind in node_kinds, f"expected at least one {expected_kind} node"

    def test_contributed_plugin_node_present(self, ctx, patched_orchestrator):
        data = _build_graph(ctx)
        plugin_ids = {n.id for n in data.nodes}
        assert "plugin:gs_contract_plugin" in plugin_ids

    def test_edges_link_plugin_to_its_contributions(self, ctx, patched_orchestrator):
        data = _build_graph(ctx)
        targets = {e.target for e in data.edges if e.source == "plugin:gs_contract_plugin"}
        assert "runner:gs_contract_runner" in targets
        assert "notifier:gs_contract_notifier" in targets
        assert "secret:gs_contract_secret" in targets
        assert "panel:gs_contract_panel" in targets
        assert "skill:gs_contract_skill" in targets

    def test_no_duplicate_node_ids(self, ctx, patched_orchestrator):
        data = _build_graph(ctx)
        ids = [n.id for n in data.nodes]
        assert len(ids) == len(set(ids))

    def test_secret_node_has_no_secret_value_in_meta(self, ctx, patched_orchestrator):
        data = _build_graph(ctx)
        secret_node = next(n for n in data.nodes if n.id == "secret:gs_contract_secret")
        assert "unused-in-this-test" not in str(secret_node.meta)
        assert "unused-in-this-test" not in secret_node.label


# ---------------------------------------------------------------------------
# _node_detail
# ---------------------------------------------------------------------------


class TestNodeDetail:
    def test_plugin_detail(self, ctx, patched_orchestrator):
        detail = _node_detail(ctx, "plugin:gs_contract_plugin")
        assert detail is not None
        assert detail.title == "gs_contract_plugin"
        assert "plugin" in detail.tags

    def test_role_detail(self, ctx, patched_orchestrator):
        detail = _node_detail(ctx, "role:developer")
        assert detail is not None
        assert detail.title == "Developer"
        assert detail.sections[0]["kind"] == "text"

    def test_secret_detail_has_name_and_status_never_value(self, ctx, patched_orchestrator):
        detail = _node_detail(ctx, "secret:gs_contract_secret")
        assert detail is not None
        assert detail.title == "gs_contract_secret"
        assert "secret" in detail.tags
        assert any(status in detail.tags for status in ("enabled", "disabled"))
        for section in detail.sections:
            assert "unused-in-this-test" not in str(section)

    def test_unknown_node_returns_none(self, ctx, patched_orchestrator):
        assert _node_detail(ctx, "role:does-not-exist") is None
        assert _node_detail(ctx, "plugin:does-not-exist") is None
        assert _node_detail(ctx, "totally-unknown-prefix") is None

    def test_runner_detail_status(self, ctx, patched_orchestrator):
        detail = _node_detail(ctx, "runner:gs_contract_runner")
        assert detail is not None
        assert detail.tags[0] == "runner"
        assert detail.tags[1] == "enabled"
