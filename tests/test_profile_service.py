"""Tests for hivepilot.services.profile_service.

Verifies that model_profiles.yaml is resolved via the XDG/config_repo-aware
`settings.resolve_config_path`, not the cwd-only `settings.resolve_path`, so
an external config repo override is honored.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hivepilot.config import Settings
from hivepilot.services import profile_service


def test_load_claude_profiles_uses_resolve_config_path(tmp_path: Path, monkeypatch) -> None:
    """load_claude_profiles must resolve its file through resolve_config_path
    so an XDG/config-repo override wins over the cwd-relative path."""
    calls: list[Path | str] = []

    override_file = tmp_path / "model_profiles.yaml"
    override_file.write_text(
        yaml.safe_dump({"claude_profiles": {"default": {"model": "opus"}}}),
        encoding="utf-8",
    )

    def fake_resolve_config_path(self, filename):
        calls.append(filename)
        return override_file

    def fail_resolve_path(self, *a, **k):
        raise AssertionError("resolve_path should not be called")

    # Settings is a pydantic BaseSettings instance; instance attributes can't
    # be reassigned arbitrarily, so patch the methods on the class instead.
    monkeypatch.setattr(Settings, "resolve_config_path", fake_resolve_config_path)
    monkeypatch.setattr(Settings, "resolve_path", fail_resolve_path)

    profile_service._cache.clear()
    data = profile_service.load_claude_profiles()

    assert calls, "resolve_config_path was never called"
    assert data == {"default": {"model": "opus"}}


# ---------------------------------------------------------------------------
# Per-runner profiles (#28) — and the end of two silences.
#
# Measured on the box before this existed: load_claude_profiles() returned 0
# profiles while EIGHT noxys roles declared one, and the miss fell through to
# the role's `model:` without a word. Worse, measured in the source:
# `role.model_profile` was consumed NOWHERE in dispatch — resolve_runner read
# `role.model or role.models[0]` and the field was pure documentation. The
# profile system had never done anything, twice over.
# ---------------------------------------------------------------------------


class TestResolveProfileModel:
    @staticmethod
    def _with_profiles(monkeypatch, data):
        from hivepilot.services import profile_service

        monkeypatch.setattr(profile_service, "load_model_profiles", lambda: data)

    def test_a_per_runner_entry_resolves_for_its_runner(self, monkeypatch):
        """The point of the map: `model_profile: architecture` lands on the
        right model WHEREVER the role runs — cursor serves gpt-5 and
        sonnet-4-thinking through ONE runner, so a flat model cannot."""
        from hivepilot.services.profile_service import resolve_profile_model

        self._with_profiles(
            monkeypatch,
            {"architecture": {"claude": "opus", "grok": "grok-4.6"}},
        )

        assert resolve_profile_model("architecture", "claude") == "opus"
        assert resolve_profile_model("architecture", "grok") == "grok-4.6"

    def test_a_legacy_model_key_counts_as_claude_only(self, monkeypatch):
        """Legacy semantics were claude-only — only ClaudeRunner ever
        consulted profiles, and values like `opus` are claude vocabulary.
        Treating them as any-runner would hand grok a model name it cannot
        serve."""
        from hivepilot.services.profile_service import resolve_profile_model

        self._with_profiles(monkeypatch, {"coding": {"model": "sonnet"}})

        assert resolve_profile_model("coding", "claude") == "sonnet"
        assert resolve_profile_model("coding", "grok") is None

    def test_a_missing_profile_warns_and_returns_none(self, monkeypatch, caplog):
        """The first silence. Eight roles believed themselves profiled for
        months; the fallback is legitimate, the silence was not."""
        import logging

        from hivepilot.services.profile_service import resolve_profile_model

        self._with_profiles(monkeypatch, {})

        with caplog.at_level(logging.WARNING):
            result = resolve_profile_model("architecture", "claude")

        assert result is None
        assert any("architecture" in r.message for r in caplog.records)

    def test_a_profile_without_this_runner_warns_naming_both(self, monkeypatch, caplog):
        """The mixed-fleet miss: the profile exists, this runner has no entry.
        The operator must learn WHICH profile and WHICH runner, or the warning
        sends them into the yaml blind."""
        import logging

        from hivepilot.services.profile_service import resolve_profile_model

        self._with_profiles(monkeypatch, {"architecture": {"claude": "opus"}})

        with caplog.at_level(logging.WARNING):
            result = resolve_profile_model("architecture", "grok")

        assert result is None
        assert any("architecture" in r.message and "grok" in r.message for r in caplog.records)

    def test_no_profile_declared_is_not_a_warning(self, monkeypatch, caplog):
        """Fourteen roles declare none. Silence is correct for them — a
        warning that fires on every dispatch is the typecheck-hook lesson."""
        import logging

        from hivepilot.services.profile_service import resolve_profile_model

        self._with_profiles(monkeypatch, {})

        with caplog.at_level(logging.WARNING):
            assert resolve_profile_model(None, "claude") is None
            assert resolve_profile_model("", "claude") is None

        assert not caplog.records


class TestTheLoaderReadsBothSpellings:
    def test_the_generic_key_is_read(self, tmp_path, monkeypatch):
        from hivepilot.config import settings
        from hivepilot.services import profile_service

        (tmp_path / "model_profiles.yaml").write_text(
            "model_profiles:\n  architecture:\n    grok: grok-4.6\n", encoding="utf-8"
        )
        monkeypatch.setattr(settings, "base_dir", tmp_path, raising=False)
        profile_service._cache.clear()

        assert profile_service.load_model_profiles()["architecture"]["grok"] == "grok-4.6"

    def test_the_legacy_claude_key_still_loads(self, tmp_path, monkeypatch):
        """Every deployed model_profiles.yaml says `claude_profiles:` — the
        init template wrote it. Dropping the key would silently zero their
        profiles, which is exactly the defect being fixed."""
        from hivepilot.config import settings
        from hivepilot.services import profile_service

        (tmp_path / "model_profiles.yaml").write_text(
            "claude_profiles:\n  coding:\n    model: sonnet\n", encoding="utf-8"
        )
        monkeypatch.setattr(settings, "base_dir", tmp_path, raising=False)
        profile_service._cache.clear()

        assert profile_service.load_model_profiles()["coding"]["model"] == "sonnet"

    def test_load_claude_profiles_remains_as_an_alias(self, tmp_path, monkeypatch):
        """cli.py and claude_runner import it by this name; the alias keeps
        every caller working while the generic name becomes the real one."""
        from hivepilot.services import profile_service

        assert profile_service.load_claude_profiles is not None


class TestRoleModelProfileFinallyReachesDispatch:
    """The second silence, and the bigger one: `role.model_profile` was
    consumed nowhere. These drive the REAL resolve_runner."""

    @staticmethod
    def _role(monkeypatch, **kw):
        from hivepilot import roles as roles_mod

        defaults = dict(
            name="dev",
            title="D",
            prompt_file="p.md",
            inputs=[],
            outputs=[],
            order=1,
            can_block=False,
            runner="claude",
            model_profile="",
        )
        defaults.update(kw)
        role = roles_mod.Role(**defaults)
        # `resolve_runner` reads ROLES[role_name] directly — patching
        # `get_role` targets a different lookup and proves nothing.
        monkeypatch.setitem(roles_mod.ROLES, "dev", role)
        return role

    def test_a_resolvable_profile_outranks_the_roles_flat_model(self, monkeypatch):
        """Declaring `model_profile` MEANS "resolve per runner". The flat
        `model:` stays as the fallback, not the winner — otherwise the eight
        roles' declarations stay dead even after the yaml exists."""
        from hivepilot import roles as roles_mod
        from hivepilot.services import profile_service

        self_p = {"architecture": {"claude": "opus-latest"}}
        monkeypatch.setattr(profile_service, "load_model_profiles", lambda: self_p)
        self._role(monkeypatch, runner="claude", model="sonnet", model_profile="architecture")

        _runner, model, _effort = roles_mod.resolve_runner("dev", None)

        assert model == "opus-latest"

    def test_an_unresolvable_profile_falls_back_to_the_flat_model_with_a_warning(
        self, monkeypatch, caplog
    ):
        """Byte-identical to today's behaviour for the eight roles — except it
        SAYS so now."""
        import logging

        from hivepilot import roles as roles_mod
        from hivepilot.services import profile_service

        monkeypatch.setattr(profile_service, "load_model_profiles", lambda: {})
        self._role(monkeypatch, runner="claude", model="sonnet", model_profile="architecture")

        with caplog.at_level(logging.WARNING):
            _runner, model, _effort = roles_mod.resolve_runner("dev", None)

        assert model == "sonnet"
        assert any("architecture" in r.message for r in caplog.records)

    def test_a_role_without_a_profile_is_untouched(self, monkeypatch):
        from hivepilot import roles as roles_mod

        self._role(monkeypatch, runner="claude", model="sonnet", model_profile="")

        _runner, model, _effort = roles_mod.resolve_runner("dev", None)

        assert model == "sonnet"
