"""
Tests for the first-party `vaultwarden` secrets-provider plugin
(`plugins/vaultwarden.py`).

`vaultwarden` is the self-hosted-server sibling of `bitwarden`: same `secrets`
provider type, same fail-closed trust model, same official `bw` CLI — but it
targets a self-hosted, Bitwarden-compatible server configured via
`settings.vaultwarden_server_url` (`bw config server <url>`). Its `register()`
returns `{"secrets": {"vaultwarden": VaultwardenBackend()}, "health": {...}}`.

Like `bitwarden`, the `bw` CLI is an optional EXTERNAL tool that is NEVER a
hivepilot dependency and never installed by tests; both `shutil.which("bw")`
and `subprocess.run` are mocked. When `bw` is absent, `BW_SESSION` is unset,
`vaultwarden_server_url` is missing, or the CLI errors, `resolve()` raises a
clear error naming ONLY the setting/env-var / item + provider — NEVER the
fetched value, NEVER the BW_SESSION token, NEVER the server URL as a leaked
secret — so the `closed` fail-mode aborts the run.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from hivepilot.config import settings
from hivepilot.registry import (
    SECRETS_MAP,
    SecretRef,
    SecretsBackendCollisionError,
    SecretsRegistry,
)

REPO_ROOT = Path(__file__).parent.parent
VAULTWARDEN_PLUGIN_PATH = REPO_ROOT / "plugins" / "vaultwarden.py"

_FAKE_VALUE = "s3cr3t-value-SHOULD-NOT-LEAK-abc123"
_FAKE_SESSION = "BW-SESSION-TOKEN-SHOULD-NOT-LEAK-xyz789"
_SERVER_URL = "https://vault.example.com"


def _load_vaultwarden_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_vaultwarden_test", VAULTWARDEN_PLUGIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def vw_module() -> ModuleType:
    return _load_vaultwarden_module()


@pytest.fixture(autouse=True)
def _vw_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fully-configured baseline: BW_SESSION set + server URL configured.
    Individual tests override to exercise fail-closed paths."""
    monkeypatch.setenv("BW_SESSION", _FAKE_SESSION)
    monkeypatch.setattr(settings, "vaultwarden_server_url", _SERVER_URL, raising=False)


def _which_present(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module.shutil, "which", lambda _binary: "/usr/bin/bw")


def _which_absent(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module.shutil, "which", lambda _binary: None)


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["bw"], returncode=0, stdout=stdout, stderr="")


def _login_payload(password: str) -> str:
    import json

    return json.dumps(
        {"success": True, "data": {"object": "item", "login": {"password": password}}}
    )


def _note_payload(notes: str) -> str:
    import json

    return json.dumps({"success": True, "data": {"object": "item", "login": None, "notes": notes}})


def _ref(**spec: object) -> SecretRef:
    return SecretRef(source="vaultwarden", spec=dict(spec))


class TestRegister:
    def test_register_exposes_vaultwarden_secrets_backend(self, vw_module: ModuleType) -> None:
        hooks = vw_module.register()
        assert set(hooks) == {"secrets", "health"}
        backends = hooks["secrets"]
        assert set(backends) == {"vaultwarden"}
        backend = backends["vaultwarden"]
        assert callable(getattr(backend, "resolve", None))
        assert backend.name == "vaultwarden"

    def test_register_exposes_health_check(self, vw_module: ModuleType) -> None:
        hooks = vw_module.register()
        assert hooks["health"]["vaultwarden"] is vw_module.health

    def test_register_returns_contributions_when_enabled_by_default(
        self, vw_module: ModuleType
    ) -> None:
        assert settings.vaultwarden_enabled is True
        hooks = vw_module.register()
        assert set(hooks["secrets"]) == {"vaultwarden"}

    def test_register_returns_empty_when_disabled(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "vaultwarden_enabled", False, raising=False)
        assert vw_module.register() == {}


class TestHealth:
    def test_error_when_bw_missing(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_absent(vw_module, monkeypatch)
        result = vw_module.health()
        assert result.status == "error"
        assert "bw" in result.detail

    def test_ok_when_bw_session_and_server_present(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(vw_module, monkeypatch)
        result = vw_module.health()
        assert result.status == "ok"

    def test_degraded_when_session_unset(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(vw_module, monkeypatch)
        monkeypatch.delenv("BW_SESSION", raising=False)
        result = vw_module.health()
        assert result.status == "degraded"
        assert "not configured" in result.detail

    def test_degraded_when_server_url_missing(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(vw_module, monkeypatch)
        monkeypatch.setattr(settings, "vaultwarden_server_url", None, raising=False)
        result = vw_module.health()
        assert result.status == "degraded"
        assert "not configured" in result.detail

    def test_health_never_leaks_session_value(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(vw_module, monkeypatch)
        result = vw_module.health()
        assert _FAKE_SESSION not in result.detail

    def test_health_is_keyword_tolerant(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(vw_module, monkeypatch)
        result = vw_module.health(project="anything")
        assert result.status in {"ok", "degraded", "error"}

    def test_health_never_raises_returns_error_type_name(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_binary: str) -> str:
            raise RuntimeError("boom-secret")

        monkeypatch.setattr(vw_module.shutil, "which", _boom)
        result = vw_module.health()
        assert result.status == "error"
        assert result.detail == "RuntimeError"
        assert "boom-secret" not in result.detail


class TestResolveHappyPath:
    def test_resolve_returns_login_password(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(vw_module, monkeypatch)
        run = MagicMock(return_value=_completed(_login_payload(_FAKE_VALUE)))
        monkeypatch.setattr(vw_module.subprocess, "run", run)

        backend = vw_module.VaultwardenBackend()
        value = backend.resolve(_ref(item="prod-db"), settings)
        assert value == _FAKE_VALUE

    def test_resolve_falls_back_to_notes(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(vw_module, monkeypatch)
        run = MagicMock(return_value=_completed(_note_payload(_FAKE_VALUE)))
        monkeypatch.setattr(vw_module.subprocess, "run", run)

        backend = vw_module.VaultwardenBackend()
        value = backend.resolve(_ref(item="api-note"), settings)
        assert value == _FAKE_VALUE

    def test_resolve_configures_server_then_gets_item_with_session(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(vw_module, monkeypatch)
        run = MagicMock(return_value=_completed(_login_payload(_FAKE_VALUE)))
        monkeypatch.setattr(vw_module.subprocess, "run", run)

        backend = vw_module.VaultwardenBackend()
        backend.resolve(_ref(item="prod-db"), settings)

        # First call points bw at the self-hosted server; a later call fetches
        # the item with an explicit session.
        all_cmds = [call.args[0] for call in run.call_args_list]
        assert ["bw", "config", "server", _SERVER_URL] in all_cmds
        get_cmd = next(c for c in all_cmds if c[:3] == ["bw", "get", "item"])
        assert get_cmd[3] == "prod-db"
        assert "--response" in get_cmd
        assert "--session" in get_cmd
        assert _FAKE_SESSION in get_cmd


class TestResolveFailClosed:
    def test_missing_item_in_spec_raises(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(vw_module, monkeypatch)
        backend = vw_module.VaultwardenBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(), settings)
        assert "vaultwarden" in str(excinfo.value)
        assert "item" in str(excinfo.value)

    def test_bw_not_on_path_raises_without_leaking(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_absent(vw_module, monkeypatch)
        backend = vw_module.VaultwardenBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(item="prod-db"), settings)
        msg = str(excinfo.value)
        assert "vaultwarden" in msg
        assert "bw" in msg
        assert _FAKE_VALUE not in msg
        assert _FAKE_SESSION not in msg

    def test_session_unset_raises_naming_env_var_only(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(vw_module, monkeypatch)
        monkeypatch.delenv("BW_SESSION", raising=False)
        backend = vw_module.VaultwardenBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(item="prod-db"), settings)
        msg = str(excinfo.value)
        assert "BW_SESSION" in msg
        assert _FAKE_SESSION not in msg

    def test_missing_server_url_raises_naming_setting_only(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The distinguishing vaultwarden fail-closed trigger: no configured
        server URL. Error names ONLY the setting, never a value/session."""
        _which_present(vw_module, monkeypatch)
        monkeypatch.setattr(settings, "vaultwarden_server_url", None, raising=False)
        backend = vw_module.VaultwardenBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(item="prod-db"), settings)
        msg = str(excinfo.value)
        assert "vaultwarden_server_url" in msg
        assert _FAKE_VALUE not in msg
        assert _FAKE_SESSION not in msg

    def test_cli_error_raises_without_leaking_value_or_session(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(vw_module, monkeypatch)
        err = subprocess.CalledProcessError(
            1,
            ["bw", "get", "item", "prod-db", "--session", _FAKE_SESSION],
            output=f"leaking {_FAKE_VALUE}",
        )
        monkeypatch.setattr(vw_module.subprocess, "run", MagicMock(side_effect=err))

        backend = vw_module.VaultwardenBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(item="prod-db"), settings)

        msg = str(excinfo.value)
        assert "vaultwarden" in msg
        assert "prod-db" in msg
        assert _FAKE_VALUE not in msg
        assert _FAKE_SESSION not in msg
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True

    def test_no_value_returned_raises(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        _which_present(vw_module, monkeypatch)
        payload = json.dumps({"success": True, "data": {"object": "item", "login": None}})
        monkeypatch.setattr(
            vw_module.subprocess, "run", MagicMock(return_value=_completed(payload))
        )
        backend = vw_module.VaultwardenBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(item="prod-db"), settings)
        assert "vaultwarden" in str(excinfo.value)


class TestMaskingNoLeak:
    def test_no_value_or_session_in_any_sink(
        self, vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(vw_module, monkeypatch)
        mock_logger = MagicMock()
        monkeypatch.setattr(vw_module, "logger", mock_logger)

        err = subprocess.CalledProcessError(
            1,
            ["bw", "get", "item", "prod-db", "--session", _FAKE_SESSION],
            output=f"boom {_FAKE_VALUE}",
        )
        monkeypatch.setattr(vw_module.subprocess, "run", MagicMock(side_effect=err))

        backend = vw_module.VaultwardenBackend()

        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(item="prod-db"), settings)
        exc_str = str(excinfo.value)
        assert _FAKE_VALUE not in exc_str
        assert _FAKE_SESSION not in exc_str

        assert mock_logger.warning.called
        log_blob = repr(mock_logger.warning.call_args_list)
        assert _FAKE_VALUE not in log_blob
        assert _FAKE_SESSION not in log_blob

        health_detail = vw_module.health().detail
        assert _FAKE_VALUE not in health_detail
        assert _FAKE_SESSION not in health_detail


class TestPluginManagerRegistersVaultwarden:
    def test_plugin_manager_registers_vaultwarden_into_secrets_map(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        pm = plugins_mod.PluginManager()

        assert "vaultwarden" in SECRETS_MAP
        assert callable(getattr(SECRETS_MAP["vaultwarden"], "resolve", None))
        assert any(r.source == "local-file" and r.name == "vaultwarden" for r in pm.loaded)

    def test_plugin_manager_skips_vaultwarden_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "vaultwarden_enabled", False, raising=False)
        plugins_mod.PluginManager()
        assert "vaultwarden" not in SECRETS_MAP

    def test_name_collision_with_vaultwarden_aborts(self, vw_module: ModuleType) -> None:
        SecretsRegistry.register("vaultwarden", vw_module.VaultwardenBackend())

        class _Other:
            def resolve(self, ref: SecretRef, s: object) -> str:
                return "other"

        with pytest.raises(SecretsBackendCollisionError):
            SecretsRegistry.register("vaultwarden", _Other())
