"""
Tests for the first-party `onepassword` secrets-provider plugin
(`plugins/onepassword.py`).

This plugin dogfoods the `secrets` plugin provider type (alongside
`runners` / `notifiers`): its `register()` returns
`{"secrets": {"onepassword": OnePasswordBackend()}}`, loaded into
`SECRETS_MAP` under the same fail-closed trust model builtin backends use
(`hivepilot/services/secrets_service.py`), so a pipeline config can reference a
stored value via `${secret:NAME}` where NAME's spec has `source: onepassword`.

The `onepasswordconnectsdk` package is NOT a hivepilot dependency and is
deliberately never installed by this plugin (worktree agents don't install
deps) — it is mocked throughout this module. `plugins/onepassword.py` lazily
imports `from onepasswordconnectsdk.client import new_client` and, when the SDK
is absent OR required config is missing OR the client errors, raises a clear
error naming ONLY the reference identity (`op://vault/item/field`) + the
provider name (`onepassword`) — NEVER the token or fetched value — so the
`closed` fail-mode aborts the run.

Mirrors `tests/test_infisical.py`'s "load the plugin by file path" mechanism
(the same one `hivepilot.plugins._scan_local_plugins` uses), so these tests
don't depend on `plugins` being an importable package on sys.path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Iterator
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
OP_PLUGIN_PATH = REPO_ROOT / "plugins" / "onepassword.py"

# A fake secret value used to prove the plugin NEVER leaks a fetched value into
# an error message. Deliberately distinctive so a substring assertion is exact.
_FAKE_VALUE = "s3cr3t-value-SHOULD-NOT-LEAK-abc123"
# A fake token used to prove the plugin NEVER leaks a credential into an error.
_FAKE_TOKEN = "tok-SHOULD-NOT-LEAK-xyz789"


def _load_onepassword_module() -> ModuleType:
    """Load plugins/onepassword.py by file path — same mechanism
    `hivepilot.plugins._scan_local_plugins` uses (no dependency on `plugins`
    being importable on sys.path)."""
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_onepassword_test", OP_PLUGIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def op_module() -> ModuleType:
    return _load_onepassword_module()


@pytest.fixture(autouse=True)
def _op_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A fully-configured Connect happy-path Settings baseline. Individual
    tests override (e.g. clear the token, switch to service-account) to exercise
    the fail-closed / selection paths."""
    monkeypatch.setattr(
        settings, "op_connect_host", "https://op-connect.example.com", raising=False
    )
    monkeypatch.setattr(settings, "op_connect_token", _FAKE_TOKEN, raising=False)
    monkeypatch.setattr(settings, "op_service_account_token", None, raising=False)
    yield


def _mock_client_returning(value: str) -> MagicMock:
    """A mock Connect client whose `.get_item(item, vault)` returns an Item-like
    object with a `fields` list holding a matching field."""
    client = MagicMock()
    field = MagicMock()
    field.label = "password"
    field.id = "field-id-1"
    field.value = value
    item_obj = MagicMock()
    item_obj.fields = [field]
    client.get_item.return_value = item_obj
    return client


def _ref(**spec: object) -> SecretRef:
    return SecretRef(source="onepassword", spec=dict(spec))


class TestRegister:
    def test_register_exposes_onepassword_secrets_backend(self, op_module: ModuleType) -> None:
        hooks = op_module.register()
        assert set(hooks) == {"secrets", "health"}
        backends = hooks["secrets"]
        assert set(backends) == {"onepassword"}
        backend = backends["onepassword"]
        # Structurally satisfies the SecretsBackend protocol.
        assert callable(getattr(backend, "resolve", None))
        assert backend.name == "onepassword"

    def test_register_exposes_health_check(self, op_module: ModuleType) -> None:
        hooks = op_module.register()
        assert "health" in hooks
        assert "onepassword" in hooks["health"]
        assert hooks["health"]["onepassword"] is op_module.health

    def test_register_returns_contributions_when_enabled_by_default(
        self, op_module: ModuleType
    ) -> None:
        # onepassword_enabled defaults True (opt-out) — unchanged behavior.
        # Pure register()-level enable gate; no secret value is involved.
        assert settings.onepassword_enabled is True
        hooks = op_module.register()
        assert set(hooks) == {"secrets", "health"}
        assert set(hooks["secrets"]) == {"onepassword"}

    def test_register_returns_empty_when_disabled(
        self, op_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Disabling contributes nothing — no secrets backend, no health. Still
        # a register()-level gate only; no secret is read or handled.
        monkeypatch.setattr(settings, "onepassword_enabled", False, raising=False)
        assert op_module.register() == {}


class TestHealth:
    """Plugin-health surface: `health()` reports CONFIGURATION status only —
    never the token value or a resolved secret (Phase 19 discipline).
    `error` if the SDK is missing, `degraded` if unconfigured, `ok` when both
    the SDK is importable and a Connect host + a token are present."""

    def test_error_when_sdk_missing(self, op_module: ModuleType) -> None:
        with patch.object(op_module, "new_client", None):
            result = op_module.health()
        assert result.status == "error"
        assert "onepasswordconnectsdk" in result.detail

    def test_ok_when_sdk_and_config_present(self, op_module: ModuleType) -> None:
        # Autouse `_op_settings` provides a fully-configured Connect baseline.
        with patch.object(op_module, "new_client", MagicMock()):
            result = op_module.health()
        assert result.status == "ok"

    def test_ok_with_service_account_token(
        self, op_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "op_connect_token", None, raising=False)
        monkeypatch.setattr(settings, "op_service_account_token", "sa-tok", raising=False)
        with patch.object(op_module, "new_client", MagicMock()):
            result = op_module.health()
        assert result.status == "ok"

    def test_degraded_when_unconfigured(
        self, op_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "op_connect_token", None, raising=False)
        monkeypatch.setattr(settings, "op_service_account_token", None, raising=False)
        with patch.object(op_module, "new_client", MagicMock()):
            result = op_module.health()
        assert result.status == "degraded"
        assert "not configured" in result.detail

    def test_health_never_leaks_token_value(self, op_module: ModuleType) -> None:
        """The token value must NEVER appear in the health detail — only
        presence booleans / mode names (Phase 19 no-secret discipline).
        `_op_settings` sets `op_connect_token=_FAKE_TOKEN`."""
        with patch.object(op_module, "new_client", MagicMock()):
            result = op_module.health()
        assert _FAKE_TOKEN not in result.detail
        assert "https://op-connect.example.com" not in result.detail

    def test_health_is_keyword_tolerant(self, op_module: ModuleType) -> None:
        with patch.object(op_module, "new_client", MagicMock()):
            result = op_module.health(project="anything")
        assert result.status in {"ok", "degraded", "error"}

    def test_health_never_raises_returns_error_type_name(
        self, op_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Boom:
            @property
            def op_connect_host(self) -> str:
                raise RuntimeError("boom-secret")

        monkeypatch.setattr("hivepilot.config.settings", _Boom())
        with patch.object(op_module, "new_client", MagicMock()):
            result = op_module.health()
        assert result.status == "error"
        assert result.detail == "RuntimeError"
        assert "boom-secret" not in result.detail

    def test_backend_is_not_a_dataclass(self, op_module: ModuleType) -> None:
        import dataclasses

        assert not dataclasses.is_dataclass(op_module.OnePasswordBackend)


class TestResolveHappyPath:
    def test_resolve_returns_fetched_value_discrete_spec(self, op_module: ModuleType) -> None:
        backend = op_module.OnePasswordBackend()
        new_client = MagicMock(return_value=_mock_client_returning(_FAKE_VALUE))

        with patch.object(op_module, "new_client", new_client):
            value = backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)

        assert value == _FAKE_VALUE

    def test_resolve_calls_client_with_vault_item_field_from_spec(
        self, op_module: ModuleType
    ) -> None:
        backend = op_module.OnePasswordBackend()
        client = _mock_client_returning(_FAKE_VALUE)
        new_client = MagicMock(return_value=client)

        with patch.object(op_module, "new_client", new_client):
            backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)

        # Client built with the Connect host + connect token from Settings.
        build_args, build_kwargs = new_client.call_args
        passed = list(build_args) + list(build_kwargs.values())
        assert "https://op-connect.example.com" in passed
        assert _FAKE_TOKEN in passed

        # Fetch keyed by the vault + item from the ref's spec.
        fetch_args, fetch_kwargs = client.get_item.call_args
        fetched = list(fetch_args) + list(fetch_kwargs.values())
        assert "db" in fetched  # item
        assert "Prod" in fetched  # vault

    def test_resolve_supports_full_op_reference(self, op_module: ModuleType) -> None:
        backend = op_module.OnePasswordBackend()
        client = _mock_client_returning(_FAKE_VALUE)
        new_client = MagicMock(return_value=client)

        with patch.object(op_module, "new_client", new_client):
            value = backend.resolve(_ref(ref="op://Prod/db/password"), settings)

        assert value == _FAKE_VALUE
        fetch_args, fetch_kwargs = client.get_item.call_args
        fetched = list(fetch_args) + list(fetch_kwargs.values())
        assert "db" in fetched
        assert "Prod" in fetched

    def test_field_selected_by_label(self, op_module: ModuleType) -> None:
        """The field named in the ref selects the matching field's value even
        when the item carries several fields."""
        backend = op_module.OnePasswordBackend()
        client = MagicMock()
        other = MagicMock()
        other.label = "username"
        other.id = "id-user"
        other.value = "not-the-secret"
        wanted = MagicMock()
        wanted.label = "password"
        wanted.id = "id-pass"
        wanted.value = _FAKE_VALUE
        item_obj = MagicMock()
        item_obj.fields = [other, wanted]
        client.get_item.return_value = item_obj
        new_client = MagicMock(return_value=client)

        with patch.object(op_module, "new_client", new_client):
            value = backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)

        assert value == _FAKE_VALUE

    def test_field_selected_by_id_when_label_absent(self, op_module: ModuleType) -> None:
        backend = op_module.OnePasswordBackend()
        client = MagicMock()
        field = MagicMock()
        field.label = "some-other-label"
        field.id = "credential"
        field.value = _FAKE_VALUE
        item_obj = MagicMock()
        item_obj.fields = [field]
        client.get_item.return_value = item_obj
        new_client = MagicMock(return_value=client)

        with patch.object(op_module, "new_client", new_client):
            value = backend.resolve(_ref(vault="Prod", item="db", field="credential"), settings)

        assert value == _FAKE_VALUE


class TestBackendSelection:
    def test_connect_backend_uses_connect_token(self, op_module: ModuleType) -> None:
        backend = op_module.OnePasswordBackend()
        new_client = MagicMock(return_value=_mock_client_returning(_FAKE_VALUE))

        with patch.object(op_module, "new_client", new_client):
            backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)

        build_args, build_kwargs = new_client.call_args
        passed = list(build_args) + list(build_kwargs.values())
        assert _FAKE_TOKEN in passed  # connect token, not the SA token

    def test_service_account_backend_uses_sa_token(
        self, op_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no Connect token but a service-account token set, the SA token
        authenticates the client against the same Connect host."""
        sa_token = "sa-tok-SHOULD-NOT-LEAK-999"
        monkeypatch.setattr(settings, "op_connect_token", None, raising=False)
        monkeypatch.setattr(settings, "op_service_account_token", sa_token, raising=False)
        backend = op_module.OnePasswordBackend()
        new_client = MagicMock(return_value=_mock_client_returning(_FAKE_VALUE))

        with patch.object(op_module, "new_client", new_client):
            backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)

        build_args, build_kwargs = new_client.call_args
        passed = list(build_args) + list(build_kwargs.values())
        assert sa_token in passed
        assert _FAKE_TOKEN not in passed


class TestResolveFailClosed:
    """Every failure path raises (so `closed` fail-mode aborts) and the error
    text names ONLY the reference identity + provider — never the token or the
    fetched value."""

    def test_missing_ref_and_discrete_keys_raises(self, op_module: ModuleType) -> None:
        backend = op_module.OnePasswordBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(), settings)
        assert "onepassword" in str(excinfo.value)

    def test_missing_field_in_discrete_spec_raises(self, op_module: ModuleType) -> None:
        backend = op_module.OnePasswordBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(vault="Prod", item="db"), settings)
        assert "onepassword" in str(excinfo.value)

    def test_missing_token_raises_naming_ref_and_provider_not_value(
        self, op_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "op_connect_token", None, raising=False)
        monkeypatch.setattr(settings, "op_service_account_token", None, raising=False)
        backend = op_module.OnePasswordBackend()
        new_client = MagicMock(return_value=_mock_client_returning(_FAKE_VALUE))

        with patch.object(op_module, "new_client", new_client):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)

        msg = str(excinfo.value)
        assert "onepassword" in msg
        assert "op://Prod/db/password" in msg
        assert _FAKE_VALUE not in msg
        # No fetch was attempted — the config gate short-circuits first.
        assert not new_client.return_value.get_item.called

    def test_missing_host_raises(
        self, op_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "op_connect_host", None, raising=False)
        backend = op_module.OnePasswordBackend()
        new_client = MagicMock(return_value=_mock_client_returning(_FAKE_VALUE))

        with patch.object(op_module, "new_client", new_client):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)

        msg = str(excinfo.value)
        assert "onepassword" in msg
        assert not new_client.return_value.get_item.called

    def test_sdk_not_installed_raises_naming_ref_and_provider(self, op_module: ModuleType) -> None:
        backend = op_module.OnePasswordBackend()
        with patch.object(op_module, "new_client", None):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)

        msg = str(excinfo.value)
        assert "onepassword" in msg
        assert "op://Prod/db/password" in msg

    def test_section_qualified_ref_raises_fail_closed_not_collapsed(
        self, op_module: ModuleType
    ) -> None:
        """A 4+ segment `op://vault/item/section/field` ref must be REJECTED,
        not silently collapsed to `(vault, item, field)` by dropping the
        section — collapsing would let field-by-label/id matching (which scans
        ALL fields regardless of section) silently fetch the wrong secret when
        two sections share a field label. No fetch is attempted."""
        backend = op_module.OnePasswordBackend()
        new_client = MagicMock(return_value=_mock_client_returning(_FAKE_VALUE))

        with patch.object(op_module, "new_client", new_client):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(ref="op://Prod/db/section/password"), settings)

        msg = str(excinfo.value)
        assert "onepassword" in msg
        assert not new_client.return_value.get_item.called

    def test_client_error_raises_without_leaking_value(self, op_module: ModuleType) -> None:
        """If the SDK itself raises — even if the exception message embeds the
        secret value — the re-raised error must NOT propagate that value (ref +
        provider only). Also proves the leaked exception is fully severed
        (`from None`)."""
        backend = op_module.OnePasswordBackend()
        client = MagicMock()
        client.get_item.side_effect = RuntimeError(f"upstream boom leaking {_FAKE_VALUE}")
        new_client = MagicMock(return_value=client)

        with patch.object(op_module, "new_client", new_client):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)

        msg = str(excinfo.value)
        assert "onepassword" in msg
        assert "op://Prod/db/password" in msg
        assert _FAKE_VALUE not in msg
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True

    def test_field_not_found_raises_without_leaking(self, op_module: ModuleType) -> None:
        backend = op_module.OnePasswordBackend()
        client = MagicMock()
        item_obj = MagicMock()
        item_obj.fields = []  # no fields at all
        client.get_item.return_value = item_obj
        new_client = MagicMock(return_value=client)

        with patch.object(op_module, "new_client", new_client):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)

        msg = str(excinfo.value)
        assert "onepassword" in msg
        assert "op://Prod/db/password" in msg

    def test_empty_string_value_raises_fail_closed_not_returned(
        self, op_module: ModuleType
    ) -> None:
        """An empty field value is a `str` (passes an `is None` check) but is
        never a legitimate secret — must raise (fail-closed), mirroring
        `EnvSecretsBackend`'s `if not value: raise`, NOT be silently returned as
        a resolved empty secret."""
        backend = op_module.OnePasswordBackend()
        new_client = MagicMock(return_value=_mock_client_returning(""))

        with patch.object(op_module, "new_client", new_client):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)

        msg = str(excinfo.value)
        assert "onepassword" in msg
        assert "op://Prod/db/password" in msg

    def test_client_construction_error_raises_without_leaking_token_or_value(
        self, op_module: ModuleType
    ) -> None:
        """Connect clients can authenticate at construction time. If
        constructing the client itself raises with a message embedding the token
        AND a value, the re-raised error must contain NEITHER — proving client
        construction sits INSIDE the same redaction boundary as the fetch call,
        not before it."""
        backend = op_module.OnePasswordBackend()
        new_client = MagicMock(
            side_effect=RuntimeError(f"auth failed for token={_FAKE_TOKEN} value={_FAKE_VALUE}")
        )

        with patch.object(op_module, "new_client", new_client):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)

        msg = str(excinfo.value)
        assert "onepassword" in msg
        assert "op://Prod/db/password" in msg
        assert _FAKE_TOKEN not in msg
        assert _FAKE_VALUE not in msg
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True


class TestServiceAccountDirectSdkMode:
    """The direct (non-Connect) service-account mode: when ``op_connect_host``
    is UNSET but ``op_service_account_token`` is set, the plugin resolves
    ``op://vault/item/field`` directly against api.1password.com via the
    official async ``onepassword-sdk`` (``onepassword.client.Client``), wrapped
    in ``asyncio.run``. The Connect path (host set) is unaffected."""

    def test_direct_path_resolves_via_official_sdk(
        self, op_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(settings, "op_connect_host", None, raising=False)
        monkeypatch.setattr(settings, "op_connect_token", None, raising=False)
        monkeypatch.setattr(settings, "op_service_account_token", "sa-tok-xyz", raising=False)
        backend = op_module.OnePasswordBackend()

        fake_client = MagicMock()
        fake_client.secrets.resolve = AsyncMock(return_value=_FAKE_VALUE)
        FakeClient = MagicMock()
        FakeClient.authenticate = AsyncMock(return_value=fake_client)

        with patch.object(op_module, "_OPClient", FakeClient):
            value = backend.resolve(_ref(ref="op://Prod/db/password"), settings)

        assert value == _FAKE_VALUE
        FakeClient.authenticate.assert_awaited_once()
        fake_client.secrets.resolve.assert_awaited_once_with("op://Prod/db/password")

    def test_direct_path_builds_reference_from_discrete_keys(
        self, op_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(settings, "op_connect_host", None, raising=False)
        monkeypatch.setattr(settings, "op_connect_token", None, raising=False)
        monkeypatch.setattr(settings, "op_service_account_token", "sa-tok-xyz", raising=False)
        backend = op_module.OnePasswordBackend()

        fake_client = MagicMock()
        fake_client.secrets.resolve = AsyncMock(return_value=_FAKE_VALUE)
        FakeClient = MagicMock()
        FakeClient.authenticate = AsyncMock(return_value=fake_client)

        with patch.object(op_module, "_OPClient", FakeClient):
            value = backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)

        assert value == _FAKE_VALUE
        fake_client.secrets.resolve.assert_awaited_once_with("op://Prod/db/password")

    def test_connect_path_still_used_when_host_set_even_with_sa_token(
        self, op_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Host set → Connect path, NEVER the direct SDK, even if only a
        service-account token is present (backward-compatible selection)."""
        monkeypatch.setattr(settings, "op_connect_token", None, raising=False)
        monkeypatch.setattr(settings, "op_service_account_token", "sa-tok", raising=False)
        backend = op_module.OnePasswordBackend()
        new_client = MagicMock(return_value=_mock_client_returning(_FAKE_VALUE))
        # If the direct SDK were (wrongly) used, this would blow up on await.
        with patch.object(op_module, "_OPClient", object()):
            with patch.object(op_module, "new_client", new_client):
                value = backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)
        assert value == _FAKE_VALUE
        new_client.assert_called_once()

    def test_direct_path_missing_sdk_raises_naming_lib_and_mode(
        self, op_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "op_connect_host", None, raising=False)
        monkeypatch.setattr(settings, "op_service_account_token", "sa-tok", raising=False)
        backend = op_module.OnePasswordBackend()
        with patch.object(op_module, "_OPClient", None):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)
        msg = str(excinfo.value)
        assert "onepassword-sdk" in msg
        assert "service-account" in msg

    def test_direct_path_error_is_redacted(
        self, op_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(settings, "op_connect_host", None, raising=False)
        monkeypatch.setattr(settings, "op_service_account_token", "sa-tok", raising=False)
        backend = op_module.OnePasswordBackend()
        FakeClient = MagicMock()
        FakeClient.authenticate = AsyncMock(
            side_effect=RuntimeError(f"auth boom leaking {_FAKE_VALUE} and {_FAKE_TOKEN}")
        )
        with patch.object(op_module, "_OPClient", FakeClient):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)
        msg = str(excinfo.value)
        assert "onepassword" in msg
        assert "op://Prod/db/password" in msg
        assert _FAKE_VALUE not in msg
        assert _FAKE_TOKEN not in msg
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True

    def test_neither_connect_nor_service_account_configured_raises(
        self, op_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "op_connect_host", None, raising=False)
        monkeypatch.setattr(settings, "op_connect_token", None, raising=False)
        monkeypatch.setattr(settings, "op_service_account_token", None, raising=False)
        backend = op_module.OnePasswordBackend()
        with pytest.raises(RuntimeError) as excinfo:
            backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)
        assert "onepassword" in str(excinfo.value)

    def test_direct_path_empty_value_fails_closed(
        self, op_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(settings, "op_connect_host", None, raising=False)
        monkeypatch.setattr(settings, "op_service_account_token", "sa-tok", raising=False)
        backend = op_module.OnePasswordBackend()
        fake_client = MagicMock()
        fake_client.secrets.resolve = AsyncMock(return_value="")
        FakeClient = MagicMock()
        FakeClient.authenticate = AsyncMock(return_value=fake_client)
        with patch.object(op_module, "_OPClient", FakeClient):
            with pytest.raises(RuntimeError) as excinfo:
                backend.resolve(_ref(vault="Prod", item="db", field="password"), settings)
        assert "op://Prod/db/password" in str(excinfo.value)

    async def test_direct_path_works_when_called_from_inside_a_running_event_loop(
        self, op_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_run_coro` future-proofs the synchronous `resolve()` contract: even
        when a caller invokes it from ALREADY inside a running event loop (this
        test itself runs as a coroutine — asyncio_mode=auto), it must not raise
        `asyncio.run() cannot be called from a running event loop` — it should
        transparently hand the work to a worker thread instead."""
        from unittest.mock import AsyncMock

        monkeypatch.setattr(settings, "op_connect_host", None, raising=False)
        monkeypatch.setattr(settings, "op_connect_token", None, raising=False)
        monkeypatch.setattr(settings, "op_service_account_token", "sa-tok-xyz", raising=False)
        backend = op_module.OnePasswordBackend()

        fake_client = MagicMock()
        fake_client.secrets.resolve = AsyncMock(return_value=_FAKE_VALUE)
        FakeClient = MagicMock()
        FakeClient.authenticate = AsyncMock(return_value=fake_client)

        # Proves a running loop IS active in this thread while resolve() runs.
        import asyncio

        assert asyncio.get_running_loop() is not None

        with patch.object(op_module, "_OPClient", FakeClient):
            # A plain synchronous call from inside a coroutine — no crash.
            value = backend.resolve(_ref(ref="op://Prod/db/password"), settings)

        assert value == _FAKE_VALUE


class TestPluginManagerRegistersOnePassword:
    def test_plugin_manager_registers_onepassword_into_secrets_map(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert "onepassword" in SECRETS_MAP
        assert callable(getattr(SECRETS_MAP["onepassword"], "resolve", None))
        assert any(r.source == "local-file" and r.name == "onepassword" for r in pm.loaded)

    def test_plugin_manager_skips_onepassword_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "onepassword_enabled", False, raising=False)

        plugins_mod.PluginManager()

        # register() early-returned {} → no secrets backend registered.
        assert "onepassword" not in SECRETS_MAP

    def test_onepassword_does_not_collide_with_infisical_or_builtins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import KNOWN_SECRET_BACKENDS

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)

        plugins_mod.PluginManager()

        # Both plugin providers coexist alongside every builtin backend.
        assert "onepassword" in SECRETS_MAP
        assert "infisical" in SECRETS_MAP
        for builtin in KNOWN_SECRET_BACKENDS:
            assert builtin in SECRETS_MAP
        assert SECRETS_MAP["onepassword"] is not SECRETS_MAP["infisical"]

    def test_name_collision_with_onepassword_aborts(self, op_module: ModuleType) -> None:
        """A second backend registering under `onepassword` is rejected by the
        fail-closed trust model (SecretsBackendCollisionError)."""
        SecretsRegistry.register("onepassword", op_module.OnePasswordBackend())

        class _Other:
            def resolve(self, ref: SecretRef, s: object) -> str:
                return "other"

        with pytest.raises(SecretsBackendCollisionError):
            SecretsRegistry.register("onepassword", _Other())
