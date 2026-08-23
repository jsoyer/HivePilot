"""Health POLARITY of the `bitwarden` plugin — error means configured-but-broken.

The regression this file pins: five default-enabled bundled plugins
(bitwarden, vaultwarden, infisical, kms, hugo) reported `error` whenever
their optional prerequisite was missing, and the Pollen header rendered five
permanent red pills on a box that used none of them. A check that always
fires tells you nothing — red must be reserved for the state an operator
should act on: someone CONFIGURED the provider (intent) and the tool it
needs is missing.

Companion file to `tests/test_plugin_bitwarden.py` (which owns the resolve /
register / leak-proof surface — its autouse baseline sets BW_SESSION, so its
"error when bw missing" test is exactly the configured-but-broken case and
still holds).
"""

from __future__ import annotations

import importlib.util
from types import ModuleType

import pytest
from conftest import BUNDLED_PLUGINS


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_bitwarden_polarity_test", BUNDLED_PLUGINS / "bitwarden.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def bw_module() -> ModuleType:
    return _load()


class TestHealthPolarity:
    def test_missing_cli_with_nothing_configured_is_degraded_not_error(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The COMMON state on a default-enabled plugin: no `bw`, no session.
        Nobody chose this plugin — mere unavailability, never a red pill."""
        monkeypatch.delenv("BW_SESSION", raising=False)
        monkeypatch.setattr(bw_module.shutil, "which", lambda _b: None)

        result = bw_module.health()

        assert result.status == "degraded"
        assert "not usable" in result.detail

    def test_missing_cli_with_a_session_configured_is_error(
        self, bw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Someone set BW_SESSION: intent exists, the tool is missing —
        the one state that deserves red. The detail names the env var,
        never its value."""
        monkeypatch.setenv("BW_SESSION", "TOKEN-VALUE-MUST-NOT-LEAK")
        monkeypatch.setattr(bw_module.shutil, "which", lambda _b: None)

        result = bw_module.health()

        assert result.status == "error"
        assert "BW_SESSION" in result.detail
        assert "TOKEN-VALUE-MUST-NOT-LEAK" not in result.detail
