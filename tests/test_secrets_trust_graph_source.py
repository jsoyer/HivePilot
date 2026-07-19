"""Tests for `plugins/secrets_trust_graph_source.py` (Mirador GraphSources
plugin sprint) -- the plugin-contributed `secrets-trust` graph source over
the secrets registry (`hivepilot.registry.SECRETS_MAP`) and the declared
secret NAME catalog (`ProjectConfig.secrets`, `hivepilot/models.py`).

Loaded by file path (mirrors `tests/test_example_graph_source.py` /
`tests/test_drift_graph_source.py`), never `import
plugins.secrets_trust_graph_source` -- see those files' own docstrings for
the `sys.modules` isolation rationale.

The single most important test class here is `TestNeverLeaksASecretValue`:
it seeds a secret whose `source="env"` spec resolves (if actually resolved)
to a KNOWN, distinctive value, and proves that value never appears anywhere
in the produced `GraphData`/`GraphDetail` -- and that no resolver is ever
called at all.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from hivepilot import graph as graph_module
from hivepilot.config import settings
from hivepilot.models import ProjectConfig, ProjectsFile

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_PATH = _REPO_ROOT / "plugins" / "secrets_trust_graph_source.py"

_spec = importlib.util.spec_from_file_location(
    "hivepilot_test_secrets_trust_graph_source_plugin", _PLUGIN_PATH
)
assert _spec and _spec.loader
secrets_trust_graph_source = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(secrets_trust_graph_source)

_MARKER_SECRET_VALUE = "sk-live-super-secret-marker-4f9c2a"


@pytest.fixture()
def isolated_graph_sources(monkeypatch):
    monkeypatch.setattr(graph_module, "_GRAPH_SOURCES", dict(graph_module._GRAPH_SOURCES))
    return graph_module._GRAPH_SOURCES


def _projects_file_with_secret(
    *, project_name: str = "demo-project", secret_name: str = "api-key", source: str = "env"
) -> ProjectsFile:
    project = ProjectConfig(
        path=Path("/tmp/demo-project"),
        secrets={secret_name: {"source": source, "key": "DEMO_API_KEY"}},
    )
    return ProjectsFile(projects={project_name: project})


def _patch_load_projects(monkeypatch, projects_file: ProjectsFile) -> None:
    from hivepilot.services import project_service

    monkeypatch.setattr(project_service, "load_projects", lambda *a, **kw: projects_file)


# ---------------------------------------------------------------------------
# Opt-in gating
# ---------------------------------------------------------------------------


class TestOptInGating:
    def test_disabled_by_default_contributes_nothing(self) -> None:
        assert settings.secrets_trust_graph_source_enabled is False
        assert secrets_trust_graph_source.register() == {}

    def test_enabled_contributes_secrets_trust_source(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "secrets_trust_graph_source_enabled", True, raising=False)

        hooks = secrets_trust_graph_source.register()
        assert [s.name for s in hooks["graph_sources"]] == ["secrets-trust"]
        spec = hooks["graph_sources"][0]
        assert spec.min_role == "read"
        assert spec.node_detail is not None


class TestRealPluginManagerScan:
    def test_registers_secrets_trust_when_enabled(
        self, monkeypatch, isolated_graph_sources
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(settings, "secrets_trust_graph_source_enabled", True, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "base_dir", _REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert graph_module.get_graph_source("secrets-trust") is not None
        record = next(r for r in pm.loaded if r.name == "secrets_trust_graph_source")
        assert record.contributions.get("graph_sources") == ["secrets-trust"]

    def test_absent_when_disabled_by_default(self, monkeypatch, isolated_graph_sources) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", _REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert graph_module.get_graph_source("secrets-trust") is None
        record = next(r for r in pm.loaded if r.name == "secrets_trust_graph_source")
        assert record.contributions == {}


# ---------------------------------------------------------------------------
# _build_graph -- provider/secret nodes
# ---------------------------------------------------------------------------


class TestBuildGraph:
    def test_no_declared_secrets_still_shows_registered_providers(self, monkeypatch) -> None:
        _patch_load_projects(monkeypatch, ProjectsFile(projects={}))
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        data = secrets_trust_graph_source._build_graph(ctx)

        provider_labels = {n.label for n in data.nodes if n.kind == "provider"}
        assert {"env", "file", "vault", "sops"} <= provider_labels
        assert not [n for n in data.nodes if n.kind == "secret"]

    def test_declared_secret_produces_secret_and_provider_nodes(self, monkeypatch) -> None:
        _patch_load_projects(
            monkeypatch, _projects_file_with_secret(secret_name="api-key", source="env")
        )
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        data = secrets_trust_graph_source._build_graph(ctx)

        secret_nodes = [n for n in data.nodes if n.kind == "secret"]
        assert len(secret_nodes) == 1
        assert secret_nodes[0].label == "api-key"
        assert secret_nodes[0].meta["project"] == "demo-project"
        assert secret_nodes[0].meta["source"] == "env"

        provider_node = next(n for n in data.nodes if n.kind == "provider" and n.label == "env")
        assert any(
            e.source == secret_nodes[0].id and e.target == provider_node.id for e in data.edges
        )

    def test_unregistered_provider_surfaces_as_unknown_not_dropped(self, monkeypatch) -> None:
        _patch_load_projects(
            monkeypatch,
            _projects_file_with_secret(secret_name="mystery", source="not-a-real-backend"),
        )
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        data = secrets_trust_graph_source._build_graph(ctx)

        provider_node = next(
            n for n in data.nodes if n.kind == "provider" and n.label == "not-a-real-backend"
        )
        assert provider_node.status == "unknown"
        secret_node = next(n for n in data.nodes if n.kind == "secret")
        assert any(e.source == secret_node.id and e.target == provider_node.id for e in data.edges)

    def test_raising_source_normalizes_to_error_graph_never_500(self, monkeypatch) -> None:
        from hivepilot.services import project_service

        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(project_service, "load_projects", _boom)
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        spec = graph_module.GraphSourceSpec(
            name="secrets-trust-test",
            data=secrets_trust_graph_source._build_graph,
            node_detail=secrets_trust_graph_source._node_detail,
        )
        data = graph_module.run_graph_fetch(spec, ctx)
        assert data.nodes[0].kind == "error"
        assert data.nodes[0].status == "error"


class TestNodeDetail:
    def test_provider_node_detail(self) -> None:
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        detail = secrets_trust_graph_source._node_detail(ctx, "provider:env")
        assert detail is not None
        assert detail.title == "env"
        assert "provider" in detail.tags

    def test_secret_node_detail(self, monkeypatch) -> None:
        _patch_load_projects(
            monkeypatch, _projects_file_with_secret(secret_name="api-key", source="env")
        )
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        detail = secrets_trust_graph_source._node_detail(ctx, "secret:demo-project:api-key")
        assert detail is not None
        assert "api-key" in detail.title
        assert "demo-project" in detail.title

    def test_unknown_node_prefix_returns_none(self) -> None:
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        assert secrets_trust_graph_source._node_detail(ctx, "nope:1") is None

    def test_secret_node_detail_unknown_project_returns_none(self) -> None:
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        assert secrets_trust_graph_source._node_detail(ctx, "secret:no-such-project:x") is None


# ---------------------------------------------------------------------------
# THE key security test -- a secret value must never leak, and the resolver
# must never even be called.
# ---------------------------------------------------------------------------


class TestNeverLeaksASecretValue:
    def test_marker_value_never_appears_in_graph_data(self, monkeypatch) -> None:
        import os

        monkeypatch.setenv("DEMO_API_KEY", _MARKER_SECRET_VALUE)
        _patch_load_projects(
            monkeypatch, _projects_file_with_secret(secret_name="api-key", source="env")
        )
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        data = secrets_trust_graph_source._build_graph(ctx)

        assert os.environ["DEMO_API_KEY"] == _MARKER_SECRET_VALUE  # sanity: really set
        assert _MARKER_SECRET_VALUE not in repr(data)
        for node in data.nodes:
            assert _MARKER_SECRET_VALUE not in repr(node)
            for value in node.meta.values():
                assert _MARKER_SECRET_VALUE != value

    def test_marker_value_never_appears_in_node_detail(self, monkeypatch) -> None:
        monkeypatch.setenv("DEMO_API_KEY", _MARKER_SECRET_VALUE)
        _patch_load_projects(
            monkeypatch, _projects_file_with_secret(secret_name="api-key", source="env")
        )
        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        detail = secrets_trust_graph_source._node_detail(ctx, "secret:demo-project:api-key")
        assert detail is not None
        assert _MARKER_SECRET_VALUE not in repr(detail)

    def test_env_backend_resolve_is_never_called(self, monkeypatch) -> None:
        from hivepilot.registry import SECRETS_MAP

        monkeypatch.setenv("DEMO_API_KEY", _MARKER_SECRET_VALUE)
        _patch_load_projects(
            monkeypatch, _projects_file_with_secret(secret_name="api-key", source="env")
        )

        def _boom(*args, **kwargs):
            raise AssertionError(
                "secrets_trust_graph_source must never call a secrets backend's resolve()"
            )

        monkeypatch.setattr(SECRETS_MAP["env"], "resolve", _boom)

        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        secrets_trust_graph_source._build_graph(ctx)
        secrets_trust_graph_source._node_detail(ctx, "secret:demo-project:api-key")

    def test_secret_resolver_resolve_is_never_called(self, monkeypatch) -> None:
        from hivepilot.services.secrets_service import secret_resolver

        _patch_load_projects(
            monkeypatch, _projects_file_with_secret(secret_name="api-key", source="env")
        )

        def _boom(*args, **kwargs):
            raise AssertionError(
                "secrets_trust_graph_source must never call secret_resolver.resolve()"
            )

        monkeypatch.setattr(secret_resolver, "resolve", _boom)

        ctx = graph_module.GraphContext(tenant="default", role="read", params={})
        secrets_trust_graph_source._build_graph(ctx)
        secrets_trust_graph_source._node_detail(ctx, "secret:demo-project:api-key")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
