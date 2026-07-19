"""Tests for `POST /v1/plugins/{name}/toggle` (Mirador actionable dashboard
PRD, Sprint 5).

Mirrors the auth/fixture patterns established in `tests/test_cancel_run.py`
(`tmp_tokens_file`/`api_client`/`_auth`) and `tests/test_api_service.py`'s
`TestPluginsHealthEndpoint` (monkeypatching `api_service._get_orchestrator`
to control `plugins.check_all()`).

Covers:
- role < admin (read, run, approve) -> 403, admin -> allowed (fail-closed
  gate, checked before any lookup or persist).
- unknown plugin name -> 404 AND `persist_plugins_disabled` is NEVER called
  (spy with call_count == 0) -- proves "no .env write on unknown".
- a known ENABLED plugin (present in `check_all()`) flips to disabled and
  persists the correctly-flipped sorted list.
- `persist_plugins_disabled` itself, exercised for real against a tmp `.env`
  path (unit-level, no monkeypatch needed) -- proves the actual file write
  contains `HIVEPILOT_PLUGINS_DISABLED` with the plugin name.
- the UNION allowlist: a plugin present in `settings.plugins_disabled` but
  NOT in `check_all()` (i.e. already disabled, hence not registered/loaded)
  is still accepted and flips back to enabled.
- toggling twice returns to the original disabled set.

Never asserts on/surfaces raw exception or `capture()` text.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services.token_service import add_token

# ---------------------------------------------------------------------------
# Shared fixtures -- mirrors tests/test_cancel_run.py / test_api_service.py.
# ---------------------------------------------------------------------------


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
def no_disabled(monkeypatch):
    """Reset `settings.plugins_disabled` to `[]` for the duration of a test
    -- process-global state that a previous test (or a real `.env`) could
    otherwise leak into this one."""
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "plugins_disabled", [])
    return settings


def _fake_orchestrator(plugin_names: list[str]) -> SimpleNamespace:
    fake_plugins = SimpleNamespace(
        check_all=lambda: {name: SimpleNamespace(status="ok", detail="") for name in plugin_names}
    )
    return SimpleNamespace(plugins=fake_plugins)


def _patch_orchestrator(monkeypatch, plugin_names: list[str]) -> None:
    from hivepilot.services import api_service

    monkeypatch.setattr(api_service, "_get_orchestrator", lambda: _fake_orchestrator(plugin_names))


def _patch_persist_spy(monkeypatch) -> MagicMock:
    from hivepilot.services import api_service

    spy = MagicMock()
    monkeypatch.setattr(api_service, "persist_plugins_disabled", spy)
    return spy


# ---------------------------------------------------------------------------
# Auth matrix -- fail-closed
# ---------------------------------------------------------------------------


class TestToggleEndpointAuth:
    def test_requires_auth(self, api_client):
        resp = api_client.post("/v1/plugins/rtk/toggle")
        assert resp.status_code == 401

    def test_read_role_forbidden(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.post("/v1/plugins/rtk/toggle", headers=_auth(raw))
        assert resp.status_code == 403

    def test_run_role_forbidden(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        resp = api_client.post("/v1/plugins/rtk/toggle", headers=_auth(raw))
        assert resp.status_code == 403

    def test_approve_role_forbidden(self, api_client, tmp_tokens_file):
        raw, _ = add_token("approve")
        resp = api_client.post("/v1/plugins/rtk/toggle", headers=_auth(raw))
        assert resp.status_code == 403

    def test_admin_role_allowed(self, api_client, tmp_tokens_file, no_disabled, monkeypatch):
        _patch_orchestrator(monkeypatch, ["rtk"])
        _patch_persist_spy(monkeypatch)
        raw, _ = add_token("admin")
        resp = api_client.post("/v1/plugins/rtk/toggle", headers=_auth(raw))
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Unknown plugin name -- 404, fail-closed (persist never called)
# ---------------------------------------------------------------------------


class TestUnknownPluginFailsClosed:
    def test_unknown_name_404(self, api_client, tmp_tokens_file, no_disabled, monkeypatch):
        _patch_orchestrator(monkeypatch, ["rtk"])
        spy = _patch_persist_spy(monkeypatch)
        raw, _ = add_token("admin")
        resp = api_client.post("/v1/plugins/does-not-exist/toggle", headers=_auth(raw))
        assert resp.status_code == 404
        assert spy.call_count == 0

    def test_unknown_name_detail_is_generic(
        self, api_client, tmp_tokens_file, no_disabled, monkeypatch
    ):
        _patch_orchestrator(monkeypatch, [])
        _patch_persist_spy(monkeypatch)
        raw, _ = add_token("admin")
        resp = api_client.post("/v1/plugins/does-not-exist/toggle", headers=_auth(raw))
        assert resp.status_code == 404
        assert resp.json()["detail"] == "unknown plugin"


# ---------------------------------------------------------------------------
# Known ENABLED plugin -> flips to disabled, persists the flipped list
# ---------------------------------------------------------------------------


class TestDisableKnownEnabledPlugin:
    def test_flips_to_disabled_and_persists_sorted_list(
        self, api_client, tmp_tokens_file, no_disabled, monkeypatch
    ):
        _patch_orchestrator(monkeypatch, ["mem0", "rtk"])
        spy = _patch_persist_spy(monkeypatch)
        raw, _ = add_token("admin")

        resp = api_client.post("/v1/plugins/rtk/toggle", headers=_auth(raw))

        assert resp.status_code == 200
        body = resp.json()
        assert body == {"name": "rtk", "disabled": True, "restart_required": True}
        spy.assert_called_once_with(["rtk"])

        from hivepilot.config import settings

        assert settings.plugins_disabled == ["rtk"]

    def test_second_plugin_appends_sorted(
        self, api_client, tmp_tokens_file, no_disabled, monkeypatch
    ):
        from hivepilot.config import settings

        settings.plugins_disabled = ["zeta"]
        _patch_orchestrator(monkeypatch, ["alpha", "zeta"])
        spy = _patch_persist_spy(monkeypatch)
        raw, _ = add_token("admin")

        resp = api_client.post("/v1/plugins/alpha/toggle", headers=_auth(raw))

        assert resp.status_code == 200
        assert resp.json()["disabled"] is True
        spy.assert_called_once_with(["alpha", "zeta"])


# ---------------------------------------------------------------------------
# Re-enable path -- UNION allowlist requirement: a plugin already disabled
# (hence absent from check_all()) must still be toggleable.
# ---------------------------------------------------------------------------


class TestReenableAlreadyDisabledPlugin:
    def test_disabled_but_unregistered_plugin_flips_to_enabled(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "plugins_disabled", ["rtk"])
        # `rtk` is disabled -> NOT registered -> absent from check_all().
        _patch_orchestrator(monkeypatch, ["mem0"])
        spy = _patch_persist_spy(monkeypatch)
        raw, _ = add_token("admin")

        resp = api_client.post("/v1/plugins/rtk/toggle", headers=_auth(raw))

        assert resp.status_code == 200
        body = resp.json()
        assert body == {"name": "rtk", "disabled": False, "restart_required": True}
        spy.assert_called_once_with([])
        assert settings.plugins_disabled == []

    def test_union_still_404s_a_name_in_neither_set(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "plugins_disabled", ["rtk"])
        _patch_orchestrator(monkeypatch, ["mem0"])
        spy = _patch_persist_spy(monkeypatch)
        raw, _ = add_token("admin")

        resp = api_client.post("/v1/plugins/ghost/toggle", headers=_auth(raw))

        assert resp.status_code == 404
        assert spy.call_count == 0


# ---------------------------------------------------------------------------
# Toggle twice -> back to the original disabled set
# ---------------------------------------------------------------------------


class TestToggleTwiceIsIdempotentRoundtrip:
    def test_toggle_twice_returns_to_original_set(
        self, api_client, tmp_tokens_file, no_disabled, monkeypatch
    ):
        _patch_orchestrator(monkeypatch, ["rtk"])
        _patch_persist_spy(monkeypatch)
        raw, _ = add_token("admin")

        first = api_client.post("/v1/plugins/rtk/toggle", headers=_auth(raw))
        assert first.status_code == 200
        assert first.json()["disabled"] is True

        from hivepilot.config import settings

        assert settings.plugins_disabled == ["rtk"]

        # `rtk` is now disabled, hence would no longer be registered in a
        # real orchestrator -- re-point check_all() to reflect that before
        # toggling back, exactly like the re-enable test above.
        _patch_orchestrator(monkeypatch, [])

        second = api_client.post("/v1/plugins/rtk/toggle", headers=_auth(raw))
        assert second.status_code == 200
        assert second.json()["disabled"] is False
        assert settings.plugins_disabled == []


# ---------------------------------------------------------------------------
# persist_plugins_disabled -- real filesystem write (unit-level, no mocking)
# ---------------------------------------------------------------------------


class TestPersistPluginsDisabledRealWrite:
    def test_writes_env_key_with_plugin_name(self, tmp_path):
        from hivepilot.ui.plugin_manager import persist_plugins_disabled

        env_path = tmp_path / ".env"
        result_path = persist_plugins_disabled(["rtk"], env_path=env_path)

        assert result_path == env_path
        content = env_path.read_text(encoding="utf-8")
        assert "HIVEPILOT_PLUGINS_DISABLED" in content
        assert "rtk" in content

    def test_preserves_other_lines_and_upserts(self, tmp_path):
        from hivepilot.ui.plugin_manager import persist_plugins_disabled

        env_path = tmp_path / ".env"
        env_path.write_text("SOME_OTHER_KEY=1\n", encoding="utf-8")

        persist_plugins_disabled(["rtk", "mem0"], env_path=env_path)

        content = env_path.read_text(encoding="utf-8")
        assert "SOME_OTHER_KEY=1" in content
        assert "HIVEPILOT_PLUGINS_DISABLED" in content
        assert "mem0" in content and "rtk" in content


# ---------------------------------------------------------------------------
# Never leak raw exception/capture text
# ---------------------------------------------------------------------------


class TestNoRawTextLeak:
    def test_success_response_never_carries_free_text_detail_field(
        self, api_client, tmp_tokens_file, no_disabled, monkeypatch
    ):
        _patch_orchestrator(monkeypatch, ["rtk"])
        _patch_persist_spy(monkeypatch)
        raw, _ = add_token("admin")

        resp = api_client.post("/v1/plugins/rtk/toggle", headers=_auth(raw))

        assert resp.status_code == 200
        assert set(resp.json().keys()) == {"name", "disabled", "restart_required"}
