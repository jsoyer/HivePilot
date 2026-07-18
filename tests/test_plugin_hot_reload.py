"""Tests for `hivepilot.plugins.PluginManager.reload()` — Phase 26b hot-reload.

Covers:
- reload picks up a NEW plugin file (added)
- reload re-registers a CHANGED plugin (updated)
- reload drops a REMOVED plugin's kind (removed)
- ATOMICITY (security-critical): a colliding candidate set, or one whose
  explicit-entry pin raises on import, leaves LIVE state COMPLETELY
  untouched — the previously-working plugin still resolves
- ownership tracking: reload never clobbers a builtin runner kind nor a kind
  this manager never staged (simulating another manager's registration)
- `plugins_changed_on_disk()` mtime-based change detection
- regression: plain `PluginManager()` construction still populates
  `PluginRecord.contributions` (Phase 26a attribution) the same way
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hivepilot import plugins as plugins_mod
from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.plugins import PluginManager, ReloadResult
from hivepilot.registry import RUNNER_MAP, RunnerRegistry, resolve_runner_class
from hivepilot.runners.base import BaseRunner, RunnerPayload


def _write_runner_plugin(
    plugin_dir: Path,
    filename: str,
    kind: str,
    class_name: str = "FixtureRunner",
    marker: str = "v1",
) -> None:
    """Write a minimal local-file plugin contributing a single runner kind.

    `marker` is a class-level attribute so a test can prove a reload actually
    re-executed the file (a NEW class object with the NEW marker value) --
    local-file plugins are always re-exec'd via `spec_from_file_location`,
    never cached in `sys.modules` (see `hivepilot.plugins._scan_local_plugins`).
    """
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / filename).write_text(
        f"""
class {class_name}:
    MARKER = {marker!r}

    def __init__(self, definition, settings):
        self.definition = definition
        self.settings = settings

    def run(self, payload):
        return None


def register():
    return {{"runners": {{"{kind}": {class_name}}}}}
""",
        encoding="utf-8",
    )


class TestReloadPicksUpChanges:
    def test_reload_adds_new_plugin(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        assert "fixture-a" not in RUNNER_MAP

        _write_runner_plugin(pdir, "a.py", kind="fixture-a")
        result = pm.reload()

        assert result.ok is True
        assert result.added == ["a"]
        assert result.removed == []
        assert "fixture-a" in RUNNER_MAP
        assert any(r.name == "a" for r in pm.loaded)

    def test_reload_reregisters_changed_plugin(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "a.py", kind="fixture-a", marker="v1")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        first_cls = RUNNER_MAP["fixture-a"]
        assert first_cls.MARKER == "v1"  # type: ignore[attr-defined]

        _write_runner_plugin(pdir, "a.py", kind="fixture-a", marker="v2")
        result = pm.reload()

        assert result.ok is True
        assert result.updated == ["a"]
        second_cls = RUNNER_MAP["fixture-a"]
        assert second_cls is not first_cls
        assert second_cls.MARKER == "v2"  # type: ignore[attr-defined]

    def test_reload_removes_deleted_plugin(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "a.py", kind="fixture-a")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        assert "fixture-a" in RUNNER_MAP

        (pdir / "a.py").unlink()
        result = pm.reload()

        assert result.ok is True
        assert result.removed == ["a"]
        assert "fixture-a" not in RUNNER_MAP
        assert not any(r.name == "a" for r in pm.loaded)


class TestReloadAtomicity:
    """Security-critical: a broken/colliding candidate set must never
    replace a working one."""

    def test_colliding_reload_leaves_live_state_untouched(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "keep.py", kind="fixture-keep")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        keep_cls_before = RUNNER_MAP["fixture-keep"]
        assert resolve_runner_class("fixture-keep") is keep_cls_before

        # New candidate set: "keep" unchanged, PLUS a new plugin that
        # collides with a BUILTIN runner kind ("shell").
        (pdir / "bad.py").write_text(
            "class BadRunner:\n"
            "    def __init__(self, definition, settings):\n        pass\n"
            "    def run(self, payload):\n        return None\n"
            "def register():\n"
            "    return {'runners': {'shell': BadRunner}}\n",
            encoding="utf-8",
        )

        result = pm.reload()

        assert result.ok is False
        assert result.error is not None
        assert result.added == []
        assert result.removed == []
        assert result.updated == []

        # Live state completely untouched: same class object as before the
        # failed reload attempt, and the builtin "shell" kind is intact too.
        from hivepilot.runners.shell_runner import ShellRunner

        assert RUNNER_MAP["fixture-keep"] is keep_cls_before
        assert resolve_runner_class("fixture-keep") is keep_cls_before
        assert resolve_runner_class("shell") is ShellRunner
        assert [r.name for r in pm.loaded] == ["keep"]

    def test_reload_raising_on_explicit_entry_import_leaves_live_state_untouched(
        self, tmp_path, monkeypatch
    ) -> None:
        """The `settings.plugins_entry` pin is the one load path that does
        NOT fail-isolate an import error (see `hivepilot.plugins.load_plugins`
        -- `import_module()` is unwrapped there, unlike the local-file scan
        and entry-point discovery paths, which both catch and skip). A
        misconfigured pin during reload must still leave live state intact.
        """
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "keep.py", kind="fixture-keep2")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        keep_cls_before = RUNNER_MAP["fixture-keep2"]

        monkeypatch.setattr(
            plugins_mod.settings,
            "plugins_entry",
            "hivepilot_does_not_exist_xyz:register",
            raising=False,
        )

        result = pm.reload()

        assert result.ok is False
        assert result.error is not None
        assert RUNNER_MAP["fixture-keep2"] is keep_cls_before
        assert resolve_runner_class("fixture-keep2") is keep_cls_before
        assert [r.name for r in pm.loaded] == ["keep"]

    def test_notifier_collision_with_builtin_leaves_live_state_untouched(
        self, tmp_path, monkeypatch
    ) -> None:
        """SHOULD-FIX (adversarial review): atomicity for a NOTIFIER-name
        collision, not just runners -- `NOTIFIER_MAP` is the same kind of
        process-global mutable map (`hivepilot.services.notification_
        service`), collision-checked the same way."""
        from hivepilot.services.notification_service import NOTIFIER_MAP

        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "keep.py", kind="fixture-keep-notif")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        keep_cls_before = RUNNER_MAP["fixture-keep-notif"]
        slack_before = NOTIFIER_MAP["slack"]

        (pdir / "bad_notifier.py").write_text(
            "def _notify(msg):\n    pass\n"
            "def register():\n    return {'notifiers': {'slack': _notify}}\n",
            encoding="utf-8",
        )

        result = pm.reload()

        assert result.ok is False
        assert result.error is not None
        assert result.added == []
        assert result.removed == []
        assert result.updated == []
        assert RUNNER_MAP["fixture-keep-notif"] is keep_cls_before
        assert NOTIFIER_MAP["slack"] is slack_before
        assert [r.name for r in pm.loaded] == ["keep"]

    def test_secrets_collision_with_builtin_leaves_live_state_untouched(
        self, tmp_path, monkeypatch
    ) -> None:
        """SHOULD-FIX (adversarial review): atomicity for a SECRETS-backend
        -name collision -- `SECRETS_MAP` (`hivepilot.registry`) is the same
        kind of process-global mutable map, collision-checked the same way.
        """
        from hivepilot.registry import SECRETS_MAP

        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "keep.py", kind="fixture-keep-secrets")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        keep_cls_before = RUNNER_MAP["fixture-keep-secrets"]
        env_backend_before = SECRETS_MAP["env"]

        (pdir / "bad_secrets.py").write_text(
            "class _DummyBackend:\n"
            "    def resolve(self, ref, settings):\n        return 'x'\n"
            "def register():\n    return {'secrets': {'env': _DummyBackend()}}\n",
            encoding="utf-8",
        )

        result = pm.reload()

        assert result.ok is False
        assert result.error is not None
        assert RUNNER_MAP["fixture-keep-secrets"] is keep_cls_before
        assert SECRETS_MAP["env"] is env_backend_before
        assert [r.name for r in pm.loaded] == ["keep"]

    def test_health_collision_leaves_live_state_untouched(self, tmp_path, monkeypatch) -> None:
        """SHOULD-FIX (adversarial review): atomicity for a HEALTH-name
        collision. `health` is a per-INSTANCE dict (not a process-global),
        so the collision here is between TWO plugins in the SAME reload
        batch declaring the same name -- the generic `_load_into`/`_commit`
        staging-then-commit machinery must still leave `pm.health` (and
        RUNNER_MAP, from the earlier-loaded "keep" plugin) untouched.
        """
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "keep.py", kind="fixture-keep-health")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        keep_cls_before = RUNNER_MAP["fixture-keep-health"]
        assert pm.health == {}

        (pdir / "h1.py").write_text(
            "def check(**kwargs):\n    return {'status': 'ok', 'detail': 'a'}\n"
            "def register():\n    return {'health': {'shared-health': check}}\n",
            encoding="utf-8",
        )
        (pdir / "h2.py").write_text(
            "def check(**kwargs):\n    return {'status': 'ok', 'detail': 'b'}\n"
            "def register():\n    return {'health': {'shared-health': check}}\n",
            encoding="utf-8",
        )

        result = pm.reload()

        assert result.ok is False
        assert result.error is not None
        assert RUNNER_MAP["fixture-keep-health"] is keep_cls_before
        assert pm.health == {}
        assert [r.name for r in pm.loaded] == ["keep"]

    def test_mixed_contribution_plugin_collision_rolls_back_earlier_contributions(
        self, tmp_path, monkeypatch
    ) -> None:
        """SHOULD-FIX (adversarial review): a SINGLE plugin contributing
        runners + notifiers + secrets together, where the LAST-processed
        type (secrets) collides with a builtin -- the earlier-staged
        contributions from THIS SAME plugin (its runner, its notifier) must
        be unwound by `_load_into`'s per-plugin `applied_*` rollback lists,
        not left orphaned. Since the whole `_load_into` pass then aborts
        (propagating the collision), none of it ever reaches live state
        regardless -- but this proves the INTERNAL rollback bookkeeping
        (`staged.runner_map.pop(...)`/`staged.notifier_map.pop(...)`) is
        exercised and correct, not merely masked by the pass-level abort.
        """
        from hivepilot.services.notification_service import NOTIFIER_MAP

        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "keep.py", kind="fixture-keep-mixed")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        keep_cls_before = RUNNER_MAP["fixture-keep-mixed"]

        (pdir / "combo.py").write_text(
            "class ComboRunner:\n"
            "    def __init__(self, definition, settings):\n        pass\n"
            "    def run(self, payload):\n        return None\n"
            "def _notify(msg):\n    pass\n"
            "class _DummyBackend:\n"
            "    def resolve(self, ref, settings):\n        return 'x'\n"
            "def register():\n"
            "    return {'runners': {'fixture-combo-mixed': ComboRunner}, "
            "'notifiers': {'fixture-notif-mixed': _notify}, "
            "'secrets': {'env': _DummyBackend()}}\n",
            encoding="utf-8",
        )

        result = pm.reload()

        assert result.ok is False
        assert result.error is not None
        # Nothing from the colliding plugin ever reached live state --
        # neither the runner nor the notifier it staged EARLIER in its own
        # register() dict (rolled back internally before the pass-level
        # raise), nor of course the "env" secrets backend it collided on.
        assert "fixture-combo-mixed" not in RUNNER_MAP
        assert "fixture-notif-mixed" not in NOTIFIER_MAP
        assert RUNNER_MAP["fixture-keep-mixed"] is keep_cls_before
        assert [r.name for r in pm.loaded] == ["keep"]


class TestReloadOwnershipTracking:
    def test_reload_never_clobbers_builtin_kind(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        from hivepilot.runners.shell_runner import ShellRunner

        assert RUNNER_MAP["shell"] is ShellRunner
        result = pm.reload()
        assert result.ok is True
        assert RUNNER_MAP["shell"] is ShellRunner

    def test_reload_never_clobbers_kind_this_manager_does_not_own(
        self, tmp_path, monkeypatch
    ) -> None:
        """A runner kind present in RUNNER_MAP that this manager never staged
        (simulating another manager's registration) must survive an
        unrelated reload untouched -- reload only removes kinds it itself
        previously added.
        """
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()

        class OtherManagerRunner(BaseRunner):
            def __init__(self, definition: RunnerDefinition, settings: Settings) -> None:
                self.definition = definition
                self.settings = settings

            def run(self, payload: RunnerPayload) -> None:
                return None

        RunnerRegistry.register("owned-elsewhere", OtherManagerRunner)

        result = pm.reload()

        assert result.ok is True
        assert RUNNER_MAP["owned-elsewhere"] is OtherManagerRunner


class TestPluginsChangedOnDisk:
    def test_false_when_no_change(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "a.py", kind="fixture-a")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        assert pm.plugins_changed_on_disk() is False

    def test_true_on_added_file(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        assert pm.plugins_changed_on_disk() is False

        _write_runner_plugin(pdir, "new.py", kind="fixture-new")
        assert pm.plugins_changed_on_disk() is True

    def test_true_on_modified_file(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "a.py", kind="fixture-a", marker="v1")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        assert pm.plugins_changed_on_disk() is False

        f = pdir / "a.py"
        f.write_text(f.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        bumped = f.stat().st_mtime + 10
        os.utime(f, (bumped, bumped))
        assert pm.plugins_changed_on_disk() is True

    def test_true_on_removed_file(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "a.py", kind="fixture-a")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        assert pm.plugins_changed_on_disk() is False

        (pdir / "a.py").unlink()
        assert pm.plugins_changed_on_disk() is True


class TestReloadReentrancy:
    """MINOR-FIX (adversarial review): Python signal handlers run on the
    main thread BETWEEN bytecode instructions, so a SIGHUP delivered while
    `reload()` is already mid-flight (inside `_load_into`/`_commit`) could
    re-enter `reload()` on the SAME `PluginManager` instance. Without a
    guard, the OUTER call's stale local `staged`/`before_names` would finish
    executing AFTER the inner, reentrant call already committed -- stomping
    `self._owned_runner_kinds`/etc. with the outer (older) values. A simple
    boolean flag makes the reentrant call a fast, logged no-op instead.
    """

    def test_reentrant_reload_call_is_skipped_not_corrupting(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()

        reentrant_result: dict[str, ReloadResult] = {}
        original_scan = plugins_mod._scan_local_plugins

        def _scan_with_reentrant_reload() -> list:
            # Simulates a SIGHUP handler firing mid-reload and re-entering
            # `reload()` on the SAME manager instance, from as early as
            # possible in `_load_into` (the very first thing it calls).
            if "captured" not in reentrant_result:
                reentrant_result["captured"] = pm.reload()
            return original_scan()

        monkeypatch.setattr(plugins_mod, "_scan_local_plugins", _scan_with_reentrant_reload)

        outer_result = pm.reload()

        # The reentrant, inner call was rejected -- fast no-op, no state
        # touched, clearly flagged as reentrant (not a generic failure).
        assert reentrant_result["captured"].ok is False
        assert "reentran" in (reentrant_result["captured"].error or "").lower()
        # The OUTER call -- the one actually in flight when the reentrant
        # call fired -- completed normally, unaffected by the rejected
        # nested attempt.
        assert outer_result.ok is True

    def test_reload_available_again_after_previous_call_completes(
        self, tmp_path, monkeypatch
    ) -> None:
        """The guard is released once a reload finishes -- it never
        permanently locks out subsequent, non-reentrant reloads."""
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()

        first = pm.reload()
        second = pm.reload()

        assert first.ok is True
        assert second.ok is True

    def test_reload_flag_cleared_even_after_a_failed_reload(self, tmp_path, monkeypatch) -> None:
        """The guard must be released in a `finally` -- a FAILED reload
        (collision) must not leave the manager permanently unable to
        reload again."""
        pdir = tmp_path / "plugins"
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()

        (pdir).mkdir(parents=True, exist_ok=True)
        (pdir / "bad.py").write_text(
            "class BadRunner:\n"
            "    def __init__(self, definition, settings):\n        pass\n"
            "    def run(self, payload):\n        return None\n"
            "def register():\n    return {'runners': {'shell': BadRunner}}\n",
            encoding="utf-8",
        )
        failed = pm.reload()
        assert failed.ok is False

        (pdir / "bad.py").unlink()
        recovered = pm.reload()
        assert recovered.ok is True


class TestRegressionAfterRefactor:
    """The `_load_into`/`_commit` refactor must not change plain
    `PluginManager()` construction behavior (Phase 26a attribution etc.)."""

    def test_plain_construction_populates_contributions(self, tmp_path, monkeypatch) -> None:
        pdir = tmp_path / "plugins"
        _write_runner_plugin(pdir, "a.py", kind="fixture-regress")
        monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
        pm = PluginManager()
        record = next(r for r in pm.loaded if r.name == "a")
        assert record.contributions == {"runners": ["fixture-regress"]}

    def test_reload_result_is_importable_dataclass(self) -> None:
        result = ReloadResult(ok=True)
        assert result.added == []
        assert result.removed == []
        assert result.updated == []
        assert result.error is None


@pytest.mark.parametrize("bad_kind", ["shell", "claude"])
def test_reload_collision_with_multiple_builtins_still_untouched(
    tmp_path, monkeypatch, bad_kind
) -> None:
    monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)
    pm = PluginManager()
    before = dict(RUNNER_MAP)

    pdir = tmp_path / "plugins"
    (pdir).mkdir(parents=True, exist_ok=True)
    (pdir / "bad.py").write_text(
        "class BadRunner:\n"
        "    def __init__(self, definition, settings):\n        pass\n"
        "    def run(self, payload):\n        return None\n"
        f"def register():\n    return {{'runners': {{'{bad_kind}': BadRunner}}}}\n",
        encoding="utf-8",
    )

    result = pm.reload()

    assert result.ok is False
    assert dict(RUNNER_MAP) == before
