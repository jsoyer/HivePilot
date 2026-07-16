"""Tests for the Mirador panel web API (Sprint 3): `GET /v1/panels` and
`GET /v1/panels/{name}` (plus their unversioned twins).

Mirrors the auth patterns established for `GET /v1/plugins/health` and
`GET /v1/memories` in `tests/test_api_service.py` — see
`TestPluginsHealthEndpoint` / `TestMemoriesEndpoint` there. The security core
of this sprint is the per-panel, DATA-DEPENDENT `min_role` gate on
`GET /v1/panels/{name}`: a panel declares its own `min_role` (default
"read"), so a `read` token must get 403 on a panel declaring
`min_role: "admin"`, while an `admin` token gets 200 for the same panel.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.plugins import PanelSpec, PluginManager
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


def _fake_plugin_manager(panels: dict[str, PanelSpec]) -> PluginManager:
    """Build a real `PluginManager` instance (bypassing `__init__`, exactly
    like `TestPluginsHealthEndpoint.test_raising_check_surfaces_as_error_not_500`
    does for `.health`) with `.panels` seeded directly. `list_panels`/
    `get_panel`/`run_panel_fetch` are then the REAL bound methods —
    end-to-end through the actual panel core (`hivepilot/plugins.py`), not a
    mock of the endpoint's own logic.
    """
    pm = object.__new__(PluginManager)
    pm.panels = panels
    return pm


def _panel_spec(
    name: str,
    title: str,
    fetch: Callable[[], Any],
    min_role: str = "read",
) -> PanelSpec:
    return PanelSpec(name=name, title=title, fetch=fetch, min_role=min_role)


def _patch_orchestrator(monkeypatch, panels: dict[str, PanelSpec]) -> None:
    from hivepilot.services import api_service

    pm = _fake_plugin_manager(panels)
    monkeypatch.setattr(api_service, "_get_orchestrator", lambda: SimpleNamespace(plugins=pm))


# ---------------------------------------------------------------------------
# GET /v1/panels (+ unversioned twin)
# ---------------------------------------------------------------------------


class TestPanelsListEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/panels")
        assert resp.status_code == 401

    def test_allows_read_role_and_returns_seeded_panels(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        _patch_orchestrator(
            monkeypatch,
            {
                "alpha": _panel_spec("alpha", "Alpha Panel", lambda: {"sections": []}, "read"),
                "beta": _panel_spec("beta", "Beta Panel", lambda: {"sections": []}, "admin"),
            },
        )
        raw, _ = add_token("read")
        resp = api_client.get("/v1/panels", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()["panels"]
        assert {"name": "alpha", "title": "Alpha Panel", "min_role": "read"} in data
        assert {"name": "beta", "title": "Beta Panel", "min_role": "admin"} in data

    def test_sorted_by_name_and_defaults_min_role_to_read(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        # No explicit min_role in the raw dict below — exercises the
        # endpoint's own `.get("min_role", "read")` default (mirrors
        # `PluginManager`'s own registration-time default).
        _patch_orchestrator(
            monkeypatch,
            {
                "zeta": {"name": "zeta", "title": "Zeta", "fetch": lambda: {"sections": []}},
                "alpha": {"name": "alpha", "title": "Alpha", "fetch": lambda: {"sections": []}},
            },
        )
        raw, _ = add_token("read")
        resp = api_client.get("/v1/panels", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()["panels"]
        assert [p["name"] for p in data] == ["alpha", "zeta"]
        assert all(p["min_role"] == "read" for p in data)

    def test_unversioned_route_also_registered(self, api_client, tmp_tokens_file, monkeypatch):
        _patch_orchestrator(monkeypatch, {})
        raw, _ = add_token("read")
        resp = api_client.get("/panels", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json() == {"panels": []}


# ---------------------------------------------------------------------------
# GET /v1/panels/{name} (+ unversioned twin) — the per-panel min_role gate.
# ---------------------------------------------------------------------------


class TestPanelFetchEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/panels/anything")
        assert resp.status_code == 401

    def test_unknown_panel_returns_404(self, api_client, tmp_tokens_file, monkeypatch):
        _patch_orchestrator(monkeypatch, {})
        raw, _ = add_token("read")
        resp = api_client.get("/v1/panels/does-not-exist", headers=_auth(raw))
        assert resp.status_code == 404

    def test_read_role_panel_returns_200_for_read_token(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        _patch_orchestrator(
            monkeypatch,
            {
                "alpha": _panel_spec(
                    "alpha",
                    "Alpha",
                    lambda: {"sections": [{"kind": "text", "content": "hello"}]},
                    "read",
                )
            },
        )
        raw, _ = add_token("read")
        resp = api_client.get("/v1/panels/alpha", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json() == {"sections": [{"kind": "text", "content": "hello"}]}

    def test_admin_panel_403_for_read_token_200_for_admin_token(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """The security core of this sprint: a panel declaring
        `min_role: "admin"` must reject a `read`-role token with 403 and
        accept an `admin`-role token with 200 — enforced INSIDE the handler,
        after resolving the panel, since the required role is data-dependent
        (see `get_panel_endpoint`'s docstring in `api_service.py`)."""
        _patch_orchestrator(
            monkeypatch,
            {
                "secure": _panel_spec(
                    "secure",
                    "Secure Panel",
                    lambda: {"sections": [{"kind": "text", "content": "top secret"}]},
                    "admin",
                )
            },
        )

        raw_read, _ = add_token("read")
        resp_read = api_client.get("/v1/panels/secure", headers=_auth(raw_read))
        assert resp_read.status_code == 403

        raw_run, _ = add_token("run")
        resp_run = api_client.get("/v1/panels/secure", headers=_auth(raw_run))
        assert resp_run.status_code == 403

        raw_approve, _ = add_token("approve")
        resp_approve = api_client.get("/v1/panels/secure", headers=_auth(raw_approve))
        assert resp_approve.status_code == 403

        raw_admin, _ = add_token("admin")
        resp_admin = api_client.get("/v1/panels/secure", headers=_auth(raw_admin))
        assert resp_admin.status_code == 200
        assert resp_admin.json()["sections"][0]["content"] == "top secret"

    def test_raising_panel_returns_200_error_panel_never_500_no_secret(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """End-to-end through the REAL `PluginManager.run_panel_fetch`
        never-raise contract (`hivepilot/plugins.py`) — a raising `fetch()`
        must degrade to a 200 error-panel (exception TYPE name only), never
        a 500, and the exception's own message (seeded here with a secret
        marker) must never reach the response body."""
        secret = "sk-panel-secret-should-never-leak"  # noqa: S105 - test fixture value

        def _boom():
            raise RuntimeError(f"leaked {secret}")

        _patch_orchestrator(monkeypatch, {"broken": _panel_spec("broken", "Broken", _boom, "read")})
        raw, _ = add_token("read")
        resp = api_client.get("/v1/panels/broken", headers=_auth(raw))
        assert resp.status_code == 200
        assert secret not in resp.text
        section = resp.json()["sections"][0]
        assert section["kind"] == "stat"
        assert section["status"] == "error"
        assert section["value"] == "RuntimeError"

    def test_malformed_panel_data_returns_200_error_panel(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        _patch_orchestrator(
            monkeypatch,
            {"malformed": _panel_spec("malformed", "Malformed", lambda: {"not": "valid"}, "read")},
        )
        raw, _ = add_token("read")
        resp = api_client.get("/v1/panels/malformed", headers=_auth(raw))
        assert resp.status_code == 200
        section = resp.json()["sections"][0]
        assert section["status"] == "error"

    def test_unversioned_twin_gated_identically(self, api_client, tmp_tokens_file, monkeypatch):
        _patch_orchestrator(
            monkeypatch,
            {"secure": _panel_spec("secure", "Secure", lambda: {"sections": []}, "admin")},
        )
        raw_read, _ = add_token("read")
        resp = api_client.get("/panels/secure", headers=_auth(raw_read))
        assert resp.status_code == 403

        raw_admin, _ = add_token("admin")
        resp = api_client.get("/panels/secure", headers=_auth(raw_admin))
        assert resp.status_code == 200

    def test_unversioned_unknown_panel_404(self, api_client, tmp_tokens_file, monkeypatch):
        _patch_orchestrator(monkeypatch, {})
        raw, _ = add_token("read")
        resp = api_client.get("/panels/nope", headers=_auth(raw))
        assert resp.status_code == 404


class TestPanelFetchEndpointNeverFailsOpen:
    """Regression coverage for the fail-open privilege-escalation gap: an
    unrecognized `min_role` made `token_service.role_rank(min_role)` return
    -1, so `role_rank(caller.role) < role_rank(min_role)` was `0 < -1` —
    ALWAYS False — and the 403 never fired. `hivepilot/plugins.py` now
    refuses to REGISTER such a panel at all (see `tests/test_panels.py`
    `TestPanelInvalidMinRoleRejection`); this class additionally proves
    `get_panel_endpoint`'s own defensive guard denies every caller even if a
    panel with an invalid `min_role` reaches it by some other path than the
    normal `PluginManager()` registration flow (e.g. a spec injected
    directly into `.panels`, bypassing registration entirely — the same
    `_fake_plugin_manager` seeding technique the rest of this file uses).
    """

    @pytest.mark.parametrize(
        "bad_min_role",
        ["Admin", "superuser", "", 123, None, []],
        ids=[
            "typo-Admin",
            "superuser",
            "empty-string",
            "non-string-int",
            "none",
            "non-hashable-list",
        ],
    )
    def test_invalid_min_role_denies_every_role_never_fails_open(
        self, api_client, tmp_tokens_file, monkeypatch, bad_min_role
    ):
        """A panel spec that bypasses registration-time validation (injected
        directly here, since `PluginManager()` itself can no longer produce
        one) must still be denied by the endpoint's own defensive guard —
        for EVERY caller role, including `admin`, since an unenforceable
        `min_role` must never be silently treated as satisfied."""
        _patch_orchestrator(
            monkeypatch,
            {
                "restricted": _panel_spec(
                    "restricted",
                    "Restricted",
                    lambda: {"sections": [{"kind": "text", "content": "top secret"}]},
                    bad_min_role,
                )
            },
        )

        for role in ("read", "run", "approve", "admin"):
            raw, _ = add_token(role)
            resp = api_client.get("/v1/panels/restricted", headers=_auth(raw))
            assert resp.status_code == 403, f"role={role} must be denied, got {resp.status_code}"

    def test_valid_but_high_min_role_still_denies_read_token(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """Sanity companion to the invalid-min_role tests above: the system
        can't be coerced into serving an admin panel to a read token even
        via the normal, valid `min_role: "admin"` path."""
        _patch_orchestrator(
            monkeypatch,
            {
                "admin_only": _panel_spec(
                    "admin_only",
                    "Admin Only",
                    lambda: {"sections": [{"kind": "text", "content": "top secret"}]},
                    "admin",
                )
            },
        )
        raw, _ = add_token("read")
        resp = api_client.get("/v1/panels/admin_only", headers=_auth(raw))
        assert resp.status_code == 403
