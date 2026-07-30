"""A capability verdict is scoped to ONE plugin, not to the whole subsystem.

Before this change, `_load_into` caught `PluginCapabilityDeniedError` /
`PluginCapabilityInvalidError` in the same `except` as the `*CollisionError`s
and ended it with a bare `raise`. The error therefore propagated out of
`PluginManager()` construction, so a deployment that had not allowlisted a
capability lost its ENTIRE plugin subsystem to an unhandled traceback rather
than losing the one plugin that declared it.

That mattered in practice because `gh_enabled`/`rtk_enabled` are opt-OUT: once
those two plugins declared `subprocess`, every unconfigured deployment —
including a fresh install — could not even run `hivepilot plugins list`.

These tests pin the corrected shape:

* a capability verdict is LOGGED AND SKIPPED (the treatment an isolated broken
  plugin already gets at discovery time),
* the refused plugin still contributes NOTHING (fail-closed is unchanged — only
  the blast radius moved), and
* a kind/name COLLISION still propagates uncaught, because two plugins claiming
  one runner kind leaves an ambiguous system state that no local rollback can
  make coherent.

Every test drives a real on-disk plugin directory under `tmp_path`, because the
defect lived in the interaction between the per-plugin loop and its `except`
clause — a unit test against `validate_capabilities` alone would have passed
throughout.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from hivepilot.plugin_capabilities import PLUGIN_CAPABILITIES
from hivepilot.plugins import PluginManager

_DECLARES_SUBPROCESS = """
    from typing import Any


    def register() -> dict[str, Any]:
        return {
            "capabilities": ["subprocess"],
            "health": {"declarer": lambda **k: None},
        }
"""

_DECLARES_NOTHING = """
    from typing import Any


    def register() -> dict[str, Any]:
        return {"health": {"innocent": lambda **k: None}}
"""

_DECLARES_BOGUS_TOKEN = """
    from typing import Any


    def register() -> dict[str, Any]:
        return {
            "capabilities": ["telepathy"],
            "health": {"bogus": lambda **k: None},
        }
"""


def _write_plugins(root: Path, **modules: str) -> Path:
    """Materialise `name=source` plugins under `root/plugins/` and return root."""
    plugin_dir = root / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    for name, source in modules.items():
        (plugin_dir / f"{name}.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return root


@pytest.fixture
def isolated_plugins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point plugin discovery exclusively at `tmp_path`.

    Pins `plugins_extra_dirs` empty and `config_repo` unset too: once the
    auditor/loader read every root, a developer's real
    `~/.local/share/hivepilot/plugins` would otherwise contaminate these
    assertions.
    """
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "base_dir", tmp_path, raising=False)
    monkeypatch.setattr(settings, "plugins_extra_dirs", [], raising=False)
    monkeypatch.setattr(settings, "config_repo", None, raising=False)
    monkeypatch.setattr(settings, "plugins_enabled", True, raising=False)
    monkeypatch.setattr(settings, "plugins_disabled", [], raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    return tmp_path


def _deny_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "plugins_capability_policy", [], raising=False)


def _allow(monkeypatch: pytest.MonkeyPatch, *tokens: str) -> None:
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "plugins_capability_policy", list(tokens), raising=False)


class TestDenialDoesNotTakeDownTheSubsystem:
    """The core fix: one refused plugin must not cost the others."""

    def test_other_plugins_still_load_when_one_is_denied(
        self, isolated_plugins: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_plugins(
            isolated_plugins,
            declarer=_DECLARES_SUBPROCESS,
            innocent=_DECLARES_NOTHING,
        )
        _deny_everything(monkeypatch)

        manager = PluginManager()

        loaded = {record.name for record in manager.loaded}
        assert "innocent" in loaded, (
            "an unrelated plugin was lost to another plugin's capability verdict"
        )
        assert "declarer" not in loaded

    def test_constructing_the_manager_does_not_raise(
        self, isolated_plugins: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_plugins(isolated_plugins, declarer=_DECLARES_SUBPROCESS)
        _deny_everything(monkeypatch)

        # Before the fix this raised PluginCapabilityDeniedError straight out
        # of __init__, which is what bricked `hivepilot plugins list`.
        PluginManager()


class TestFailClosedIsUnchanged:
    """Only the blast radius moved. The verdict did not."""

    def test_denied_plugin_contributes_nothing(
        self, isolated_plugins: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_plugins(
            isolated_plugins,
            declarer=_DECLARES_SUBPROCESS,
            innocent=_DECLARES_NOTHING,
        )
        _deny_everything(monkeypatch)

        manager = PluginManager()

        assert "declarer" not in manager.health, "a refused plugin staged a health check"
        assert "innocent" in manager.health, "the surviving plugin lost its contribution"

    def test_denied_plugin_is_absent_from_loaded_records(
        self, isolated_plugins: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_plugins(isolated_plugins, declarer=_DECLARES_SUBPROCESS)
        _deny_everything(monkeypatch)

        manager = PluginManager()

        # Converting a crash into a record with populated `contributions`
        # would be a silent fail-OPEN, which is worse than the crash.
        for record in manager.loaded:
            assert record.name != "declarer"

    def test_allowlisting_the_token_lets_it_load_normally(
        self, isolated_plugins: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_plugins(isolated_plugins, declarer=_DECLARES_SUBPROCESS)
        _allow(monkeypatch, "subprocess")

        manager = PluginManager()

        assert "declarer" in {record.name for record in manager.loaded}
        assert "declarer" in manager.health
        assert manager.denied == []


class TestRefusalIsLegibleNotSilent:
    """A plugin that vanishes without explanation is the failure mode this
    repo keeps rediscovering, so the refusal must be reported."""

    def test_denied_plugin_is_recorded_with_its_reason(
        self, isolated_plugins: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_plugins(isolated_plugins, declarer=_DECLARES_SUBPROCESS)
        _deny_everything(monkeypatch)

        manager = PluginManager()

        denied = {record.name: reason for record, reason in manager.denied}
        assert "declarer" in denied, "a refused plugin was silently missing, not reported"
        assert "subprocess" in denied["declarer"]

    def test_denial_emits_a_warning_naming_plugin_and_tokens(
        self,
        isolated_plugins: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _write_plugins(isolated_plugins, declarer=_DECLARES_SUBPROCESS)
        _deny_everything(monkeypatch)

        with caplog.at_level("WARNING"):
            PluginManager()

        assert "plugins.capability_denied" in caplog.text
        assert "declarer" in caplog.text
        assert "subprocess" in caplog.text


class TestInvalidIsDistinctFromDenied:
    """Both are per-plugin skips, but they need DIFFERENT operator actions:
    allow the capability, versus fix the plugin. Same treatment, distinct
    events."""

    def test_a_token_outside_the_vocabulary_is_skipped_not_fatal(
        self, isolated_plugins: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_plugins(
            isolated_plugins,
            bogus=_DECLARES_BOGUS_TOKEN,
            innocent=_DECLARES_NOTHING,
        )
        _allow(monkeypatch, "subprocess")

        manager = PluginManager()

        assert "telepathy" not in PLUGIN_CAPABILITIES
        assert "innocent" in {record.name for record in manager.loaded}
        assert "bogus" not in {record.name for record in manager.loaded}
        assert "bogus" in {record.name for record, _ in manager.denied}

    def test_invalid_emits_its_own_event_not_the_denied_one(
        self,
        isolated_plugins: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _write_plugins(isolated_plugins, bogus=_DECLARES_BOGUS_TOKEN)
        _allow(monkeypatch, "subprocess")

        with caplog.at_level("WARNING"):
            PluginManager()

        assert "plugins.capability_invalid" in caplog.text
        # A plugin bug must not be reported as an operator policy problem.
        assert "plugins.capability_denied" not in caplog.text


class TestCollisionsStillHardStop:
    """Regression guard for the distinction the fix rests on: a collision
    leaves an AMBIGUOUS system state, so it must keep propagating."""

    def test_runner_kind_collision_still_propagates_uncaught(
        self, isolated_plugins: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        colliding = """
            from typing import Any


            class _R:
                pass


            def register() -> dict[str, Any]:
                return {"runners": {"claude": _R}}
        """
        _write_plugins(isolated_plugins, hijacker=colliding)
        _allow(monkeypatch, *PLUGIN_CAPABILITIES)

        from hivepilot.registry import RunnerKindCollisionError

        with pytest.raises(RunnerKindCollisionError):
            PluginManager()
