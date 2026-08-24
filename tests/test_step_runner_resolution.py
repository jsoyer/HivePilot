"""An explicit `runner_ref` on a step must survive its task having a role.

Before this, `if task.role:` synthesized a `RunnerDefinition` from the role
alone — `command=None`, `options` holding only `permission_mode` — and never
reached the branch that reads `step.runner_ref`. Every named runner a config
declared was silently discarded the moment its task gained a role.

In the noxys config that was **21 of 29 tasks**: every `profile` option
(`automation`, `architecture`, `coding`) dropped. The clearest damage was
`pentest`, whose `local validation` step declares `runner: shell,
runner_ref: validation-suite` — a test suite. Giving that task `role: ciso`
would have sent the test suite to Claude instead of running it.

The rule these tests pin: **the step's declared runner wins, and the role
overlays model/effort/permission only when both are the same kind.** A claude
role has nothing to say about a shell step.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.models import RunnerDefinition, TaskConfig, TaskStep
from hivepilot.orchestrator import resolve_step_runner


class _FakeRegistry:
    """Stands in for `RunnerRegistry`, which needs real config to build."""

    def __init__(self, definitions: dict[str, RunnerDefinition]) -> None:
        self._definitions = definitions

    def _definition_for(self, key: str) -> RunnerDefinition:
        try:
            return self._definitions[key]
        except KeyError:
            raise ValueError(f"unknown runner {key!r}") from None


@pytest.fixture
def registry() -> _FakeRegistry:
    return _FakeRegistry(
        {
            "claude-docs": RunnerDefinition(
                name="claude-docs",
                kind="claude",
                command="claude",
                options={"profile": "automation"},
            ),
            "validation-suite": RunnerDefinition(
                name="validation-suite",
                kind="shell",
                command="pytest",
            ),
            "claude": RunnerDefinition(name="claude", kind="claude", command="claude"),
            "shell": RunnerDefinition(name="shell", kind="shell"),
        }
    )


def _task(role: str | None, steps: list[TaskStep]) -> TaskConfig:
    return TaskConfig(description="t", role=role, steps=steps)


def _resolve(task: TaskConfig, step: TaskStep, registry: _FakeRegistry):
    return resolve_step_runner(
        task=task,
        step=step,
        registry=registry,
        policy=None,
        stage_model=None,
        stage_effort=None,
    )


def test_a_shell_step_stays_shell_under_a_claude_role(registry: _FakeRegistry) -> None:
    """The `pentest` case, and the one that makes this a bug rather than a
    preference: a declared test suite must not be handed to a model."""
    step = TaskStep(name="local validation", runner="shell", runner_ref="validation-suite")
    _key, runner_def = _resolve(_task("ciso", [step]), step, registry)

    assert runner_def.kind == "shell"
    assert runner_def.command == "pytest"


def test_a_bare_shell_step_stays_shell_under_a_role(registry: _FakeRegistry) -> None:
    """The same protection, for the other way a step declares itself.

    `groomer-scan`'s `signals` step is `runner: shell` with an inline
    `command` and NO `runner_ref` — the form `runner_ref` handling alone does
    not cover. Without this, giving that task a role (the open attribution
    gap) would send `hivepilot drift scan` to a model.

    `TaskStep.runner` is a required field, so it is always an explicit
    declaration — never a default standing in for one.
    """
    step = TaskStep(name="signals", runner="shell", command="hivepilot drift scan")
    key, runner_def = _resolve(_task("ciso", [step]), step, registry)

    assert runner_def.kind == "shell"
    assert key == "shell"


def test_a_model_step_still_follows_the_roles_runner(registry: _FakeRegistry) -> None:
    """The narrow rule must not gut role → runner mapping.

    Every step declares a `runner`, so honouring that declaration wholesale
    would leave a role's runner binding with nothing left to decide. Only
    kinds that *cannot* be agent work override it.
    """
    step = TaskStep(name="review", runner="claude")
    key, runner_def = _resolve(_task("ciso", [step]), step, registry)

    assert key == "ciso"
    assert runner_def.name == "role:ciso"


def test_a_named_runners_options_survive_the_role(
    registry: _FakeRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`profile: automation` is the whole reason a config names a runner.

    Dropping it left the step running under a different Claude profile than
    the pipeline author selected, with nothing on screen to say so.

    Pinned to a claude-kind role so this tests SAME-kind overlay (command
    kept), not the roster-preset agent-family switch.
    """
    import hivepilot.roles as roles_mod
    from hivepilot.roles import Role

    monkeypatch.setattr(
        roles_mod,
        "ROLES",
        {
            "ciso": Role(
                name="ciso",
                title="CISO",
                prompt_file=Path("ciso.md"),
                model_profile="architecture",
                inputs=[],
                outputs=[],
                can_block=True,
                order=1,
                runner="claude",
                model="opus",
            )
        },
    )
    step = TaskStep(name="propose", runner="claude", runner_ref="claude-docs")
    _key, runner_def = _resolve(_task("ciso", [step]), step, registry)

    assert runner_def.options.get("profile") == "automation"
    assert runner_def.kind == "claude"
    assert runner_def.command == "claude"


def test_a_step_with_no_runner_ref_still_follows_its_role(registry: _FakeRegistry) -> None:
    """Unchanged behaviour where nothing was declared.

    The role remains the authority whenever the step expresses no preference
    — this change narrows the role's reach, it does not remove it.
    """
    step = TaskStep(name="review", runner="claude")
    key, runner_def = _resolve(_task("ciso", [step]), step, registry)

    assert key == "ciso"
    assert runner_def.name == "role:ciso"


def test_a_roleless_task_is_untouched(registry: _FakeRegistry) -> None:
    step = TaskStep(name="propose", runner="claude", runner_ref="claude-docs")
    key, runner_def = _resolve(_task(None, [step]), step, registry)

    assert key == "claude-docs"
    assert runner_def.options.get("profile") == "automation"


def test_the_role_overlays_permission_mode_on_a_matching_kind(
    registry: _FakeRegistry,
) -> None:
    """A role's permission posture is a security decision and still applies
    to agent runners — it is only the *runner selection* that the step wins.

    Uses `developer`, the one default role that actually declares a
    `permission_mode`, rather than a stubbed role: this asserts against the
    real registry the orchestrator reads at runtime.
    """
    step = TaskStep(name="propose", runner="claude", runner_ref="claude-docs")
    _key, runner_def = _resolve(_task("developer", [step]), step, registry)

    assert runner_def.options.get("permission_mode") == "bypassPermissions"
    assert runner_def.options.get("profile") == "automation", "the profile must survive too"


def test_a_roles_allowed_tools_reach_the_runner(
    registry: _FakeRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grant a role declares must actually arrive.

    `claude_runner._resolve_allowed_tools` reads this option, and nothing on
    the execution path was setting it — only the pre-flight approval probe
    did, on a definition the executor then rebuilt without it. So a role could
    state exactly which tools it may use and the runner would never hear of
    it, which is why agents with a careful `allowed_tools` list still behaved
    as though they had none.

    Registered into `ROLES` rather than read from config: the default
    registry the test suite loads carries no `allowed_tools`, and asserting
    against whatever a deployment happens to configure would make this test
    pass or fail for reasons unrelated to the code.
    """
    from hivepilot import roles as roles_module

    granted = roles_module.Role(
        name="doc-grant",
        title="Documentation",
        prompt_file=Path("prompts/agents/doc.md"),
        model_profile="automation",
        inputs=[],
        outputs=["documentation"],
        can_block=False,
        order=99,
        runner="claude",
        allowed_tools=["Bash(rtk git:*)", "Bash(gh pr view:*)"],
    )
    monkeypatch.setitem(roles_module.ROLES, "doc-grant", granted)

    step = TaskStep(name="write docs", runner="claude", runner_ref="claude-docs")
    _key, runner_def = _resolve(_task("doc-grant", [step]), step, registry)

    assert runner_def.options.get("allowed_tools") == [
        "Bash(rtk git:*)",
        "Bash(gh pr view:*)",
    ], "the role's declared grant must arrive"
    assert runner_def.options.get("profile") == "automation", "and must not cost the profile"


def test_the_role_does_not_overlay_onto_a_different_kind(registry: _FakeRegistry) -> None:
    """A claude role's model/effort/permission mean nothing to a shell step.

    Stamping them on would be harmless-looking noise that makes a shell
    runner read as if an agent had configured it — and `bypassPermissions`
    in particular has no business appearing on a plain shell command.
    """
    step = TaskStep(name="local validation", runner="shell", runner_ref="validation-suite")
    _key, runner_def = _resolve(_task("developer", [step]), step, registry)

    assert "permission_mode" not in runner_def.options
    assert runner_def.model is None
