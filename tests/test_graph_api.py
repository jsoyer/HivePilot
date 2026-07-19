"""Tests for the Mirador Graph View web API (Sprint 1): `GET /v1/graph/sources`,
`GET /v1/graph/{source}`, `GET /v1/graph/{source}/node/{node_id}` (plus their
unversioned twins). Mirrors `tests/test_panels_api.py`'s auth / DATA-DEPENDENT
`min_role` gate patterns.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot import graph as graph_module
from hivepilot.services.token_service import add_token


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


@pytest.fixture()
def isolated_graph_sources(monkeypatch):
    """A fresh COPY of the module-global graph-source registry, seeded with
    whatever built-ins are already registered (the real `plugins` source),
    so a test can register additional fake sources without leaking them
    into other test modules — `monkeypatch` restores the original dict
    object at teardown."""
    monkeypatch.setattr(graph_module, "_GRAPH_SOURCES", dict(graph_module._GRAPH_SOURCES))
    return graph_module._GRAPH_SOURCES


def _register_fake_source(
    name: str,
    data: Callable[[graph_module.GraphContext], Any],
    *,
    min_role: str = "read",
    node_detail: Callable[[graph_module.GraphContext, str], Any] | None = None,
) -> graph_module.GraphSourceSpec:
    spec = graph_module.GraphSourceSpec(
        name=name, data=data, node_detail=node_detail, min_role=min_role
    )
    graph_module.register_graph_source(spec)
    return spec


# ---------------------------------------------------------------------------
# GET /v1/graph/sources (+ unversioned twin)
# ---------------------------------------------------------------------------


class TestGraphSourcesListEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/graph/sources")
        assert resp.status_code == 401

    def test_lists_builtin_plugins_source(
        self, api_client, tmp_tokens_file, isolated_graph_sources
    ):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/sources", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()["sources"]
        plugins_entry = next(s for s in data if s["name"] == "plugins")
        assert set(plugins_entry.keys()) == {"name", "title", "min_role", "params"}
        assert plugins_entry["min_role"] == "read"

    def test_unversioned_route_also_registered(
        self, api_client, tmp_tokens_file, isolated_graph_sources
    ):
        raw, _ = add_token("read")
        resp = api_client.get("/graph/sources", headers=_auth(raw))
        assert resp.status_code == 200
        assert "plugins" in [s["name"] for s in resp.json()["sources"]]


# ---------------------------------------------------------------------------
# GET /v1/graph/{source} (+ unversioned twin)
# ---------------------------------------------------------------------------


class TestGraphDataEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/graph/plugins")
        assert resp.status_code == 401

    def test_unknown_source_404(self, api_client, tmp_tokens_file, isolated_graph_sources):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/nope", headers=_auth(raw))
        assert resp.status_code == 404

    def test_plugins_source_returns_well_formed_graph_data(
        self, api_client, tmp_tokens_file, isolated_graph_sources
    ):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/plugins", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"source", "nodes", "edges", "layout_hint"}
        assert data["source"] == "plugins"
        assert len(data["nodes"]) >= 1
        kinds = {n["kind"] for n in data["nodes"]}
        assert "role" in kinds
        node = data["nodes"][0]
        assert set(node.keys()) == {"id", "label", "kind", "status", "group", "badges", "meta"}

    def test_raising_source_normalizes_to_error_graph_never_500(
        self, api_client, tmp_tokens_file, isolated_graph_sources
    ):
        secret = "sk-graph-secret-should-never-leak"  # noqa: S105 - test fixture value

        def _boom(ctx: graph_module.GraphContext) -> graph_module.GraphData:
            raise RuntimeError(f"leaked {secret}")

        _register_fake_source("broken", _boom)
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/broken", headers=_auth(raw))
        assert resp.status_code == 200
        assert secret not in resp.text
        node = resp.json()["nodes"][0]
        assert node["kind"] == "error"
        assert node["status"] == "error"
        assert node["label"] == "RuntimeError"

    def test_malformed_source_returns_200_error_graph(
        self, api_client, tmp_tokens_file, isolated_graph_sources
    ):
        _register_fake_source("malformed", lambda ctx: {"not": "valid"})
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/malformed", headers=_auth(raw))
        assert resp.status_code == 200
        node = resp.json()["nodes"][0]
        assert node["status"] == "error"

    def test_min_role_enforced_after_resolution(
        self, api_client, tmp_tokens_file, isolated_graph_sources
    ):
        _register_fake_source(
            "secure",
            lambda ctx: graph_module.GraphData(source="secure", nodes=(), edges=()),
            min_role="admin",
        )
        raw_read, _ = add_token("read")
        resp_read = api_client.get("/v1/graph/secure", headers=_auth(raw_read))
        assert resp_read.status_code == 403

        raw_admin, _ = add_token("admin")
        resp_admin = api_client.get("/v1/graph/secure", headers=_auth(raw_admin))
        assert resp_admin.status_code == 200

    def test_unknown_min_role_denies_every_caller_never_fails_open(
        self, api_client, tmp_tokens_file, isolated_graph_sources
    ):
        _register_fake_source(
            "restricted",
            lambda ctx: graph_module.GraphData(source="restricted", nodes=(), edges=()),
            min_role="superuser",
        )
        for role in ("read", "run", "approve", "admin"):
            raw, _ = add_token(role)
            resp = api_client.get("/v1/graph/restricted", headers=_auth(raw))
            assert resp.status_code == 403, f"role={role} must be denied, got {resp.status_code}"

    def test_tenant_context_reflects_callers_own_tenant_only(
        self, api_client, tmp_tokens_file, isolated_graph_sources
    ):
        """`GraphContext` is built from the CALLER's own resolved token —
        never client-supplied. Registers a fake source that echoes
        `ctx.tenant` straight into a node's label, then proves two
        different-tenant `read` tokens each see only their OWN tenant
        reflected back."""

        def _echo_tenant(ctx: graph_module.GraphContext) -> graph_module.GraphData:
            return graph_module.GraphData(
                source="tenant_echo",
                nodes=(graph_module.GraphNode(id="t", label=ctx.tenant, kind="tenant"),),
                edges=(),
            )

        _register_fake_source("tenant_echo", _echo_tenant)

        raw_acme, _ = add_token("read", tenant="acme")
        raw_globex, _ = add_token("read", tenant="globex")

        resp_acme = api_client.get("/v1/graph/tenant_echo", headers=_auth(raw_acme))
        resp_globex = api_client.get("/v1/graph/tenant_echo", headers=_auth(raw_globex))

        assert resp_acme.status_code == 200
        assert resp_globex.status_code == 200
        assert resp_acme.json()["nodes"][0]["label"] == "acme"
        assert resp_globex.json()["nodes"][0]["label"] == "globex"

    def test_unversioned_twin_gated_identically(
        self, api_client, tmp_tokens_file, isolated_graph_sources
    ):
        _register_fake_source(
            "secure2",
            lambda ctx: graph_module.GraphData(source="secure2", nodes=(), edges=()),
            min_role="admin",
        )
        raw_read, _ = add_token("read")
        resp = api_client.get("/graph/secure2", headers=_auth(raw_read))
        assert resp.status_code == 403

        raw_admin, _ = add_token("admin")
        resp = api_client.get("/graph/secure2", headers=_auth(raw_admin))
        assert resp.status_code == 200

    def test_unversioned_unknown_source_404(
        self, api_client, tmp_tokens_file, isolated_graph_sources
    ):
        raw, _ = add_token("read")
        resp = api_client.get("/graph/nope", headers=_auth(raw))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /v1/graph/{source}/node/{node_id} (+ unversioned twin)
# ---------------------------------------------------------------------------


class TestGraphNodeDetailEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/graph/plugins/node/role:developer")
        assert resp.status_code == 401

    def test_unknown_source_404(self, api_client, tmp_tokens_file, isolated_graph_sources):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/nope/node/anything", headers=_auth(raw))
        assert resp.status_code == 404

    def test_unknown_node_404(self, api_client, tmp_tokens_file, isolated_graph_sources):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/plugins/node/does-not-exist", headers=_auth(raw))
        assert resp.status_code == 404

    def test_source_with_no_node_detail_callable_404(
        self, api_client, tmp_tokens_file, isolated_graph_sources
    ):
        _register_fake_source(
            "no_detail", lambda ctx: graph_module.GraphData(source="no_detail", nodes=(), edges=())
        )
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/no_detail/node/anything", headers=_auth(raw))
        assert resp.status_code == 404

    def test_known_node_returns_detail(self, api_client, tmp_tokens_file, isolated_graph_sources):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/plugins/node/role:developer", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"title", "tags", "sections"}
        assert data["title"] == "Developer"

    def test_raising_node_detail_returns_200_error_detail_never_500(
        self, api_client, tmp_tokens_file, isolated_graph_sources
    ):
        secret = "sk-node-detail-secret-should-never-leak"  # noqa: S105 - test fixture value

        def _boom_detail(ctx: graph_module.GraphContext, node_id: str) -> graph_module.GraphDetail:
            raise RuntimeError(f"leaked {secret}")

        _register_fake_source(
            "broken_detail",
            lambda ctx: graph_module.GraphData(source="broken_detail", nodes=(), edges=()),
            node_detail=_boom_detail,
        )
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/broken_detail/node/anything", headers=_auth(raw))
        assert resp.status_code == 200
        assert secret not in resp.text
        assert resp.json()["tags"] == ["error"]

    def test_min_role_enforced_on_node_detail(
        self, api_client, tmp_tokens_file, isolated_graph_sources
    ):
        _register_fake_source(
            "secure3",
            lambda ctx: graph_module.GraphData(source="secure3", nodes=(), edges=()),
            node_detail=lambda ctx, node_id: graph_module.GraphDetail(title="x"),
            min_role="admin",
        )
        raw_read, _ = add_token("read")
        resp = api_client.get("/v1/graph/secure3/node/x", headers=_auth(raw_read))
        assert resp.status_code == 403

        raw_admin, _ = add_token("admin")
        resp = api_client.get("/v1/graph/secure3/node/x", headers=_auth(raw_admin))
        assert resp.status_code == 200

    def test_unversioned_twin_gated_identically(
        self, api_client, tmp_tokens_file, isolated_graph_sources
    ):
        raw, _ = add_token("read")
        resp = api_client.get("/graph/plugins/node/role:developer", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["title"] == "Developer"

    def test_unversioned_unknown_node_404(
        self, api_client, tmp_tokens_file, isolated_graph_sources
    ):
        raw, _ = add_token("read")
        resp = api_client.get("/graph/plugins/node/does-not-exist", headers=_auth(raw))
        assert resp.status_code == 404
