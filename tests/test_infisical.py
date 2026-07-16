"""
Tests for the first-party `infisical` secrets-provider plugin
(`plugins/infisical.py`).

Infisical (https://infisical.com) is an open-source, self-hostable config /
value store. This plugin dogfoods the THIRD plugin provider type (`secrets`,
alongside `runners` / `notifiers`): its `register()` returns
`{"secrets": {"infisical": InfisicalBackend()}}`, loaded into
`SECRETS_MAP` under the same fail-closed trust model builtin backends use
(`hivepilot/services/secrets_service.py`), so a pipeline config can reference a
stored value via `${secret:NAME}` where NAME's spec has `source: infisical`.

The `infisicalsdk` package is NOT a hivepilot dependency and is deliberately
never installed by this plugin (worktree agents don't install deps) — it is
mocked throughout this module. `plugins/infisical.py` lazily imports
`from infisical_sdk import InfisicalSDKClient` and, when the SDK is absent OR
required config is missing OR the client errors, raises a clear error naming
ONLY the secret key + the provider name — NEVER the fetched value — so the
`closed` fail-mode aborts the run.

Mirrors `tests/test_mem0.py`'s "load the plugin by file path" mechanism (the
same one `hivepilot.plugins._scan_local_plugins` uses), so these tests don't
depend on `plugins` being an importable package on sys.path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.registry import (
    SECRETS_MAP,
    SecretRef,
    SecretsBackendCollisionError,
    SecretsRegistry,
)

REPO_ROOT = Path(__file__).parent.parent
INFISICAL_PLUGIN_PATH = REPO_ROOT / "plugins" / "infisical.py"

# A fake secret value used to prove the plugin NEVER leaks a fetched value into
# an error message. Deliberately distinctive so a substring assertion is exact.
_FAKE_VALUE = "s3cr3t-value-SHOULD-NOT-LEAK-abc123"


def _load_infisical_module() -> ModuleType:
    """Load plugins/infisical.py by file path — same mechanism
    `hivepilot.plugins._scan_local_plugins` uses (no dependency on `plugins`
    being importable on sys.path)."""
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_infisical_test", INFISICAL_PLUGIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def infisical_module() -> ModuleType:
    return _load_infisical_module()


@pytest.fixture(autouse=True)
def _infisical_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fully-configured happy-path Settings baseline. Individual tests
    override (e.g. clear the token) to exercise the fail-closed paths."""
    monkeypatch.setattr(settings, "infisical_url", "https://infisical.example.com", raising=False)
    monkeypatch.setattr(settings, "infisical_token", "tok-123", raising=False)
    monkeypatch.setattr(settings, "infisical_workspace_id", "ws-abc", raising=False)
    monkeypatch.setattr(settings, "infisical_environment", "dev", raising=False)


def _mock_client_returning(value: str) -> MagicMock:
    """A mock InfisicalSDKClient instance whose
    `.secrets.get_secret_by_name(...)` returns an object with `.secretValue`."""
    client = MagicMock()
    secret_obj = MagicMock()
    secret_obj.secretValue = value
    client.secrets.get_secret_by_name.return_value = secret_obj
    return client


def _ref(**spec: object) -> SecretRef:
    return SecretRef(source="infisical", spec=dict(spec))


class TestRegister:
    def test_register_exposes_infisical_secrets_backend(self, infisical_module: ModuleType) -> None:
        hooks = infisical_module.register()
        assert set(hooks) == {"secrets", "health"}
        backends = hooks["secrets"]
        assert set(backends) == {"infisical"}
        backend = backends["infisical"]
        # Structurally satisfies the SecretsBackend protocol.
        assert callable(getattr(backend, "resolve", None))
        assert backend.name == "infisical"

    def test_register_exposes_health_check(self, infisical_module: ModuleType) -> None:
        hooks = infisical_module.register()
        assert "health" in hooks
        assert "infisical" in hooks["health"]
        assert hooks["health"]["infisical"] is infisical_module.health

    def test_register_returns_contributions_when_enabled_by_default(
        self, infisical_module: ModuleType
    ) -> None:
        # infisical_enabled defaults True (opt-out) — unchanged behavior. This
        # is a pure register()-level enable gate; no secret value is involved.
        assert settings.infisical_enabled is True
        hooks = infisical_module.register()
        assert set(hooks) == {"secrets", "health"}
        assert set(hooks["secrets"]) == {"infisical"}

    def test_register_returns_empty_when_disabled(
        self, infisical_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Disabling contributes nothing — no secrets backend, no health. Still
        # a register()-level gate only; no secret is read or handled.
        monkeypatch.setattr(settings, "infisical_enabled", False, raising=False)
        assert infisical_module.register() == {}


class TestHealth:
    """Plugin-health surface: `health()` reports CONFIGURATION status only —
    never the token/endpoint value or a resolved secret (Phase 19 discipline).
    `error` if the SDK is missing, `degraded` if unconfigured, `ok` when both
    the SDK is importable and the required config is present."""

    def test_error_when_sdk_missing(self, infisical_module: ModuleType) -> None:
        with patch.object(infisical_module, "InfisicalSDKClient", None):
            result = infisical_module.health()
        assert result.status == "error"
        assert "infisicalsdk" in result.detail

    def test_ok_when_sdk_and_config_present(self, infisical_module: ModuleType) -> None:
        # Autouse `_infisical_settings` provides a fully-configured baseline.
        with patch.object(infisical_module, "InfisicalSDKClient", MagicMock()):
            result = infisical_module.health()
        assert result.status == "ok"

    def test_degraded_when_unconfigured(
        self, infisical_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "infisical_token", None, raising=False)
        with patch.object(infisical_module, "InfisicalSDKClient", MagicMock()):
            result = infisical_module.health()
        assert result.status == "degraded"
        assert "not configured" in result.detail

    def test_health_never_leaks_token_value(
        self, infisical_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The token value must NEVER appear in the health detail — only
        presence booleans / mode names (Phase 19 no-secret discipline)."""
        monkeypatch.setattr(settings, "infisical_token", _FAKE_VALUE, raising=False)
        with patch.object(infisical_module, "InfisicalSDKClient", MagicMock()):
            result = infisical_module.health()
        assert _FAKE_VALUE not in result.detail
        assert "https://infisical.example.com" not in result.detail

    def test_health_is_keyword_tolerant(self, infisical_module: ModuleType) -> None:
        with patch.object(infisical_module, "InfisicalSDKClient", MagicMock()):
            result = infisical_module.health(project="anything")
        assert result.status in {"ok", "degraded", "error"}

    def test_health_never_raises_returns_error_type_name(
        self, infisical_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force an internal failure while reading config to prove the guard
        # returns the exception TYPE name, never a message.
        class _Boom:
            @property
            def infisical_token(self) -> str:
                raise RuntimeError("boom-secret")

        monkeypatch.setattr("hivepilot.config.settings", _Boom())
        with patch.object(infisical_module, "InfisicalSDKClient", MagicMock()):
            result = infisical_module.health()
        assert result.status == "error"
        assert result.detail == "RuntimeError"
        assert "boom-secret" not in result.detail


class TestResolveHappyPath:
    def test_resolve_returns_fetched_value(self, infisical_module: ModuleType) -> None:
        backend = infisical_module.InfisicalBackend()
        client_cls = MagicMock(return_value=_mock_client_returning(_FAKE_VALUE))

        with patch.object(infisical_module, "InfisicalSDKClient", client_cls):
            value = backend.resolve(_ref(key="DATABASE_URL"), settings)

        assert value == _FAKE_VALUE

    def test_resolve_calls_client_with_key_workspace_env_from_settings(
        self, infisical_module: ModuleType
    ) -> None:
        backend = infisical_module.InfisicalBackend()
        client = _mock_client_returning(_FAKE_VALUE)
        client_cls = MagicMock(return_value=client)

        with patch.object(infisical_module, "InfisicalSDKClient", client_cls):
            backend.resolve(_ref(key="DATABASE_URL"), settings)

        # Client built with the self-host host + token from Settings.
        _, build_kwargs = client_cls.call_args
        assert build_kwargs["token"] == "tok-123"
        assert build_kwargs["host"] == "https://infisical.example.com"

        # Fetch keyed by the ref's key + workspace/env from Settings.
        _, fetch_kwargs = client.secrets.get_secret_by_name.call_args
        assert fetch_kwargs["secret_name"] == "DATABASE_URL"
        assert fetch_kwargs["project_id"] == "ws-abc"
        assert fetch_kwargs["environment_slug"] == "dev"
        assert fetch_kwargs["secret_path"] == "/"

    def test_ref_spec_overrides_environment_path_and_workspace(
        self, infisical_module: ModuleType
    ) -> None:
        backend = infisical_module.InfisicalBackend()
        client = _mock_client_returning(_FAKE_VALUE)
        client_cls = MagicMock(return_value=client)

        with patch.object(infisical_module, "InfisicalSDKClient", client_cls):
            backend.resolve(
                _ref(
                    key="API_KEY",
                    environment="prod",
                    path="/svc/api",
                    workspace_id="ws-override",
                ),
                settings,
            )

        _, fetch_kwargs = client.secrets.get_secret_by_name.call_args
        assert fetch_kwargs["environment_slug"] == "prod"
        assert fetch_kwargs["secret_path"] == "/svc/api"
        assert fetch_kwargs["project_id"] == "ws-override"

    def test_no_host_when_infisical_url_unset(
        self, infisical_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset self-host URL -> SDK constructed without `host` (hosted default)."""
        monkeypatch.setattr(settings, "infisical_url", None, raising=False)
        backend = infisical_module.InfisicalBackend()
        client_cls = MagicMock(return_value=_mock_client_returning(_FAKE_VALUE))

        with patch.object(infisical_module, "InfisicalSDKClient", client_cls):
            backend.resolve(_ref(key="DATABASE_URL"), settings)

        _, build_kwargs = client_cls.call_args
        assert "host" not in build_kwargs

    def test_extracts_snake_case_secret_value_attribute(self, infisical_module: ModuleType) -> None:
        """Graceful degradation on SDK signature drift: a `secret_value`
        attribute (instead of `secretValue`) is still read."""
        backend = infisical_module.InfisicalBackend()
        client = MagicMock()
        secret_obj = MagicMock(spec=["secret_value"])
        secret_obj.secret_value = _FAKE_VALUE
        client.secrets.get_secret_by_name.return_value = secret_obj
        client_cls = MagicMock(return_value=client)

        with patch.object(infisical_module, "InfisicalSDKClient", client_cls):
            value = backend.resolve(_ref(key="DATABASE_URL"), settings)

        assert value == _FAKE_VALUE


class TestResolveFailClosed:
    """Every failure path raises (so `closed` fail-mode aborts) and the error
    text names ONLY the secret key + provider — never the fetched value."""

    def test_missing_key_in_spec_raises(self, infisical_module: ModuleType) -> None:
        backend = infisical_module.InfisicalBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(), settings)
        assert "infisical" in str(excinfo.value)
        assert "key" in str(excinfo.value)

    def test_missing_token_raises_naming_key_and_provider_not_value(
        self, infisical_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "infisical_token", None, raising=False)
        backend = infisical_module.InfisicalBackend()
        client_cls = MagicMock(return_value=_mock_client_returning(_FAKE_VALUE))

        with patch.object(infisical_module, "InfisicalSDKClient", client_cls):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(key="DATABASE_URL"), settings)

        msg = str(excinfo.value)
        assert "DATABASE_URL" in msg
        assert "infisical" in msg
        assert _FAKE_VALUE not in msg
        # No fetch was attempted at all — config gate short-circuits first.
        assert not client_cls.return_value.secrets.get_secret_by_name.called

    def test_sdk_not_installed_raises_naming_key_and_provider(
        self, infisical_module: ModuleType
    ) -> None:
        backend = infisical_module.InfisicalBackend()
        with patch.object(infisical_module, "InfisicalSDKClient", None):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(key="DATABASE_URL"), settings)

        msg = str(excinfo.value)
        assert "DATABASE_URL" in msg
        assert "infisical" in msg

    def test_client_error_raises_without_leaking_value(self, infisical_module: ModuleType) -> None:
        """If the SDK itself raises — and even if the exception message
        embeds the secret value — the re-raised error must NOT propagate that
        value (name + provider only). Also proves the leaked exception is
        fully severed (`from None`): a naive caller walking
        `__cause__`/`__context__` cannot resurface the original, value-bearing
        exception either."""
        backend = infisical_module.InfisicalBackend()
        client = MagicMock()
        client.secrets.get_secret_by_name.side_effect = RuntimeError(
            f"upstream boom leaking {_FAKE_VALUE}"
        )
        client_cls = MagicMock(return_value=client)

        with patch.object(infisical_module, "InfisicalSDKClient", client_cls):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(key="DATABASE_URL"), settings)

        msg = str(excinfo.value)
        assert "DATABASE_URL" in msg
        assert "infisical" in msg
        assert _FAKE_VALUE not in msg
        # `raise ... from None` sets __cause__ to None and __suppress_context__
        # to True — a caller inspecting the exception chain (not just the
        # message string) can't resurface the original value-bearing error.
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True

    def test_no_value_returned_raises_without_leaking(self, infisical_module: ModuleType) -> None:
        backend = infisical_module.InfisicalBackend()
        client = MagicMock()
        secret_obj = MagicMock(spec=[])  # neither secretValue nor secret_value
        client.secrets.get_secret_by_name.return_value = secret_obj
        client_cls = MagicMock(return_value=client)

        with patch.object(infisical_module, "InfisicalSDKClient", client_cls):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(key="DATABASE_URL"), settings)

        msg = str(excinfo.value)
        assert "DATABASE_URL" in msg
        assert "infisical" in msg

    def test_empty_string_value_raises_fail_closed_not_returned(
        self, infisical_module: ModuleType
    ) -> None:
        """An empty `.secretValue` is a `str` (passes an `is None` check) but
        is never a legitimate secret — must raise (fail-closed), mirroring
        `EnvSecretsBackend`'s `if not value: raise`, NOT be silently returned
        as a resolved empty secret."""
        backend = infisical_module.InfisicalBackend()
        client_cls = MagicMock(return_value=_mock_client_returning(""))

        with patch.object(infisical_module, "InfisicalSDKClient", client_cls):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(key="DATABASE_URL"), settings)

        msg = str(excinfo.value)
        assert "DATABASE_URL" in msg
        assert "infisical" in msg

    def test_client_construction_error_raises_without_leaking_token_or_value(
        self, infisical_module: ModuleType
    ) -> None:
        """Modern SDK clients often authenticate at construction time. If
        constructing the client itself raises with a message embedding the
        token AND a value, the re-raised error must contain NEITHER — proving
        client construction sits INSIDE the same redaction boundary as the
        fetch call, not before it."""
        backend = infisical_module.InfisicalBackend()
        client_cls = MagicMock(
            side_effect=RuntimeError(f"auth failed for token=tok-123 value={_FAKE_VALUE}")
        )

        with patch.object(infisical_module, "InfisicalSDKClient", client_cls):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(key="DATABASE_URL"), settings)

        msg = str(excinfo.value)
        assert "DATABASE_URL" in msg
        assert "infisical" in msg
        assert "tok-123" not in msg
        assert _FAKE_VALUE not in msg
        assert excinfo.value.__cause__ is None


class TestPluginManagerRegistersInfisical:
    def test_plugin_manager_registers_infisical_into_secrets_map(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert "infisical" in SECRETS_MAP
        assert callable(getattr(SECRETS_MAP["infisical"], "resolve", None))
        assert any(r.source == "local-file" and r.name == "infisical" for r in pm.loaded)

    def test_plugin_manager_skips_infisical_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "infisical_enabled", False, raising=False)

        plugins_mod.PluginManager()

        # register() early-returned {} → no secrets backend registered.
        assert "infisical" not in SECRETS_MAP

    def test_name_collision_with_infisical_aborts(self, infisical_module: ModuleType) -> None:
        """A second backend registering under `infisical` is rejected by the
        fail-closed trust model (SecretsBackendCollisionError)."""
        SecretsRegistry.register("infisical", infisical_module.InfisicalBackend())

        class _Other:
            def resolve(self, ref: SecretRef, s: object) -> str:
                return "other"

        with pytest.raises(SecretsBackendCollisionError):
            SecretsRegistry.register("infisical", _Other())


class TestPluginsListShowsInfisical:
    def test_plugins_list_shows_infisical_as_plugin_backend(
        self, infisical_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from hivepilot.cli import app

        SecretsRegistry.register("infisical", infisical_module.InfisicalBackend())

        mock_orch = MagicMock()
        mock_orch.plugins.loaded = []
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)

        result = CliRunner().invoke(app, ["plugins", "list"])

        assert result.exit_code == 0, result.output
        assert "infisical" in result.output
