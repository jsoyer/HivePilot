"""A runner that cannot restrict must refuse, not run unrestricted.

`PromptCliRunner` contains **0** references to `permission_mode` or
`allowed_tools`. `claude_runner` contains **38**. Ten agent CLI plugins
therefore discarded a role's tool whitelist in silence — and passed the CLI's
auto-approve flag while doing it, so on every one of them the most permissive
option available was chosen.

`_role_runner_options`' own docstring records what that costs, measured
2026-08-04: a review dispatch built without options ran 23 bare Bash commands —
`ls -la /`, `find / -maxdepth 6`, `cat …` — across the whole filesystem and
burned 1 099 726 cache tokens on a 3.8 KB diff. It never once ran `gh pr diff`.

The binaries were never the limitation. `pi` has `--tools`/`--exclude-tools`/
`--no-tools`, `gemini` has `--allowed-tools` and `--sandbox`, `vibe` has
`--enabled-tools`/`--disabled-tools`, `codex` has `-s/--sandbox`. We simply
never passed them.

Until they are wired, the honest answer is a refusal. An unrestricted agent is
not a degraded outcome — it is a different one, and not the one asked for.
"""

from __future__ import annotations

import pytest

from hivepilot.runners.base import (
    RunnerControlUnsupportedError,
    assert_runner_honours,
)

RESTRICTED = {
    "allowed_tools": ["Bash(rtk git:*)", "Read(./**)"],
    "permission_mode": "bypassPermissions",
}


class _Honours:
    honoured_controls = frozenset({"permission_mode", "allowed_tools"})


class _HonoursNothing:
    pass


class _HonoursHalf:
    honoured_controls = frozenset({"permission_mode"})


class TestItRefusesWhatItCannotApply:
    def test_a_restricted_role_on_a_runner_that_drops_it_raises(self):
        with pytest.raises(RunnerControlUnsupportedError, match="allowed_tools"):
            assert_runner_honours("pi", _HonoursNothing, RESTRICTED)

    def test_the_message_names_the_runner_and_what_is_missing(self):
        """An operator reading this must know which role to move and where.
        A bare 'unsupported' would send them into the source."""
        with pytest.raises(RunnerControlUnsupportedError) as exc:
            assert_runner_honours("opencode", _HonoursNothing, RESTRICTED)

        message = str(exc.value)
        assert "opencode" in message
        assert "allowed_tools" in message and "permission_mode" in message

    def test_partial_support_still_refuses_the_part_it_drops(self):
        """The dangerous shape: a runner that applies the permission mode and
        silently ignores the whitelist would look like it was honouring the
        role."""
        with pytest.raises(RunnerControlUnsupportedError, match="allowed_tools"):
            assert_runner_honours("half", _HonoursHalf, RESTRICTED)

    def test_a_runner_declaring_nothing_is_treated_as_honouring_nothing(self):
        """Default is empty, and that default is the design: a NEW runner
        must opt in. Opting in without wiring the flag is the one way to make
        this lie."""
        assert not getattr(_HonoursNothing, "honoured_controls", frozenset())

        with pytest.raises(RunnerControlUnsupportedError):
            assert_runner_honours("new", _HonoursNothing, {"allowed_tools": ["Read(./**)"]})


class TestItDoesNotRefuseWhatIsFine:
    """The discriminating half. A guard that always blocks passes every
    blocking test — these are the cases it must let through."""

    def test_a_runner_that_honours_both_passes(self):
        assert_runner_honours("claude", _Honours, RESTRICTED)

    def test_a_role_with_no_controls_runs_anywhere(self):
        """Most steps set neither. They must reach every runner exactly as
        before — this guard adds no new failure for them."""
        assert_runner_honours("shell", _HonoursNothing, {})
        assert_runner_honours("terraform", _HonoursNothing, None)

    def test_unrelated_options_are_not_controls(self):
        """`options` carries model flags, api providers, cli_flags… Only the
        two safety controls are gated."""
        assert_runner_honours(
            "opencode", _HonoursNothing, {"mode": "api", "api_model": "x", "cli_flags": ["-q"]}
        )

    @pytest.mark.parametrize("empty", [[], "", None])
    def test_an_empty_control_is_not_a_demand(self, empty):
        """An absent grant yields an ABSENT key by `_role_runner_options`'
        contract — an empty `allowed_tools` is a whitelist matching nothing.
        Either way it is not a role asking to be restricted, so it must not
        trip the guard."""
        assert_runner_honours("pi", _HonoursNothing, {"allowed_tools": empty})


class TestTheRealRunnersDeclareHonestly:
    def test_claude_declares_both_and_wires_both(self):
        """Declaring without wiring is the failure mode this whole change
        exists to end, so the claim is checked against the code."""
        import inspect

        from hivepilot.runners.claude_runner import ClaudeRunner

        assert ClaudeRunner.honoured_controls == frozenset({"permission_mode", "allowed_tools"})
        src = inspect.getsource(ClaudeRunner)
        assert '"--allowed-tools"' in src
        assert '"--permission-mode"' in src

    def test_prompt_cli_runners_declare_nothing_because_they_apply_nothing(self):
        """When one of them learns `--tools` / `--allowed-tools` /
        `--enabled-tools`, it opts in HERE — and this test is what makes that
        a deliberate act rather than an accident."""
        import inspect

        from hivepilot.runners.prompt_cli_runner import PromptCliRunner

        assert not PromptCliRunner.honoured_controls

        src = inspect.getsource(PromptCliRunner)
        assert "allowed_tools" not in src, (
            "PromptCliRunner now references allowed_tools — if it applies it, "
            "declare it in honoured_controls; the declaration is the contract"
        )


class TestEveryDispatchPathIsGuarded:
    """Two entry points, `execute_definition` and `capture_definition`. One
    unguarded would leave a silent route open — and the silent route is the
    whole defect."""

    @pytest.mark.parametrize("method", ["execute_definition", "capture_definition"])
    def test_the_registry_checks_before_instantiating(self, method):
        import inspect

        from hivepilot import registry

        src = inspect.getsource(getattr(registry.RunnerRegistry, method))

        assert "assert_runner_honours" in src
        # BEFORE the runner is built and called: refusing after the process
        # has started is not refusing.
        assert src.index("assert_runner_honours") < src.rindex("runner_cls(definition")
