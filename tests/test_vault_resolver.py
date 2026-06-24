"""
Tests for Vault and SOPS secret resolvers in secrets_service.py (PROD-HARDENING 2b).

All tests are offline — hvac and subprocess are mocked.

Verifies:
- _from_vault returns correct value when hvac is mocked
- _from_vault raises ValueError when vault is unconfigured
- _from_vault raises ImportError when hvac is not installed
- Unknown source raises ValueError (existing behaviour preserved)
- _from_sops returns correct value with mocked subprocess
- _from_sops raises RuntimeError when sops binary absent
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hivepilot.services.secrets_service import SecretResolver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hvac_mock(secret_data: dict) -> types.ModuleType:
    """Build a minimal hvac stub that returns *secret_data* from KV v2."""
    hvac_mod = types.ModuleType("hvac")
    client_instance = MagicMock()
    client_instance.secrets.kv.v2.read_secret_version.return_value = {"data": {"data": secret_data}}
    hvac_mod.Client = MagicMock(return_value=client_instance)
    return hvac_mod


# ---------------------------------------------------------------------------
# Vault resolver tests
# ---------------------------------------------------------------------------


class TestFromVault:
    def test_returns_correct_value_when_hvac_mocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With hvac mocked, _from_vault returns the correct secret value."""
        monkeypatch.setenv("HIVEPILOT_VAULT_ADDR", "http://vault.local:8200")
        monkeypatch.setenv("HIVEPILOT_VAULT_TOKEN", "test-token")

        hvac_stub = _make_hvac_mock({"my_api_key": "super-secret-123"})
        with patch.dict(sys.modules, {"hvac": hvac_stub}):
            resolver = SecretResolver()
            result = resolver._from_vault({"path": "secret/data/myapp", "key": "my_api_key"})

        assert result == "super-secret-123"

    def test_vault_client_receives_correct_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """hvac.Client is called with the right URL, and read_secret_version with right path."""
        monkeypatch.setenv("HIVEPILOT_VAULT_ADDR", "http://vault.local:8200")
        monkeypatch.setenv("HIVEPILOT_VAULT_TOKEN", "root-token")

        hvac_stub = _make_hvac_mock({"password": "hunter2"})
        with patch.dict(sys.modules, {"hvac": hvac_stub}):
            resolver = SecretResolver()
            resolver._from_vault({"path": "secret/data/myapp", "key": "password"})

        hvac_stub.Client.assert_called_once_with(url="http://vault.local:8200", token="root-token")
        client_instance = hvac_stub.Client.return_value
        client_instance.secrets.kv.v2.read_secret_version.assert_called_once_with(
            path="secret/data/myapp"
        )

    def test_raises_value_error_when_vault_addr_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Raises ValueError with clear message when HIVEPILOT_VAULT_ADDR is absent."""
        monkeypatch.delenv("HIVEPILOT_VAULT_ADDR", raising=False)
        monkeypatch.delenv("HIVEPILOT_VAULT_TOKEN", raising=False)

        hvac_stub = _make_hvac_mock({})
        with patch.dict(sys.modules, {"hvac": hvac_stub}):
            resolver = SecretResolver()
            with pytest.raises(ValueError, match="HIVEPILOT_VAULT_ADDR"):
                resolver._from_vault({"path": "secret/data/myapp", "key": "key"})

    def test_raises_value_error_when_vault_token_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Raises ValueError with clear message when HIVEPILOT_VAULT_TOKEN is absent."""
        monkeypatch.setenv("HIVEPILOT_VAULT_ADDR", "http://vault.local:8200")
        monkeypatch.delenv("HIVEPILOT_VAULT_TOKEN", raising=False)

        hvac_stub = _make_hvac_mock({})
        with patch.dict(sys.modules, {"hvac": hvac_stub}):
            resolver = SecretResolver()
            with pytest.raises(ValueError, match="HIVEPILOT_VAULT_TOKEN"):
                resolver._from_vault({"path": "secret/data/myapp", "key": "key"})

    def test_raises_import_error_when_hvac_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Raises ImportError with clear install hint when hvac is not installed."""
        monkeypatch.setenv("HIVEPILOT_VAULT_ADDR", "http://vault.local:8200")
        monkeypatch.setenv("HIVEPILOT_VAULT_TOKEN", "root-token")

        # Remove hvac from sys.modules so the lazy import fails
        with patch.dict(sys.modules, {"hvac": None}):
            resolver = SecretResolver()
            with pytest.raises(ImportError, match="pip install hvac"):
                resolver._from_vault({"path": "secret/data/myapp", "key": "key"})

    def test_vault_resolver_registered_in_resolvers(self) -> None:
        """'vault' is registered in SecretResolver.resolvers."""
        resolver = SecretResolver()
        assert "vault" in resolver.resolvers

    def test_resolve_dispatch_vault_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """resolve() dispatches 'vault' source correctly end-to-end."""
        monkeypatch.setenv("HIVEPILOT_VAULT_ADDR", "http://vault.local:8200")
        monkeypatch.setenv("HIVEPILOT_VAULT_TOKEN", "test-token")

        hvac_stub = _make_hvac_mock({"db_pass": "secret-db-pass"})
        with patch.dict(sys.modules, {"hvac": hvac_stub}):
            resolver = SecretResolver()
            result = resolver.resolve(
                {
                    "database_password": {
                        "source": "vault",
                        "path": "secret/data/db",
                        "key": "db_pass",
                    }
                }
            )

        assert result == {"database_password": "secret-db-pass"}


# ---------------------------------------------------------------------------
# SOPS resolver tests
# ---------------------------------------------------------------------------


class TestFromSops:
    def test_returns_correct_value_with_mocked_subprocess(self, tmp_path: Path) -> None:
        """With sops binary mocked, _from_sops returns the correct decrypted value."""
        sops_file = tmp_path / "secrets.yaml"
        sops_file.write_text("placeholder", encoding="utf-8")

        decrypted_yaml = yaml.safe_dump({"api_key": "my-decrypted-value"})

        with (
            patch("shutil.which", return_value="/usr/bin/sops"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=decrypted_yaml, stderr=""),
            ),
        ):
            resolver = SecretResolver()
            result = resolver._from_sops({"file": str(sops_file), "key": "api_key"})

        assert result == "my-decrypted-value"

    def test_raises_runtime_error_when_sops_absent(self, tmp_path: Path) -> None:
        """Raises RuntimeError with clear message when sops binary is not in PATH."""
        with patch("shutil.which", return_value=None):
            resolver = SecretResolver()
            with pytest.raises(RuntimeError, match="sops"):
                resolver._from_sops({"file": "secrets.yaml", "key": "api_key"})

    def test_raises_runtime_error_on_sops_failure(self, tmp_path: Path) -> None:
        """Raises RuntimeError when sops -d exits non-zero."""
        sops_file = tmp_path / "secrets.yaml"
        sops_file.write_text("placeholder", encoding="utf-8")

        with (
            patch("shutil.which", return_value="/usr/bin/sops"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=1, stdout="", stderr="decryption failed"),
            ),
        ):
            resolver = SecretResolver()
            with pytest.raises(RuntimeError, match="decryption failed"):
                resolver._from_sops({"file": str(sops_file), "key": "api_key"})

    def test_raises_key_error_when_key_not_in_decrypted_output(self, tmp_path: Path) -> None:
        """Raises KeyError when the requested key is absent in decrypted content."""
        decrypted_yaml = yaml.safe_dump({"other_key": "some-value"})

        with (
            patch("shutil.which", return_value="/usr/bin/sops"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout=decrypted_yaml, stderr=""),
            ),
        ):
            resolver = SecretResolver()
            with pytest.raises(KeyError, match="missing_key"):
                resolver._from_sops({"file": "secrets.yaml", "key": "missing_key"})

    def test_sops_resolver_registered(self) -> None:
        """'sops' is registered in SecretResolver.resolvers."""
        resolver = SecretResolver()
        assert "sops" in resolver.resolvers


# ---------------------------------------------------------------------------
# Existing behaviour — unknown source
# ---------------------------------------------------------------------------


class TestUnknownSource:
    def test_unknown_source_raises_value_error(self) -> None:
        """Unknown source raises ValueError (existing behavior preserved)."""
        resolver = SecretResolver()
        with pytest.raises(ValueError, match="Unknown secret source"):
            resolver.resolve({"my_secret": {"source": "unknown_backend", "key": "foo"}})
