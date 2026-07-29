"""Tests for the `pinokio` DETECTION plugin (`plugins/pinokio.py`).

Every test here fails against the pre-change tree for the same blunt reason:
`plugins/pinokio.py` does not exist there, so `_load_pinokio_module()` raises
`FileNotFoundError` at collection of the first test that calls it, and the
`settings.pinokio_enabled` assertions raise `AttributeError`. There is no
subtler pre-existing behaviour being pinned down — the module is new.

What is deliberately NOT tested, because it is deliberately NOT built: a
runner contribution. Pinokio ships no headless prompt CLI (its optional CLI
is `pterm`, installed on demand into `PINOKIO_HOME/bin/npm/bin/pterm` by
`pinokiod`'s `kernel/bin/cli.js`), so there is nothing to route a task to.
`test_register_never_contributes_a_runner` pins that absence so a later
change cannot quietly add a runner that shells out to a binary nobody has.

All filesystem state is built under `tmp_path`; `HOME`/`XDG_CONFIG_HOME`/
`PINOKIO_HOME` are always monkeypatched, so no test ever reads the real `~`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from hivepilot.config import settings

REPO_ROOT = Path(__file__).resolve().parent.parent
PINOKIO_PLUGIN_PATH = REPO_ROOT / "plugins" / "pinokio.py"

_ENV_KEYS = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "PINOKIO_HOME")

pytestmark = pytest.mark.usefixtures("_clean_pinokio_env")


def _load_pinokio_module() -> ModuleType:
    """Load `plugins/pinokio.py` by file path — the same mechanism
    `hivepilot.plugins._scan_local_plugins` uses, and the loading pattern
    `tests/test_hugo.py` / `tests/test_gating_conformance.py` already use
    (never `import plugins.pinokio`, which would pollute `sys.modules`)."""
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_pinokio_test", PINOKIO_PLUGIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def _clean_pinokio_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every environment variable detection reads, so a test that does
    not set one is genuinely exercising the 'unset' branch rather than
    inheriting the developer's own machine."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _make_home(root: Path, *, markers: tuple[str, ...] = ("ENVIRONMENT", "key.json")) -> Path:
    """Build a Pinokio home the way `pinokiod` does on first init: the home
    directory plus the two files `kernel/index.js` writes unconditionally
    (`ENVIRONMENT`, then `key.json`). Note it deliberately does NOT create
    `api/` — that folder only appears once an app is downloaded, so a fresh
    home genuinely has none."""
    root.mkdir(parents=True, exist_ok=True)
    for marker in markers:
        (root / marker).write_text("{}", encoding="utf-8")
    return root


def _write_store(config_home: Path, payload: Any) -> Path:
    """Write the Electron-store file the Pinokio desktop app keeps its
    settings in: `<userData>/config.json`, where Electron's `userData` on
    Linux is `<XDG_CONFIG_HOME or ~/.config>/<app name>` and Pinokio's app
    name is `Pinokio` (package.json `name`, no `productName` override)."""
    store_dir = config_home / "Pinokio"
    store_dir.mkdir(parents=True, exist_ok=True)
    store = store_dir / "config.json"
    store.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return store


def _skip_if_root() -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("permission-denied branches are unreachable as root")


# ---------------------------------------------------------------------------
# Present -> detected, with the discovered install path reported
# ---------------------------------------------------------------------------


class TestDetectedWhenPresent:
    def test_default_convention_home_is_detected_and_path_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _make_home(tmp_path / "pinokio")
        monkeypatch.setenv("HOME", str(tmp_path))

        pinokio = _load_pinokio_module()
        detection = pinokio.detect()

        assert detection.present is True
        assert detection.home == str(home)
        assert detection.status == "ok"
        assert detection.problems == ()

        status = pinokio.health()
        assert status.status == "ok"
        assert str(home) in status.detail

    def test_register_contributes_only_a_health_check_named_pinokio(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_home(tmp_path / "pinokio")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(settings, "pinokio_enabled", True, raising=False)

        pinokio = _load_pinokio_module()
        contributed = pinokio.register()

        assert set(contributed) == {"health"}
        assert set(contributed["health"]) == {"pinokio"}
        assert callable(contributed["health"]["pinokio"])

    def test_installed_apps_are_enumerated_from_the_api_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _make_home(tmp_path / "pinokio")
        (home / "api" / "comfyui.git").mkdir(parents=True)
        (home / "api" / "some-local-app").mkdir(parents=True)
        # A stray FILE under api/ is not an app — `pinokiod` only ever treats
        # directories under `api/` as installed apps.
        (home / "api" / "notes.txt").write_text("x", encoding="utf-8")

        monkeypatch.setenv("HOME", str(tmp_path))
        pinokio = _load_pinokio_module()
        detection = pinokio.detect()

        assert detection.apps == ("comfyui.git", "some-local-app")
        assert pinokio.health().detail.endswith("2 app(s) installed")

    def test_missing_api_directory_is_normal_for_a_fresh_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`api/` is created lazily on the first app download, so its absence
        must read as 'zero apps', NOT as a broken layout."""
        _make_home(tmp_path / "pinokio")
        monkeypatch.setenv("HOME", str(tmp_path))

        pinokio = _load_pinokio_module()
        detection = pinokio.detect()

        assert detection.apps == ()
        assert detection.status == "ok"


# ---------------------------------------------------------------------------
# Absent -> register() returns {}, health is degraded
# ---------------------------------------------------------------------------


class TestAbsent:
    def test_register_returns_empty_and_health_degraded_when_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))  # empty HOME, no ~/pinokio
        monkeypatch.setattr(settings, "pinokio_enabled", True, raising=False)

        pinokio = _load_pinokio_module()

        assert pinokio.register() == {}
        status = pinokio.health()
        assert status.status == "degraded"
        assert "not detected" in status.detail

    def test_no_home_env_at_all_yields_no_candidates_and_never_touches_real_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With HOME, XDG_CONFIG_HOME and PINOKIO_HOME all unset there is no
        location to check — the plugin must report that plainly instead of
        falling back to `Path.home()` / `os.path.expanduser`, which would
        resolve through the password database to some other user's home."""
        pinokio = _load_pinokio_module()
        detection = pinokio.detect()

        assert detection.present is False
        assert detection.checked == ()
        assert pinokio.health().status == "degraded"

    def test_empty_env_values_are_not_treated_as_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The repo's most-repeated bug class: an empty value read as 'no
        constraint'. An empty `PINOKIO_HOME`/`HOME` must mean 'nothing
        configured', never a candidate rooted at the filesystem root."""
        monkeypatch.setenv("PINOKIO_HOME", "")
        monkeypatch.setenv("HOME", "   ")

        pinokio = _load_pinokio_module()
        detection = pinokio.detect()

        assert detection.present is False
        assert detection.checked == ()


# ---------------------------------------------------------------------------
# The enable flag is a permission gate, not a detection signal
# ---------------------------------------------------------------------------


class TestEnableFlagGate:
    def test_flag_defaults_true(self) -> None:
        assert settings.pinokio_enabled is True

    def test_flag_off_returns_empty_even_when_pinokio_is_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_home(tmp_path / "pinokio")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(settings, "pinokio_enabled", False, raising=False)

        pinokio = _load_pinokio_module()

        assert pinokio.register() == {}
        # health() stays a pure detection probe: the flag governs whether the
        # engine WIRES the check in, not what the host actually looks like.
        assert pinokio.health().status == "ok"


# ---------------------------------------------------------------------------
# Home resolution order + env honouring
# ---------------------------------------------------------------------------


class TestHomeResolution:
    def test_pinokio_home_env_var_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elsewhere = _make_home(tmp_path / "mnt" / "big-disk" / "pinokio")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("PINOKIO_HOME", str(elsewhere))

        pinokio = _load_pinokio_module()
        detection = pinokio.detect()

        assert detection.home == str(elsewhere)
        assert detection.source == "PINOKIO_HOME"

    def test_electron_store_config_wins_over_pinokio_home_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mirrors `pinokiod`'s own precedence, `kernel/index.js`:
        `this.store.get("home") || process.env.PINOKIO_HOME`."""
        from_store = _make_home(tmp_path / "chosen")
        from_env = _make_home(tmp_path / "ignored")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("PINOKIO_HOME", str(from_env))
        _write_store(tmp_path / "home" / ".config", {"home": str(from_store)})

        pinokio = _load_pinokio_module()
        detection = pinokio.detect()

        assert detection.home == str(from_store)
        assert detection.source == "electron-store"

    def test_xdg_config_home_is_honoured_for_the_electron_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chosen = _make_home(tmp_path / "chosen")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
        # A decoy under ~/.config must be ignored while XDG_CONFIG_HOME is set.
        _write_store(tmp_path / "home" / ".config", {"home": str(tmp_path / "decoy")})
        _write_store(tmp_path / "xdg-config", {"home": str(chosen)})

        pinokio = _load_pinokio_module()

        assert pinokio.detect().home == str(chosen)

    def test_xdg_data_home_is_never_used_as_a_detection_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pinokio's store lives under Electron's `userData` (XDG_CONFIG_HOME),
        and Pinokio itself REWRITES `XDG_DATA_HOME` to `PINOKIO_HOME/cache/
        XDG_DATA_HOME` for the apps it launches. Treating the caller's
        `XDG_DATA_HOME` as an install location would therefore be a guess —
        and a wrong one inside any Pinokio-launched process."""
        _make_home(tmp_path / "xdg-data" / "pinokio")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))

        pinokio = _load_pinokio_module()
        detection = pinokio.detect()

        assert detection.present is False
        assert all("xdg-data" not in entry for entry in detection.checked)

    def test_store_present_but_home_unset_falls_through_and_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `config.json` with no (or a blank) `home` key means Pinokio is
        installed but no home has been chosen — it must NOT be read as
        'no constraint, use the default'-and-claim-ok silently."""
        store = _write_store(tmp_path / "home" / ".config", {"home": "   "})
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        pinokio = _load_pinokio_module()
        detection = pinokio.detect()

        assert detection.present is False
        assert any(str(store) in problem for problem in detection.problems)


# ---------------------------------------------------------------------------
# Fail closed: partial / unreadable layouts are degraded, never ok
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_partial_layout_is_degraded_and_names_the_missing_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_home(tmp_path / "pinokio", markers=("ENVIRONMENT",))  # no key.json
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(settings, "pinokio_enabled", True, raising=False)

        pinokio = _load_pinokio_module()
        detection = pinokio.detect()

        assert detection.present is False
        assert detection.status == "degraded"
        assert any("key.json" in problem for problem in detection.problems)
        assert pinokio.health().status == "degraded"
        assert pinokio.register() == {}

    def test_home_that_is_a_file_not_a_directory_is_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pinokio").write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv("HOME", str(tmp_path))

        pinokio = _load_pinokio_module()
        detection = pinokio.detect()

        assert detection.present is False
        assert pinokio.health().status == "degraded"

    def test_unreadable_home_is_degraded_never_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _skip_if_root()
        home = _make_home(tmp_path / "pinokio")
        home.chmod(0o000)
        try:
            monkeypatch.setenv("HOME", str(tmp_path))
            pinokio = _load_pinokio_module()
            detection = pinokio.detect()

            assert detection.present is False
            assert detection.status == "degraded"
            assert any(str(home) in problem for problem in detection.problems)
        finally:
            home.chmod(stat.S_IRWXU)

    def test_unreadable_api_directory_reports_home_but_stays_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pinokio IS present (both markers found) but the app inventory could
        not be read — report the install path and say exactly what failed,
        never `ok` on partial evidence."""
        _skip_if_root()
        home = _make_home(tmp_path / "pinokio")
        api = home / "api"
        api.mkdir()
        api.chmod(0o000)
        try:
            monkeypatch.setenv("HOME", str(tmp_path))
            monkeypatch.setattr(settings, "pinokio_enabled", True, raising=False)
            pinokio = _load_pinokio_module()
            detection = pinokio.detect()

            assert detection.home == str(home)
            assert detection.apps == ()
            assert detection.status == "degraded"
            assert any(str(api) in problem for problem in detection.problems)

            status = pinokio.health()
            assert status.status == "degraded"
            assert str(home) in status.detail
            # Still registered: the operator only learns about the degradation
            # through the health check being wired in.
            assert set(pinokio.register()) == {"health"}
        finally:
            api.chmod(stat.S_IRWXU)

    def test_malformed_store_json_is_reported_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _write_store(tmp_path / "home" / ".config", "{ this is not json")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        pinokio = _load_pinokio_module()
        detection = pinokio.detect()

        assert detection.present is False
        assert any(str(store) in problem for problem in detection.problems)

    def test_store_json_that_is_not_an_object_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_store(tmp_path / "home" / ".config", ["home", "/somewhere"])
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        pinokio = _load_pinokio_module()

        assert pinokio.detect().present is False

    def test_health_detail_is_a_single_sanitised_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`home` comes out of a user-writable JSON file and app names come
        out of a directory listing; neither may inject control characters or
        newlines into a health detail that is rendered to a terminal."""
        nasty = tmp_path / "ho\nme\x1b[31m"
        _make_home(nasty)
        _write_store(tmp_path / "home" / ".config", {"home": str(nasty)})
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        pinokio = _load_pinokio_module()
        detail = pinokio.health().detail

        assert "\n" not in detail
        assert "\x1b" not in detail


# ---------------------------------------------------------------------------
# Side-effect freedom + honest contributions
# ---------------------------------------------------------------------------


class TestNoSideEffects:
    def test_module_source_spawns_no_process_and_opens_no_socket(self) -> None:
        """Detection must be filesystem-only: `register()` runs on every
        process start, so a network probe or a process spawn there would tax
        every single CLI invocation — and 'never execute what you discover'
        is a hard requirement, since what is discovered is arbitrary
        third-party AI apps."""
        source = PINOKIO_PLUGIN_PATH.read_text(encoding="utf-8")
        for forbidden in ("subprocess", "socket", "urllib", "requests", "http.client", "popen"):
            assert forbidden not in source, f"pinokio plugin must not reference {forbidden!r}"

    def test_detect_does_not_shell_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("pinokio detection must never spawn a process")

        monkeypatch.setattr(subprocess, "run", _boom)
        monkeypatch.setattr(subprocess, "Popen", _boom)
        monkeypatch.setattr(os, "system", _boom)

        home = _make_home(tmp_path / "pinokio")
        (home / "api" / "an-app").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path))

        pinokio = _load_pinokio_module()
        assert pinokio.detect().apps == ("an-app",)

    def test_register_never_contributes_a_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pinokio is a launcher for local AI apps with web UIs, not a
        prompt-in/text-out CLI. There is no binary to route a task to, so the
        plugin contributes detection only."""
        from hivepilot.models import KNOWN_RUNNER_KINDS
        from hivepilot.services.agent_checks import AGENT_RUNNER_KINDS

        _make_home(tmp_path / "pinokio")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(settings, "pinokio_enabled", True, raising=False)

        pinokio = _load_pinokio_module()

        assert "runners" not in pinokio.register()
        assert "pinokio" not in AGENT_RUNNER_KINDS
        assert "pinokio" not in KNOWN_RUNNER_KINDS


# ---------------------------------------------------------------------------
# `hivepilot plugins list` integration
# ---------------------------------------------------------------------------


class TestPluginsListIntegration:
    def test_plugins_list_shows_pinokio_with_an_accurate_contributions_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drives a REAL `PluginManager` over the real `plugins/` directory,
        exactly like tests/test_plugins_list_taxonomy.py, so the assertion
        proves real registration -> real attribution -> real rendering."""
        from unittest.mock import MagicMock

        from typer.testing import CliRunner

        from hivepilot.cli import app
        from hivepilot.plugins import PluginManager

        _make_home(tmp_path / "pinokio")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(settings, "pinokio_enabled", True, raising=False)

        manager = PluginManager()

        record = next((r for r in manager.loaded if r.name == "pinokio"), None)
        assert record is not None, "pinokio plugin did not register"
        # Accurate: a health check and nothing else. No runner is claimed.
        assert record.contributions == {"health": ["pinokio"]}
        assert manager.health["pinokio"]().status == "ok"

        mock_orch = MagicMock()
        mock_orch.plugins = manager
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda: mock_orch)
        result = CliRunner().invoke(app, ["plugins", "list"])

        assert result.exit_code == 0
        assert "pinokio" in result.stdout
        assert "health: pinokio" in result.stdout
        assert "runners: pinokio" not in result.stdout

    def test_plugins_list_credits_pinokio_nothing_when_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local-file discovery loads every `plugins/*.py` regardless of its
        gate, so an absent Pinokio still gets a `PluginRecord` — with an EMPTY
        `contributions` dict, rendering an empty `contributes` cell. Same
        assertion shape tests/test_plugins_list_taxonomy.py uses for the
        dormant `sample` / `sample_skill` plugins."""
        from hivepilot.plugins import PluginManager

        monkeypatch.setenv("HOME", str(tmp_path))  # nothing installed
        monkeypatch.setattr(settings, "pinokio_enabled", True, raising=False)

        manager = PluginManager()
        record = next((r for r in manager.loaded if r.name == "pinokio"), None)

        assert record is not None
        assert record.contributions == {}
        assert "pinokio" not in manager.health


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
