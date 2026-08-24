"""Roster presets — identity is Claude, mix overlays agent kinds only."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from hivepilot.models import RunnerDefinition, TaskConfig, TaskStep
from hivepilot.orchestrator import resolve_step_runner
from hivepilot.roles import Role
from hivepilot.services.policy_service import Policy
from hivepilot.services.roster_preset import (
    clear_roster_preset_cache,
    drop_unhonoured_bypass,
    load_roster_preset,
)


def _role(name: str, **kwargs: object) -> Role:
    defaults = dict(
        name=name,
        title=name,
        prompt_file=Path("x.md"),
        model_profile="coding",
        inputs=[],
        outputs=[],
        can_block=False,
        order=1,
        runner="claude",
    )
    defaults.update(kwargs)
    return Role(**defaults)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clear_preset_cache() -> Iterator[None]:
    clear_roster_preset_cache()
    yield
    clear_roster_preset_cache()


class _FakeRegistry:
    def __init__(self, definitions: dict[str, RunnerDefinition]) -> None:
        self._definitions = definitions

    def _definition_for(self, key: str) -> RunnerDefinition:
        return self._definitions[key]


def test_missing_claude_preset_is_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "roster_preset", "claude")
    preset = load_roster_preset()
    assert preset.name == "claude"
    assert preset.roles == {}


def test_unknown_preset_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "roster_preset", "does-not-exist")
    with pytest.raises(FileNotFoundError, match="does-not-exist"):
        load_roster_preset()


def test_mix_preset_loads_roles_and_judge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hivepilot.config import settings

    preset_dir = tmp_path / "roster-presets"
    preset_dir.mkdir()
    (preset_dir / "mix.yaml").write_text(
        yaml.safe_dump(
            {
                "roles": {
                    "developer": {"runner": "cursor", "model": "gpt-5.3-codex-high"},
                    "reviewer": {"runner": "grok", "model": "grok-4.6"},
                },
                "judge": {"runner": "grok", "model": "grok-4.6"},
                "lessons": {"runner": "grok", "model": "grok-4.6"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "roster_preset", "mix")
    preset = load_roster_preset()
    assert preset.override_for("developer")["runner"] == "cursor"
    assert preset.judge_runner == "grok"
    assert preset.lesson_distill_runner == "grok"


def test_preset_flips_named_claude_ref_to_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy/preset grok|cursor must not keep spawning `command: claude`."""
    import hivepilot.roles as roles_mod

    monkeypatch.setattr(
        roles_mod,
        "ROLES",
        {"developer": _role("developer", model="sonnet", permission_mode="bypassPermissions")},
    )
    from hivepilot.services import roster_preset as rp

    monkeypatch.setattr(
        rp,
        "load_roster_preset",
        lambda name=None: rp.RosterPreset(
            name="mix",
            roles={"developer": {"runner": "cursor", "model": "gpt-5.3-codex-high"}},
        ),
    )
    registry = _FakeRegistry(
        {
            "claude-refactor": RunnerDefinition(
                name="claude-refactor",
                kind="claude",
                command="claude",
                options={"profile": "coding"},
            )
        }
    )
    step = TaskStep(name="implementation", runner="claude", runner_ref="claude-refactor")
    task = TaskConfig(description="t", role="developer", steps=[step])
    _key, runner_def = resolve_step_runner(task=task, step=step, registry=registry, policy=None)
    assert runner_def.kind == "cursor"
    assert runner_def.command is None
    assert runner_def.model == "gpt-5.3-codex-high"
    assert "permission_mode" not in runner_def.options


def test_shell_step_stays_shell_under_mix_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    import hivepilot.roles as roles_mod

    monkeypatch.setattr(
        roles_mod,
        "ROLES",
        {"ciso": _role("ciso", model="opus")},
    )
    from hivepilot.services import roster_preset as rp

    monkeypatch.setattr(
        rp,
        "load_roster_preset",
        lambda name=None: rp.RosterPreset(
            name="mix", roles={"ciso": {"runner": "grok", "model": "grok-4.6"}}
        ),
    )
    registry = _FakeRegistry(
        {
            "validation-suite": RunnerDefinition(
                name="validation-suite", kind="shell", command="pytest"
            )
        }
    )
    step = TaskStep(name="local validation", runner="shell", runner_ref="validation-suite")
    task = TaskConfig(description="t", role="ciso", steps=[step])
    _key, runner_def = resolve_step_runner(task=task, step=step, registry=registry, policy=None)
    assert runner_def.kind == "shell"
    assert runner_def.command == "pytest"


def test_identity_preset_keeps_claude_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import hivepilot.roles as roles_mod

    monkeypatch.setattr(
        roles_mod,
        "ROLES",
        {"ciso": _role("ciso", model="opus")},
    )
    from hivepilot.services import roster_preset as rp

    monkeypatch.setattr(rp, "load_roster_preset", lambda name=None: rp.RosterPreset(name="claude"))
    registry = _FakeRegistry(
        {
            "claude-docs": RunnerDefinition(
                name="claude-docs",
                kind="claude",
                command="claude",
                options={"profile": "automation"},
            )
        }
    )
    step = TaskStep(name="propose", runner="claude", runner_ref="claude-docs")
    task = TaskConfig(description="t", role="ciso", steps=[step])
    _key, runner_def = resolve_step_runner(task=task, step=step, registry=registry, policy=None)
    assert runner_def.kind == "claude"
    assert runner_def.command == "claude"
    assert runner_def.options.get("profile") == "automation"


def test_policy_outranks_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    import hivepilot.roles as roles_mod
    from hivepilot.roles import resolve_runner

    monkeypatch.setattr(
        roles_mod,
        "ROLES",
        {"developer": _role("developer", model="sonnet")},
    )
    from hivepilot.services import roster_preset as rp

    monkeypatch.setattr(
        rp,
        "load_roster_preset",
        lambda name=None: rp.RosterPreset(
            name="mix",
            roles={"developer": {"runner": "cursor", "model": "gpt-5.3-codex-high"}},
        ),
    )
    policy = Policy(role_overrides={"developer": {"runner": "grok", "model": "grok-4.6"}})
    runner, model, _effort = resolve_runner("developer", policy)
    assert runner == "grok"
    assert model == "grok-4.6"


def test_drop_bypass_only_on_print_full_tools() -> None:
    dropped = drop_unhonoured_bypass("cursor", {"permission_mode": "bypassPermissions"})
    assert "permission_mode" not in dropped
    kept = drop_unhonoured_bypass("grok", {"permission_mode": "bypassPermissions"})
    assert kept["permission_mode"] == "bypassPermissions"
    tools = drop_unhonoured_bypass("cursor", {"allowed_tools": ["Read(./**)"]})
    assert tools["allowed_tools"] == ["Read(./**)"]
