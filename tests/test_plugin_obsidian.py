"""
Tests for the `obsidian` plugin (Sprint 2 of the plugins plan).

`plugins/obsidian.py` is a local-file plugin (see docs/v4/PLUGINS.md) that
logs pipeline runs into the Obsidian vault, both as a notifier (live log,
one line per `send_notification(...)` call) and as lifecycle hooks
(`on_pipeline_end` / `on_error`, a structured run-report block). Both
surfaces append to the SAME daily journal note:
    12 - HivePilot/Runs/YYYY-MM-DD.md

Covers, per the sprint spec:
(a) `register()` exposes notifier `obsidian` + the two hooks, and `obsidian`
    does not collide with `KNOWN_NOTIFIER_NAMES`.
(b) `notify()` appends to the daily journal in a tmp vault; a second call
    appends (doesn't overwrite).
(c) `notify()` raises `NotConfigured` when the vault is unset/missing.
(d) `on_pipeline_end` / `on_error` append a run-report block; an internal
    error is swallowed — a hook must never propagate.
(e) Loading via the real `PluginManager` local-file discovery mechanism
    registers `obsidian` into `NOTIFIER_MAP` and the hooks into the manager
    (mirrors `tests/test_rtk.py` / `tests/test_plugin_loading_mechanisms.py`).
"""

from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

import hivepilot.config as config_mod
from hivepilot.services.notification_service import (
    KNOWN_NOTIFIER_NAMES,
    NOTIFIER_MAP,
    NotConfigured,
)

REPO_ROOT = Path(__file__).parent.parent
OBSIDIAN_PLUGIN_PATH = REPO_ROOT / "plugins" / "obsidian.py"
_HIVEPILOT_SUBTREE = "12 - HivePilot"


def _load_obsidian_module() -> ModuleType:
    """Load plugins/obsidian.py by file path — same mechanism
    `hivepilot.plugins._scan_local_plugins` uses (no dependency on `plugins`
    being an importable package on sys.path)."""
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_obsidian_test", OBSIDIAN_PLUGIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def obsidian_module() -> ModuleType:
    return _load_obsidian_module()


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "Vault"
    (vault / _HIVEPILOT_SUBTREE / "Runs").mkdir(parents=True)
    return vault


def _today_journal(vault: Path) -> Path:
    today = datetime.date.today().isoformat()
    return vault / _HIVEPILOT_SUBTREE / "Runs" / f"{today}.md"


class TestRegister:
    def test_register_exposes_obsidian_notifier_and_hooks(
        self, obsidian_module: ModuleType
    ) -> None:
        hooks = obsidian_module.register()
        assert "notifiers" in hooks
        assert "obsidian" in hooks["notifiers"]
        assert hooks["on_pipeline_end"] is obsidian_module.on_pipeline_end
        assert hooks["on_error"] is obsidian_module.on_error

    def test_obsidian_name_does_not_collide_with_known_notifiers(self) -> None:
        assert "obsidian" not in KNOWN_NOTIFIER_NAMES

    def test_register_exposes_health_check(self, obsidian_module: ModuleType) -> None:
        hooks = obsidian_module.register()
        assert "health" in hooks
        assert hooks["health"]["obsidian"] is obsidian_module.health

    def test_register_returns_contributions_when_enabled_by_default(
        self, obsidian_module: ModuleType
    ) -> None:
        # obsidian_enabled defaults True (opt-out) — unchanged behavior.
        assert config_mod.settings.obsidian_enabled is True
        hooks = obsidian_module.register()
        assert "obsidian" in hooks["notifiers"]
        assert "on_pipeline_end" in hooks
        assert "on_error" in hooks
        assert "obsidian" in hooks["health"]

    def test_register_returns_empty_when_disabled(
        self, obsidian_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config_mod.settings, "obsidian_enabled", False, raising=False)
        assert obsidian_module.register() == {}


class TestHealth:
    """Sprint 2 (plugin-health): `health()` reflects `settings.obsidian_vault`
    presence + on-disk existence. `settings.obsidian_vault` is a non-Optional
    `Path` field defaulting to `Path("obsidian-vault")`
    (`hivepilot/config.py`) rather than `None` — "unset" is therefore
    detected against that field default, matching the plugin's own
    `_DEFAULT_OBSIDIAN_VAULT` sentinel."""

    def test_ok_when_vault_configured_and_exists(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        result = obsidian_module.health()

        assert result.status == "ok"

    def test_error_when_vault_configured_but_missing(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "NotThere"
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", missing, raising=False)

        result = obsidian_module.health()

        assert result.status == "error"

    def test_degraded_when_vault_unset_default(
        self, obsidian_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            config_mod.settings,
            "obsidian_vault",
            obsidian_module._DEFAULT_OBSIDIAN_VAULT,
            raising=False,
        )

        result = obsidian_module.health()

        assert result.status == "degraded"
        assert "not configured" in result.detail


class TestNotify:
    def test_notify_appends_to_daily_journal(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        obsidian_module.notify("first message")

        journal = _today_journal(vault)
        assert journal.exists()
        content = journal.read_text(encoding="utf-8")
        assert "first message" in content
        assert "language: en" in content  # went through ObsidianService frontmatter

    def test_second_notify_appends_not_overwrites(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        obsidian_module.notify("first message")
        obsidian_module.notify("second message")

        content = _today_journal(vault).read_text(encoding="utf-8")
        assert "first message" in content
        assert "second message" in content

    def test_notify_raises_not_configured_when_vault_unset(
        self, obsidian_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", None, raising=False)

        with pytest.raises(NotConfigured):
            obsidian_module.notify("message")

    def test_notify_raises_not_configured_when_vault_missing(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing_vault = tmp_path / "does-not-exist"
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", missing_vault, raising=False)

        with pytest.raises(NotConfigured):
            obsidian_module.notify("message")


class TestLifecycleHooks:
    def test_on_pipeline_end_appends_run_report(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        obsidian_module.on_pipeline_end(run_id=42, pipeline="default", status="complete")

        content = _today_journal(vault).read_text(encoding="utf-8")
        assert "42" in content
        assert "default" in content
        assert "complete" in content

    def test_on_error_appends_failure_report(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        obsidian_module.on_error(run_id=7, pipeline="default", stage="Build")

        content = _today_journal(vault).read_text(encoding="utf-8")
        assert "7" in content
        assert "default" in content
        assert "Build" in content

    def test_on_pipeline_end_noop_when_vault_unconfigured(
        self, obsidian_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", None, raising=False)

        # Must not raise.
        obsidian_module.on_pipeline_end(run_id=1, pipeline="p", status="complete")

    def test_on_error_noop_when_vault_unconfigured(
        self, obsidian_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", None, raising=False)

        # Must not raise.
        obsidian_module.on_error(run_id=1, pipeline="p", stage="s")

    def test_on_pipeline_end_swallows_internal_error(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        with (
            patch.object(
                obsidian_module.ObsidianService,
                "append_daily",
                side_effect=RuntimeError("boom"),
            ),
            patch.object(obsidian_module, "logger", MagicMock()) as mock_logger,
        ):
            # Must not raise — a hook must never crash a run.
            obsidian_module.on_pipeline_end(run_id=1, pipeline="p", status="complete")

        assert mock_logger.warning.called

    def test_on_error_swallows_internal_error(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        with (
            patch.object(
                obsidian_module.ObsidianService,
                "append_daily",
                side_effect=RuntimeError("boom"),
            ),
            patch.object(obsidian_module, "logger", MagicMock()) as mock_logger,
        ):
            # Must not raise — a hook must never crash a run.
            obsidian_module.on_error(run_id=1, pipeline="p", stage="s")

        assert mock_logger.warning.called


class TestLifecycleHooksHonorDryRun:
    """`on_pipeline_end` / `on_error` now honor a `dry_run=True` kwarg
    (threaded in by `Orchestrator.run_pipeline` — hook-context-enrichment):
    a dry-run pipeline must NOT write a real run-report note into the
    vault."""

    def test_on_pipeline_end_dry_run_true_does_not_write_vault(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        obsidian_module.on_pipeline_end(
            run_id=42, pipeline="default", status="complete", dry_run=True
        )

        journal = _today_journal(vault)
        assert not journal.exists()

    def test_on_pipeline_end_dry_run_false_writes_vault(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        obsidian_module.on_pipeline_end(
            run_id=42, pipeline="default", status="complete", dry_run=False
        )

        content = _today_journal(vault).read_text(encoding="utf-8")
        assert "42" in content

    def test_on_pipeline_end_absent_dry_run_kwarg_writes_vault(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Backward-compat: a caller that doesn't pass `dry_run` at all
        (older behavior / direct invocation) still writes for real."""
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        obsidian_module.on_pipeline_end(run_id=42, pipeline="default", status="complete")

        content = _today_journal(vault).read_text(encoding="utf-8")
        assert "42" in content

    def test_on_error_dry_run_true_does_not_write_vault(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        obsidian_module.on_error(run_id=7, pipeline="default", stage="Build", dry_run=True)

        journal = _today_journal(vault)
        assert not journal.exists()

    def test_on_error_dry_run_false_writes_vault(
        self, obsidian_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = _make_vault(tmp_path)
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", vault, raising=False)

        obsidian_module.on_error(run_id=7, pipeline="default", stage="Build", dry_run=False)

        content = _today_journal(vault).read_text(encoding="utf-8")
        assert "Build" in content


class TestPluginManagerDiscoversObsidian:
    @pytest.fixture(autouse=True)
    def _restore_notifier_map(self):
        """NOTIFIER_MAP is process-global mutable state — snapshot/restore
        around every test here so `obsidian` (registered by the real
        `plugins/obsidian.py` on disk) never leaks into other test modules
        sharing the pytest session (same pattern as test_rtk.py)."""
        snapshot = dict(NOTIFIER_MAP)
        yield
        NOTIFIER_MAP.clear()
        NOTIFIER_MAP.update(snapshot)

    def test_plugin_manager_registers_obsidian_into_notifier_map(self, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert "obsidian" in NOTIFIER_MAP
        assert any(r.source == "local-file" and r.name == "obsidian" for r in pm.loaded)
        assert pm.hooks.get("on_pipeline_end")
        assert pm.hooks.get("on_error")

    def test_plugin_manager_skips_obsidian_when_disabled(self, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "obsidian_enabled", False, raising=False)

        pm = plugins_mod.PluginManager()

        # register() early-returned {} → no notifier and no lifecycle hooks.
        assert "obsidian" not in NOTIFIER_MAP
        assert not pm.hooks.get("on_pipeline_end")
        assert not pm.hooks.get("on_error")
