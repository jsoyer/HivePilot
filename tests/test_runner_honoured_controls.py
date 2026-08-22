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

        # `getattr` with a default, because `honoured_controls` is NOT a
        # Protocol member — a runner that says nothing is the safe, and here
        # the correct, answer. See `_ROLE_CONTROLS` in runners/base.py.
        assert not getattr(PromptCliRunner, "honoured_controls", frozenset())

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


class TestPermissionModeOnPromptCliRunners:
    """#30's honest half. Static extraction settled the rest without probes:

    `allowed_tools` stays unhonourable off the claude path — pi and vibe
    restrict by tool NAME (`bash`), so translating `Bash(rtk git:*)` would
    grant the whole shell: WIDER than the role asked, worse than refusing.
    gemini's `--allowed-tools` is deprecated AND inverted ("allowed to run
    without confirmation" — an auto-approve list). codex's `-s` is sandbox
    confinement, a different axis.

    `permission_mode` IS honourable on pi and vibe: their approve flags are
    exactly that semantic, and until now they were hardcoded ON — the
    engine auto-approved for every caller regardless of what the role said.
    """

    @staticmethod
    def _args_for(runner_cls, permission_mode, tmp_path):
        from hivepilot.config import settings
        from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
        from hivepilot.runners.base import RunnerPayload

        pf = tmp_path / "p.md"
        pf.write_text("do it", encoding="utf-8")
        options = {"permission_mode": permission_mode} if permission_mode else {}
        definition = RunnerDefinition(name="r", kind="shell", command=None, options=options)
        payload = RunnerPayload(
            project_name="p",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(name="s", runner="shell", prompt_file=str(pf)),
            metadata={},
            secrets={},
        )
        runner = runner_cls(definition, settings)
        return runner._build_cli_args(payload, "do it")

    def test_pi_bypass_emits_approve(self, tmp_path):
        from hivepilot.runners.prompt_cli_runner import PiRunner

        args = self._args_for(PiRunner, "bypassPermissions", tmp_path)

        assert "--approve" in args
        assert "--no-approve" not in args

    def test_pi_without_bypass_emits_no_approve(self, tmp_path):
        """The defect this ends: `--approve` was HARDCODED, so every caller
        got auto-approval whatever the role said. `--no-approve` exists in
        pi's help precisely for this."""
        from hivepilot.runners.prompt_cli_runner import PiRunner

        args = self._args_for(PiRunner, None, tmp_path)

        assert "--no-approve" in args
        assert "--approve" not in [a for a in args if a != "--no-approve"]

    def test_vibe_bypass_emits_auto_approve(self, tmp_path):
        from hivepilot.runners.prompt_cli_runner import VibeRunner

        args = self._args_for(VibeRunner, "bypassPermissions", tmp_path)

        assert "--auto-approve" in args

    def test_vibe_without_bypass_omits_it(self, tmp_path):
        """vibe has no explicit no-approve flag; omitting `--auto-approve` IS
        the safe mode — it then prompts, which in headless means refusing
        rather than silently acting."""
        from hivepilot.runners.prompt_cli_runner import VibeRunner

        args = self._args_for(VibeRunner, None, tmp_path)

        assert "--auto-approve" not in args

    def test_pi_and_vibe_now_declare_permission_mode_only(self):
        """Half a declaration, honestly: permission_mode is wired,
        allowed_tools is NOT (name-level flags cannot express command
        patterns without widening the grant). #569's partial-support test
        guarantees a role asking for both is still refused."""
        from hivepilot.runners.prompt_cli_runner import PiRunner, VibeRunner

        assert PiRunner.honoured_controls == frozenset({"permission_mode"})
        assert VibeRunner.honoured_controls == frozenset({"permission_mode"})

    def test_a_role_with_allowed_tools_is_still_refused_on_pi(self):
        """The widening trap, pinned: `Bash(rtk git:*)` has no faithful
        name-level translation, so the refusal must survive this change."""
        import pytest as _p

        from hivepilot.runners.base import RunnerControlUnsupportedError, assert_runner_honours
        from hivepilot.runners.prompt_cli_runner import PiRunner

        with _p.raises(RunnerControlUnsupportedError, match="allowed_tools"):
            assert_runner_honours(
                "pi",
                PiRunner,
                {"permission_mode": "bypassPermissions", "allowed_tools": ["Bash(rtk git:*)"]},
            )

    def test_other_prompt_cli_runners_are_untouched(self, tmp_path):
        """gemini/codex/opencode keep declaring nothing — their mechanisms
        (deprecated inverted flag; sandbox confinement) are not this control."""
        from hivepilot.runners.prompt_cli_runner import CodexRunner, GeminiRunner

        for cls in (GeminiRunner, CodexRunner):
            assert not getattr(cls, "honoured_controls", frozenset())
