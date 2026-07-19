"""Tests for the `graph_sources` plugin capability (Mirador Graph View PRD,
Sprint 4): plugin `register()["graph_sources"]` -> `PluginManager` ->
`hivepilot.graph.register_graph_source` wiring, fail-closed disabled-plugin
handling, collision handling + atomic rollback, never-raise fetch routing
through the SAME wrapper built-ins use, fail-closed bogus `min_role`
denial, and `PluginRecord.contributions["graph_sources"]` taxonomy
attribution (feeding `plugin_index.graph_source_contributions` /
`plugins list`).

Mirrors `tests/test_plugins.py` (local-file plugin scanning + collision/
rollback style, e.g. `TestPluginHealthSurface`) and `tests/test_graph_api.py`
(API-layer `/v1/graph/{source}` integration + `isolated_graph_sources`
isolation fixture, since `hivepilot.graph`'s `_GRAPH_SOURCES` dict is
process-global and must never leak a test-registered source into another
test module).
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

import hivepilot.graph_sources  # noqa: F401 - side-effect import, registers built-ins
from hivepilot import graph as graph_module
from hivepilot.services.token_service import add_token


@pytest.fixture()
def isolated_graph_sources(monkeypatch):
    """Fresh COPY of the module-global graph-source registry, seeded with
    whatever built-ins are already registered — mirrors
    `tests/test_graph_api.py::isolated_graph_sources` so a plugin-
    contributed source registered by one test never leaks into another."""
    monkeypatch.setattr(graph_module, "_GRAPH_SOURCES", dict(graph_module._GRAPH_SOURCES))
    return graph_module._GRAPH_SOURCES


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _auth(raw_token: str) -> dict:
    return {"Authorization": f"Bearer {raw_token}"}


def _write_plugin_dir(tmp_path):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    return pdir


# ---------------------------------------------------------------------------
# AC1 — a plugin-contributed GraphSourceSpec is registered + reachable via
# GET /v1/graph/{name}
# ---------------------------------------------------------------------------


class TestPluginGraphSourceRegistration:
    def test_plugin_contributed_source_registered_and_reachable(
        self, tmp_path, monkeypatch, isolated_graph_sources, tmp_tokens_file, api_client
    ):
        from hivepilot import plugins as plugins_mod

        pdir = _write_plugin_dir(tmp_path)
        (pdir / "test_graph_plugin.py").write_text(
            "from hivepilot.graph import GraphData, GraphNode, GraphSourceSpec\n"
            "\n"
            "def _data(ctx):\n"
            "    return GraphData(\n"
            "        source='plugin-graph',\n"
            "        nodes=(GraphNode(id='n1', label='N1', kind='demo'),),\n"
            "    )\n"
            "\n"
            "def register():\n"
            "    return {\n"
            "        'graph_sources': [\n"
            "            GraphSourceSpec(name='plugin-graph', data=_data, min_role='read')\n"
            "        ]\n"
            "    }\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()

        assert graph_module.get_graph_source("plugin-graph") is not None
        record = next(r for r in pm.loaded if r.name == "test_graph_plugin")
        assert record.contributions.get("graph_sources") == ["plugin-graph"]

        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/plugin-graph", headers=_auth(raw))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "plugin-graph"
        assert any(n["id"] == "n1" for n in body["nodes"])

    def test_plugin_declaring_only_graph_sources_is_attributed(
        self, tmp_path, monkeypatch, isolated_graph_sources
    ) -> None:
        """A plugin whose `register()` returns ONLY `graph_sources` (no
        runner/notifier/secrets/health/panel/skill) must still enter the
        atomic collision-checked block — mirrors every other single-kind
        contribution type."""
        from hivepilot import plugins as plugins_mod

        pdir = _write_plugin_dir(tmp_path)
        (pdir / "only_graph.py").write_text(
            "from hivepilot.graph import GraphData, GraphSourceSpec\n"
            "\n"
            "def _data(ctx):\n"
            "    return GraphData(source='only-graph', nodes=())\n"
            "\n"
            "def register():\n"
            "    return {'graph_sources': [GraphSourceSpec(name='only-graph', data=_data)]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()
        record = next(r for r in pm.loaded if r.name == "only_graph")
        assert record.contributions == {"graph_sources": ["only-graph"]}


# ---------------------------------------------------------------------------
# AC2 — a DISABLED plugin contributes NO graph source (fail-closed)
# ---------------------------------------------------------------------------


class TestDisabledPluginContributesNoGraphSource:
    def test_disabled_plugin_registers_nothing(
        self, tmp_path, monkeypatch, isolated_graph_sources
    ) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = _write_plugin_dir(tmp_path)
        (pdir / "disabled_graph_plugin.py").write_text(
            "from hivepilot.graph import GraphData, GraphSourceSpec\n"
            "\n"
            "def _data(ctx):\n"
            "    return GraphData(source='disabled-graph', nodes=())\n"
            "\n"
            "def register():\n"
            "    return {\n"
            "        'graph_sources': [GraphSourceSpec(name='disabled-graph', data=_data)]\n"
            "    }\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(
            plugins_mod.settings, "plugins_disabled", ["disabled_graph_plugin"], raising=False
        )

        pm = plugins_mod.PluginManager()

        assert "disabled_graph_plugin" not in {r.name for r in pm.loaded}
        assert graph_module.get_graph_source("disabled-graph") is None


# ---------------------------------------------------------------------------
# AC3 — a name collision (built-in or another plugin) is a hard stop, never
# a crash and never a silent overwrite; atomic rollback of the SAME
# plugin's other contributions (including earlier graph sources).
# ---------------------------------------------------------------------------


class TestGraphSourceCollision:
    def test_collision_with_builtin_raises_and_does_not_overwrite(
        self, tmp_path, monkeypatch, isolated_graph_sources
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.graph import GraphSourceNameCollisionError

        original_plugins_source = graph_module.get_graph_source("plugins")
        assert original_plugins_source is not None

        pdir = _write_plugin_dir(tmp_path)
        (pdir / "colliding_graph_plugin.py").write_text(
            "from hivepilot.graph import GraphData, GraphSourceSpec\n"
            "\n"
            "def _data(ctx):\n"
            "    return GraphData(source='plugins', nodes=())\n"
            "\n"
            "def register():\n"
            "    return {'graph_sources': [GraphSourceSpec(name='plugins', data=_data)]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(GraphSourceNameCollisionError):
            plugins_mod.PluginManager()

        # Not a crash-and-corrupt: the built-in 'plugins' source is untouched
        # (same object, never silently replaced).
        assert graph_module.get_graph_source("plugins") is original_plugins_source

    def test_collision_rolls_back_same_plugins_other_contributions(
        self, tmp_path, monkeypatch, isolated_graph_sources
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.graph import GraphSourceNameCollisionError
        from hivepilot.registry import RUNNER_MAP

        pdir = _write_plugin_dir(tmp_path)
        (pdir / "mixed_colliding_graph.py").write_text(
            "from hivepilot.graph import GraphData, GraphSourceSpec\n"
            "\n"
            "class DemoRunner:\n"
            "    def __init__(self, definition, settings):\n"
            "        pass\n"
            "\n"
            "    def run(self, payload):\n"
            "        return None\n"
            "\n"
            "def _data(ctx):\n"
            "    return GraphData(source='plugins', nodes=())\n"
            "\n"
            "def register():\n"
            "    return {\n"
            "        'runners': {'mixed-graph-kind': DemoRunner},\n"
            "        'graph_sources': [GraphSourceSpec(name='plugins', data=_data)],\n"
            "    }\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(GraphSourceNameCollisionError):
            plugins_mod.PluginManager()

        assert "mixed-graph-kind" not in RUNNER_MAP

    def test_earlier_graph_source_in_same_plugin_rolled_back_on_later_collision(
        self, tmp_path, monkeypatch, isolated_graph_sources
    ) -> None:
        """A single plugin declaring TWO graph sources where the SECOND
        collides with a built-in must not leave the FIRST orphaned/live:
        both are staged into `_StagedPluginState.graph_source_map` first
        (never registered into the live `hivepilot.graph` registry until
        `_commit`), so the collision on the second name means the whole
        staged pass is discarded before anything ever reaches the live
        registry. Mirrors `tests/test_plugins.py::TestPluginHealthSurface::
        test_collision_rolls_back_that_plugins_earlier_health_registrations`.
        """
        from hivepilot import plugins as plugins_mod
        from hivepilot.graph import GraphSourceNameCollisionError

        pdir = _write_plugin_dir(tmp_path)
        (pdir / "b_partial_graph.py").write_text(
            "from hivepilot.graph import GraphData, GraphSourceSpec\n"
            "\n"
            "def _d1(ctx):\n"
            "    return GraphData(source='rb-fresh', nodes=())\n"
            "\n"
            "def _d2(ctx):\n"
            "    return GraphData(source='plugins', nodes=())\n"
            "\n"
            "def register():\n"
            "    return {\n"
            "        'graph_sources': [\n"
            "            GraphSourceSpec(name='rb-fresh', data=_d1),\n"
            "            GraphSourceSpec(name='plugins', data=_d2),\n"
            "        ]\n"
            "    }\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(GraphSourceNameCollisionError):
            plugins_mod.PluginManager()

        assert graph_module.get_graph_source("rb-fresh") is None

    def test_collision_between_two_distinct_plugins_still_raises(
        self, tmp_path, monkeypatch, isolated_graph_sources
    ) -> None:
        """The ownership model (F1/F2 fix) must NOT weaken genuine
        cross-plugin collision detection — only a manager's OWN previously-
        registered names are exempt from colliding with themselves on
        reload; two DIFFERENT plugins declaring the same graph-source name
        in the SAME pass must still hard-stop."""
        from hivepilot import plugins as plugins_mod
        from hivepilot.graph import GraphSourceNameCollisionError

        pdir = _write_plugin_dir(tmp_path)
        for stem in ("a_first_dup", "b_second_dup"):
            (pdir / f"{stem}.py").write_text(
                "from hivepilot.graph import GraphData, GraphSourceSpec\n"
                "\n"
                "def _data(ctx):\n"
                "    return GraphData(source='dup-graph', nodes=())\n"
                "\n"
                "def register():\n"
                "    return {'graph_sources': [GraphSourceSpec(name='dup-graph', data=_data)]}\n",
                encoding="utf-8",
            )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        with pytest.raises(GraphSourceNameCollisionError):
            plugins_mod.PluginManager()

        assert graph_module.get_graph_source("dup-graph") is None


# ---------------------------------------------------------------------------
# F1/F2 — hot-reload ownership + teardown (opus review, post-S4 fix): graph
# sources must follow the SAME `_owned_*` / `_commit` teardown model
# runners/notifiers/secrets already have, not a direct, un-owned
# `register_graph_source` call.
# ---------------------------------------------------------------------------


class TestHotReloadOwnership:
    def _write_enableable_graph_plugin(self, pdir, stem: str, source_name: str) -> None:
        (pdir / f"{stem}.py").write_text(
            "from hivepilot.graph import GraphData, GraphSourceSpec\n"
            "\n"
            f"def _data(ctx):\n"
            f"    return GraphData(source={source_name!r}, nodes=())\n"
            "\n"
            "def register():\n"
            "    return {\n"
            "        'graph_sources': [\n"
            f"            GraphSourceSpec(name={source_name!r}, data=_data)\n"
            "        ]\n"
            "    }\n",
            encoding="utf-8",
        )

    def test_f1_disable_and_reload_removes_graph_source(
        self, tmp_path, monkeypatch, isolated_graph_sources
    ) -> None:
        """Fail-OPEN regression guard: enable a plugin's graph source, then
        disable it via `HIVEPILOT_PLUGINS_DISABLED` (what the Mirador web
        toggle persists) and `reload()` — the source must be GONE from both
        `get_graph_source()` and `list_graph_sources()`, not still served."""
        from hivepilot import plugins as plugins_mod

        pdir = _write_plugin_dir(tmp_path)
        self._write_enableable_graph_plugin(pdir, "toggle_graph_plugin", "toggle-source")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()
        assert graph_module.get_graph_source("toggle-source") is not None

        monkeypatch.setattr(
            plugins_mod.settings, "plugins_disabled", ["toggle_graph_plugin"], raising=False
        )
        result = pm.reload()

        assert result.ok is True
        assert graph_module.get_graph_source("toggle-source") is None
        assert "toggle-source" not in [s.name for s in graph_module.list_graph_sources()]

    def test_f2_reload_twice_stays_ok_and_source_stays_reachable(
        self, tmp_path, monkeypatch, isolated_graph_sources
    ) -> None:
        """Global hot-reload regression guard: a STILL-ENABLED plugin's
        module is re-`exec()`d on every `reload()` (fresh `GraphSourceSpec`
        object, same name) — this must never self-collide and abort the
        WHOLE plugin set's reload."""
        from hivepilot import plugins as plugins_mod

        pdir = _write_plugin_dir(tmp_path)
        self._write_enableable_graph_plugin(pdir, "stable_graph_plugin", "stable-source")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()
        assert graph_module.get_graph_source("stable-source") is not None

        result_1 = pm.reload()
        assert result_1.ok is True, result_1.error
        assert graph_module.get_graph_source("stable-source") is not None

        result_2 = pm.reload()
        assert result_2.ok is True, result_2.error
        assert graph_module.get_graph_source("stable-source") is not None


# ---------------------------------------------------------------------------
# AC4 — a plugin source that RAISES at fetch is normalized to an error
# graph (no 500), routed through the SAME `run_graph_fetch` wrapper
# built-ins use.
# ---------------------------------------------------------------------------


class TestPluginSourceRaisingAtFetch:
    def test_raising_plugin_source_normalizes_never_500(
        self, tmp_path, monkeypatch, isolated_graph_sources, tmp_tokens_file, api_client
    ) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = _write_plugin_dir(tmp_path)
        (pdir / "boom_graph.py").write_text(
            "from hivepilot.graph import GraphSourceSpec\n"
            "\n"
            "def _data(ctx):\n"
            "    raise RuntimeError('kaboom')\n"
            "\n"
            "def register():\n"
            "    return {'graph_sources': [GraphSourceSpec(name='boom-graph', data=_data)]}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        plugins_mod.PluginManager()

        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/boom-graph", headers=_auth(raw))

        assert resp.status_code == 200, resp.text
        node = resp.json()["nodes"][0]
        assert node["kind"] == "error"
        assert node["status"] == "error"
        assert node["label"] == "RuntimeError"
        assert "kaboom" not in resp.text


# ---------------------------------------------------------------------------
# AC5 — a bogus/unknown min_role must DENY, never fail-open (S1's
# `_resolve_graph_min_role_rank` returns max-rank+1 for an unrecognized
# role).
# ---------------------------------------------------------------------------


class TestBogusMinRoleFailsClosed:
    def test_bogus_min_role_denies_even_admin(
        self, tmp_path, monkeypatch, isolated_graph_sources, tmp_tokens_file, api_client
    ) -> None:
        from hivepilot import plugins as plugins_mod

        pdir = _write_plugin_dir(tmp_path)
        (pdir / "bogus_role_graph.py").write_text(
            "from hivepilot.graph import GraphData, GraphSourceSpec\n"
            "\n"
            "def _data(ctx):\n"
            "    return GraphData(source='bogus-role', nodes=())\n"
            "\n"
            "def register():\n"
            "    return {\n"
            "        'graph_sources': [\n"
            "            GraphSourceSpec(name='bogus-role', data=_data, min_role='superuser')\n"
            "        ]\n"
            "    }\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        # Registration itself must NOT raise: unlike PanelSpec/SkillSpec,
        # graph sources don't reject an invalid min_role at registration
        # time — S1's `_resolve_graph_min_role_rank` (hivepilot/services/
        # api_service.py) is the fail-closed enforcement point instead.
        pm = plugins_mod.PluginManager()
        assert graph_module.get_graph_source("bogus-role") is not None
        record = next(r for r in pm.loaded if r.name == "bogus_role_graph")
        assert record.contributions.get("graph_sources") == ["bogus-role"]

        raw_admin, _ = add_token("admin")
        raw_read, _ = add_token("read")

        resp_admin = api_client.get("/v1/graph/bogus-role", headers=_auth(raw_admin))
        resp_read = api_client.get("/v1/graph/bogus-role", headers=_auth(raw_read))

        # An unrecognized min_role resolves to max-rank+1 — HIGHER than
        # every real role including admin — so even an admin token is
        # denied. Never fail-open.
        assert resp_admin.status_code == 403
        assert resp_read.status_code == 403


# ---------------------------------------------------------------------------
# AC6 — taxonomy: `hivepilot.services.plugin_index` enumerates graph-source
# contributions per plugin (feeds `plugins list`'s "contributes" column).
# ---------------------------------------------------------------------------


class TestGraphSourceContributionsTaxonomyHelper:
    def test_enumerates_only_plugins_that_contributed_a_graph_source(
        self, tmp_path, monkeypatch, isolated_graph_sources
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.services.plugin_index import graph_source_contributions

        pdir = _write_plugin_dir(tmp_path)
        (pdir / "with_graph.py").write_text(
            "from hivepilot.graph import GraphData, GraphSourceSpec\n"
            "\n"
            "def _data(ctx):\n"
            "    return GraphData(source='tax-graph', nodes=())\n"
            "\n"
            "def register():\n"
            "    return {'graph_sources': [GraphSourceSpec(name='tax-graph', data=_data)]}\n",
            encoding="utf-8",
        )
        (pdir / "without_graph.py").write_text(
            "def register():\n    return {}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

        pm = plugins_mod.PluginManager()
        mapping = graph_source_contributions(pm)

        assert mapping == {"with_graph": ["tax-graph"]}
        assert "without_graph" not in mapping


# ---------------------------------------------------------------------------
# F4 — `hivepilot plugins list`'s rendered "contributes" column includes
# `graph_sources` (closes AC6: the CLI table itself, not just the
# underlying `PluginRecord.contributions` data proven above).
# ---------------------------------------------------------------------------


class TestPluginsListRendersGraphSourceContribution:
    def test_graph_sources_contribution_shown_in_plugins_list_output(
        self, tmp_path, monkeypatch, isolated_graph_sources
    ) -> None:
        from unittest.mock import MagicMock

        from typer.testing import CliRunner

        from hivepilot import plugins as plugins_mod
        from hivepilot.cli import app

        pdir = _write_plugin_dir(tmp_path)
        (pdir / "cli_render_graph.py").write_text(
            "from hivepilot.graph import GraphData, GraphSourceSpec\n"
            "\n"
            "def _data(ctx):\n"
            "    return GraphData(source='cli-render-graph', nodes=())\n"
            "\n"
            "def register():\n"
            "    return {\n"
            "        'graph_sources': [\n"
            "            GraphSourceSpec(name='cli-render-graph', data=_data)\n"
            "        ]\n"
            "    }\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = plugins_mod.PluginManager()

        mock_orch = MagicMock()
        mock_orch.plugins = pm
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "list"])

        assert result.exit_code == 0, result.output
        assert "cli_render_graph" in result.output
        assert "graph_sources: cli-render-graph" in result.output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
