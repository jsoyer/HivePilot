"""
Tests for the first-party `bitwarden` secrets-provider plugin
(`plugins/bitwarden.py`).

`bitwarden` is a `secrets` provider (the THIRD plugin provider type, alongside
`runners` / `notifiers`): its `register()` returns
`{"secrets": {"bitwarden": BitwardenBackend()}, "health": {...}}`, loaded into
`SECRETS_MAP` under the same fail-closed trust model builtin backends use, so a
pipeline config can reference a stored value via `${secret:NAME}` where NAME's
spec has `source: bitwarden`.

Unlike infisical/onepassword (Python SDKs), this backend shells out to the
official Bitwarden `bw` CLI — an optional EXTERNAL tool that is NEVER a hivepilot
dependency and is never installed by tests. Both `shutil.which("bw")` and
`subprocess.run` are mocked throughout. When `bw` is absent, `BW_SESSION` is
unset, or the CLI errors, `resolve()` raises a clear error naming ONLY the item
+ provider — NEVER the fetched value and NEVER the BW_SESSION token — so the
`closed` fail-mode aborts the run.

Mirrors `tests/test_infisical.py`'s "load the plugin by file path" mechanism
(the same one `hivepilot.plugins._scan_local_plugins` uses).
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
BITWARDEN_PLUGIN_PATH = REPO_ROOT / "plugins" / "bitwarden.py"

# A fake secret value + fake session token used to prove the plugin NEVER leaks
# either into an error message, a log line, or the health detail. Deliberately
# distinctive so a substring assertion is exact.
_FAKE_VALUE = "s3cr3t-value-SHOULD-NOT-LEAK-abc123"
_FAKE_SESSION = "BW-SESSION-TOKEN-SHOULD-NOT-LEAK-xyz789"


def _load_bitwarden_module() -> ModuleType:
    """Load plugins/bitwarden.py by file path — same mechanism
    `hivepilot.plugins._scan_local_plugins` uses (no dependency on `plugins`
    being importable on sys.path)."""
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_bitwarden_test", BITWARDEN_PLUGIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def bw_module() -> ModuleType:
    return _load_bitwarden_module()


@pytest.fixture(autouse=True)
def _bw_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fully-configured happy-path baseline: `bw` on PATH + BW_SESSION set.
    Individual tests override (e.g. clear the session) to exercise the
    fail-closed paths."""
    monkeypatch.setenv("BW_SESSION", _FAKE_SESSION)


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
    return SecretRef(source="bitwarden", spec=dict(spec))


class TestRegister:
    def test_register_exposes_bitwarden_secrets_backend(self, bw_module: ModuleType) -> None:
        hooks = bw_module.register()
        assert set(hooks) == {"secrets", "health"}
        backends = hooks["secrets"]
        assert set(backends) == {"bitwarden"}
        backend = backends["bitwarden"]
        assert callable(getattr(backend, "resolve", None))
        assert backend.name == "bitwarden"

    def test_register_exposes_health_check(self, bw_module: ModuleType) -> None:
        hooks = bw_module.register()
        assert hooks["health"]["bitwarden"] is bw_module.health

    def test_register_returns_contributions_when_enabled_by_default(
        self, bw_module: ModuleType
    ) -> None:
        assert settings.bitwarden_enabled is True
        hooks = bw_module.register()
        assert set(hooks["secrets"]) == {"bitwarden"}

    def test_register_returns_empty_when_disabled(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "bitwarden_enabled", False, raising=False)
        assert bw_module.register() == {}


class TestHealth:
    def test_error_when_bw_missing(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_absent(bw_module, monkeypatch)
        result = bw_module.health()
        assert result.status == "error"
        assert "bw" in result.detail

    def test_ok_when_bw_present_and_session_set(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(bw_module, monkeypatch)
        result = bw_module.health()
        assert result.status == "ok"

    def test_degraded_when_session_unset(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(bw_module, monkeypatch)
        monkeypatch.delenv("BW_SESSION", raising=False)
        result = bw_module.health()
        assert result.status == "degraded"
        assert "not configured" in result.detail

    def test_health_never_leaks_session_value(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(bw_module, monkeypatch)
        result = bw_module.health()
        assert _FAKE_SESSION not in result.detail

    def test_health_is_keyword_tolerant(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(bw_module, monkeypatch)
        result = bw_module.health(project="anything")
        assert result.status in {"ok", "degraded", "error"}

    def test_health_never_raises_returns_error_type_name(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_binary: str) -> str:
            raise RuntimeError("boom-secret")

        monkeypatch.setattr(bw_module.shutil, "which", _boom)
        result = bw_module.health()
        assert result.status == "error"
        assert result.detail == "RuntimeError"
        assert "boom-secret" not in result.detail


class TestResolveHappyPath:
    def test_resolve_returns_login_password(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(bw_module, monkeypatch)
        run = MagicMock(return_value=_completed(_login_payload(_FAKE_VALUE)))
        monkeypatch.setattr(bw_module.subprocess, "run", run)

        backend = bw_module.BitwardenBackend()
        value = backend.resolve(_ref(item="prod-db"), settings)
        assert value == _FAKE_VALUE

    def test_resolve_falls_back_to_notes_for_secure_note(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(bw_module, monkeypatch)
        run = MagicMock(return_value=_completed(_note_payload(_FAKE_VALUE)))
        monkeypatch.setattr(bw_module.subprocess, "run", run)

        backend = bw_module.BitwardenBackend()
        value = backend.resolve(_ref(item="api-note"), settings)
        assert value == _FAKE_VALUE

    def test_resolve_invokes_bw_get_item_with_session(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(bw_module, monkeypatch)
        run = MagicMock(return_value=_completed(_login_payload(_FAKE_VALUE)))
        monkeypatch.setattr(bw_module.subprocess, "run", run)

        backend = bw_module.BitwardenBackend()
        backend.resolve(_ref(item="prod-db"), settings)

        args, _kwargs = run.call_args
        cmd = args[0]
        assert cmd[:4] == ["bw", "get", "item", "prod-db"]
        assert "--response" in cmd
        # Session is passed explicitly (never relies on an ambient unlocked vault).
        assert "--session" in cmd
        assert _FAKE_SESSION in cmd


class TestResolveFailClosed:
    def test_missing_item_in_spec_raises(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(bw_module, monkeypatch)
        backend = bw_module.BitwardenBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(), settings)
        msg = str(excinfo.value)
        assert "bitwarden" in msg
        assert "item" in msg

    def test_bw_not_on_path_raises_without_leaking(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_absent(bw_module, monkeypatch)
        backend = bw_module.BitwardenBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(item="prod-db"), settings)
        msg = str(excinfo.value)
        assert "bitwarden" in msg
        assert "bw" in msg
        assert _FAKE_VALUE not in msg
        assert _FAKE_SESSION not in msg

    def test_session_unset_raises_naming_env_var_only(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(bw_module, monkeypatch)
        monkeypatch.delenv("BW_SESSION", raising=False)
        backend = bw_module.BitwardenBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(item="prod-db"), settings)
        msg = str(excinfo.value)
        assert "BW_SESSION" in msg
        assert _FAKE_VALUE not in msg
        assert _FAKE_SESSION not in msg

    def test_cli_error_raises_without_leaking_value_or_session(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A CalledProcessError embeds the full command (INCLUDING the
        --session token) in its str(). The re-raised error must contain
        NEITHER the session token NOR the secret value — proving redaction."""
        _which_present(bw_module, monkeypatch)
        err = subprocess.CalledProcessError(
            1,
            ["bw", "get", "item", "prod-db", "--session", _FAKE_SESSION],
            output=f"leaking {_FAKE_VALUE}",
        )
        monkeypatch.setattr(bw_module.subprocess, "run", MagicMock(side_effect=err))

        backend = bw_module.BitwardenBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(item="prod-db"), settings)

        msg = str(excinfo.value)
        assert "bitwarden" in msg
        assert "prod-db" in msg
        assert _FAKE_VALUE not in msg
        assert _FAKE_SESSION not in msg
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True

    def test_no_value_returned_raises(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        _which_present(bw_module, monkeypatch)
        payload = json.dumps({"success": True, "data": {"object": "item", "login": None}})
        monkeypatch.setattr(
            bw_module.subprocess, "run", MagicMock(return_value=_completed(payload))
        )
        backend = bw_module.BitwardenBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(item="prod-db"), settings)
        assert "bitwarden" in str(excinfo.value)
        assert "prod-db" in str(excinfo.value)

    def test_empty_password_raises_fail_closed(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(bw_module, monkeypatch)
        monkeypatch.setattr(
            bw_module.subprocess, "run", MagicMock(return_value=_completed(_login_payload("")))
        )
        backend = bw_module.BitwardenBackend()
        with pytest.raises(RuntimeError):
            backend.resolve(_ref(item="prod-db"), settings)


class TestMaskingNoLeak:
    """Dedicated masking proof: neither the fake secret value nor the fake
    BW_SESSION token may appear in ANY sink — the raised exception string, the
    logger call arguments, or the health() detail."""

    def test_no_value_or_session_in_any_sink(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _which_present(bw_module, monkeypatch)
        mock_logger = MagicMock()
        monkeypatch.setattr(bw_module, "logger", mock_logger)

        err = subprocess.CalledProcessError(
            1,
            ["bw", "get", "item", "prod-db", "--session", _FAKE_SESSION],
            output=f"boom {_FAKE_VALUE}",
        )
        monkeypatch.setattr(bw_module.subprocess, "run", MagicMock(side_effect=err))

        backend = bw_module.BitwardenBackend()

        # Sink 1: the raised exception string.
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(item="prod-db"), settings)
        exc_str = str(excinfo.value)
        assert _FAKE_VALUE not in exc_str
        assert _FAKE_SESSION not in exc_str

        # Sink 2: every logger call argument.
        assert mock_logger.warning.called
        log_blob = repr(mock_logger.warning.call_args_list)
        assert _FAKE_VALUE not in log_blob
        assert _FAKE_SESSION not in log_blob

        # Sink 3: the health() detail.
        health_detail = bw_module.health().detail
        assert _FAKE_VALUE not in health_detail
        assert _FAKE_SESSION not in health_detail


class TestPluginManagerRegistersBitwarden:
    def test_plugin_manager_registers_bitwarden_into_secrets_map(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        pm = plugins_mod.PluginManager()

        assert "bitwarden" in SECRETS_MAP
        assert callable(getattr(SECRETS_MAP["bitwarden"], "resolve", None))
        assert any(r.source == "local-file" and r.name == "bitwarden" for r in pm.loaded)

    def test_plugin_manager_skips_bitwarden_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "bitwarden_enabled", False, raising=False)
        plugins_mod.PluginManager()
        assert "bitwarden" not in SECRETS_MAP

    def test_name_collision_with_bitwarden_aborts(self, bw_module: ModuleType) -> None:
        SecretsRegistry.register("bitwarden", bw_module.BitwardenBackend())

        class _Other:
            def resolve(self, ref: SecretRef, s: object) -> str:
                return "other"

        with pytest.raises(SecretsBackendCollisionError):
            SecretsRegistry.register("bitwarden", _Other())
