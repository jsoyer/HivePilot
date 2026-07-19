"""No-secret-leak tests for the Mirador Graph View graph endpoints (Sprint
1): a secret plugin's graph node/detail must expose NAME + status ONLY,
never a resolved secret VALUE — mirrors the Phase 19 secrets-masking
discipline already applied elsewhere in this repo (plugin health/detail,
panel sections). Verified END TO END through the real `/v1/graph/*` API,
like `tests/test_panels_api.py`'s
`test_raising_panel_returns_200_error_panel_never_500_no_secret`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services.token_service import add_token

_SECRET_VALUE = "sk-super-secret-should-never-leak-anywhere"  # noqa: S105 - test fixture value

_PLUGIN_SOURCE = f'''
class _LeakySecretsBackend:
    def resolve(self, ref, settings):
        return "{_SECRET_VALUE}"


def register():
    return {{
        "secrets": {{"gs_leaky_secret": _LeakySecretsBackend()}},
    }}
'''


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
def seeded_secret_plugin_manager(tmp_path, monkeypatch):
    from hivepilot import plugins as plugins_mod

    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "gs_leaky_secret_plugin.py").write_text(_PLUGIN_SOURCE, encoding="utf-8")

    monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
    return plugins_mod.PluginManager()


@pytest.fixture()
def patched_orchestrator(monkeypatch, seeded_secret_plugin_manager):
    from hivepilot.services import api_service

    monkeypatch.setattr(
        api_service,
        "_get_orchestrator",
        lambda: SimpleNamespace(plugins=seeded_secret_plugin_manager),
    )
    return seeded_secret_plugin_manager


class TestNoSecretLeakInGraphData:
    def test_secret_value_never_in_plugins_graph_data(
        self, api_client, tmp_tokens_file, patched_orchestrator
    ):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/plugins", headers=_auth(raw))
        assert resp.status_code == 200
        assert _SECRET_VALUE not in resp.text

    def test_secret_value_never_in_secret_node_detail(
        self, api_client, tmp_tokens_file, patched_orchestrator
    ):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/plugins/node/secret:gs_leaky_secret", headers=_auth(raw))
        assert resp.status_code == 200
        assert _SECRET_VALUE not in resp.text
        data = resp.json()
        assert data["title"] == "gs_leaky_secret"
        assert "secret" in data["tags"]

    def test_secret_value_never_in_plugin_node_detail(
        self, api_client, tmp_tokens_file, patched_orchestrator
    ):
        raw, _ = add_token("read")
        resp = api_client.get(
            "/v1/graph/plugins/node/plugin:gs_leaky_secret_plugin", headers=_auth(raw)
        )
        assert resp.status_code == 200
        assert _SECRET_VALUE not in resp.text

    def test_secret_value_never_leaked_via_raising_node_detail_error_path(
        self, api_client, tmp_tokens_file, patched_orchestrator
    ):
        """Belt-and-suspenders: even the never-raise error path
        (`run_graph_node_detail`) must not somehow echo the secret value if
        a future change made `node_detail` call `.resolve()` and raise."""
        raw, _ = add_token("read")
        resp = api_client.get("/v1/graph/plugins/node/secret:does-not-exist", headers=_auth(raw))
        # unknown secret name -> generic "disabled" detail, never a 500/leak
        assert resp.status_code == 200
        assert _SECRET_VALUE not in resp.text
