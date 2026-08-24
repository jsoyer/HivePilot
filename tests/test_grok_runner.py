"""grok is a claude-shaped runner, and the inheritance is the safety.

The tempting version was three lines against `PromptCliRunner`, like
`GeminiRunner`. It would have run — and silently dropped every role's
`permission_mode` and `allowed_tools` (that class has 0 references to either,
against 38 in `claude_runner`), along with the elevated-mode sandbox and the
environment scrub. Since #569 it would simply be refused, which is better, but
still not the runner anyone wants.

Grok's headless surface is Claude Code's, values included. Verified against
`grok --help` 1.0.5 on 2026-08-21:

    --permission-mode {default, acceptEdits, auto, dontAsk, bypassPermissions,
                       plan}      Claude's exact set, plus auto/dontAsk
    --allow <RULE>                compat alias `--allowedTools`
    -m, --model                   grok-4.6 (default) | grok-4.5

So `ClaudeRunner` gained three named flags and grok overrides them. The half of
this file that matters most is `TestClaudeIsUnchanged`: a refactor that quietly
altered claude's argv would be far more expensive than no grok at all.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hivepilot.runners.claude_runner import ClaudeRunner
from hivepilot.runners.grok_runner import GrokRunner


class TestItInheritsTheSafety:
    def test_it_is_a_claude_runner_not_a_prompt_cli_runner(self):
        """The whole design in one assertion."""
        from hivepilot.runners.prompt_cli_runner import PromptCliRunner

        assert issubclass(GrokRunner, ClaudeRunner)
        assert not issubclass(GrokRunner, PromptCliRunner)

    def test_it_honours_both_controls(self):
        assert GrokRunner.honoured_controls == frozenset({"permission_mode", "allowed_tools"})

    def test_a_restricted_role_is_not_refused_on_grok(self):
        """The consequence of the inheritance, checked through the real guard
        rather than by reading the attribute back."""
        from hivepilot.runners.base import assert_runner_honours

        assert_runner_honours(
            "grok",
            GrokRunner,
            {"allowed_tools": ["Bash(rtk git:*)"], "permission_mode": "bypassPermissions"},
        )

    def test_it_would_be_refused_if_it_had_taken_the_easy_route(self):
        """Proves the guard is what protects this, not luck: the same options
        against a runner declaring nothing must raise."""
        from hivepilot.runners.base import RunnerControlUnsupportedError, assert_runner_honours

        class _EasyRoute:
            pass

        with pytest.raises(RunnerControlUnsupportedError):
            assert_runner_honours("grok", _EasyRoute, {"allowed_tools": ["Bash(rtk git:*)"]})

    def test_api_mode_fails_closed(self):
        """`ClaudeRunner` advertises cli+api, but its API path is Anthropic's
        Messages API specifically. A resolved `mode: api` must be refused at
        validation, never dispatched to the wrong vendor's endpoint."""
        from hivepilot.runners.base import RunnerModeUnsupportedError, validate_runner_mode

        with pytest.raises(RunnerModeUnsupportedError):
            validate_runner_mode("grok", GrokRunner.supported_modes, "api")

        validate_runner_mode("grok", GrokRunner.supported_modes, "cli")


class TestTheFlagsMatchTheBinary:
    """Each overridden name was read from `grok --help`, not guessed. A wrong
    spelling here fails at dispatch with an unhelpful CLI error, so it is
    pinned where the reason can be read."""

    def test_the_print_flag_is_grok_s_single(self):
        assert GrokRunner.print_flag == "-p"

    def test_the_allow_flag_is_grok_s_allow(self):
        assert GrokRunner.allowed_tools_flag == "--allow"

    def test_the_permission_mode_flag_is_spelled_the_same_as_claude_s(self):
        """Same spelling AND the same values — grok accepts
        bypassPermissions, which is what all 22 noxys roles set."""
        assert GrokRunner.permission_mode_flag == ClaudeRunner.permission_mode_flag


class TestClaudeIsUnchanged:
    """Parameterising a 1595-line module is only acceptable if the runner it
    was written for dispatches identically."""

    def test_claude_keeps_its_own_flag_names(self):
        assert ClaudeRunner.print_flag == "--print"
        assert ClaudeRunner.allowed_tools_flag == "--allowed-tools"
        assert ClaudeRunner.permission_mode_flag == "--permission-mode"

    def test_no_literal_flag_survives_in_the_builder(self):
        """If one did, a sibling overriding the attribute would emit BOTH the
        overridden flag and claude's hard-coded one."""
        import inspect

        src = inspect.getsource(ClaudeRunner._build_invocation)

        assert '"--print"' not in src
        assert '"--allowed-tools"' not in src
        assert '"--permission-mode"' not in src

    def test_the_command_is_still_not_hardcoded(self):
        """`"claude"` has never been a literal here — the command comes from
        `definition.command or settings.claude_command`. That is what made the
        three flags the whole of what a sibling needs."""
        import ast
        import inspect

        from hivepilot.runners import claude_runner

        # Over the AST, not the raw text: the first version of this test read
        # the source as a string and tripped on the COMMENT that explains why
        # the literal is absent. A guard that its own documentation breaks is
        # not measuring the code.
        tree = ast.parse(inspect.getsource(claude_runner))
        literals = [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        # docstrings are string constants too, and they legitimately say
        # "claude" -- only an exact standalone literal would be the bug.
        assert "claude" not in [lit.strip() for lit in literals]


class TestThePluginGatesOnThePath:
    def test_it_registers_the_runner_when_the_binary_is_present(self, monkeypatch):
        from hivepilot.bundled_plugins import grok as plugin

        monkeypatch.setattr(plugin.shutil, "which", lambda _: "/usr/local/bin/grok")

        contributed = plugin.register()

        assert contributed["runners"] == {"grok": GrokRunner}

    def test_an_absent_binary_contributes_NOTHING(self, monkeypatch):
        """Not a degraded runner — no kind at all, so `resolve_runner_class`
        raises its actionable error BEFORE a run row exists. A builtin
        declares availability by presence in a dict; a plugin declares it by a
        check, and that is the difference this plugin exists to preserve."""
        from hivepilot.bundled_plugins import grok as plugin

        monkeypatch.setattr(plugin.shutil, "which", lambda _: None)

        assert plugin.register() == {}

    def test_the_flag_off_contributes_nothing_even_with_the_binary(self, monkeypatch):
        from hivepilot.bundled_plugins import grok as plugin
        from hivepilot.config import settings

        monkeypatch.setattr(plugin.shutil, "which", lambda _: "/usr/local/bin/grok")
        monkeypatch.setattr(settings, "grok_enabled", False, raising=False)

        assert plugin.register() == {}

    def test_health_is_degraded_not_ok_when_absent(self, monkeypatch):
        """`degraded` rather than `error`: the kind being unavailable is not a
        fault, and an operator who never installed grok must not see a red
        box."""
        from hivepilot.bundled_plugins import grok as plugin

        monkeypatch.setattr(plugin.shutil, "which", lambda _: None)

        assert plugin.health().status == "degraded"


class TestItIsReachableTheWayTheOthersAre:
    def test_it_is_in_the_agent_cli_plugin_set(self):
        from hivepilot.services.plugin_installer import AGENT_CLI_PLUGINS

        assert "grok" in AGENT_CLI_PLUGINS

    def test_guided_install_knows_it(self):
        from hivepilot.services.agent_install import AGENT_INSTALL_SPECS

        spec = AGENT_INSTALL_SPECS["grok"]

        assert spec.binary == "grok"
        assert spec.command == "curl -fsSL https://x.ai/cli/install.sh | bash"

    def test_the_bundled_plugin_ships_in_the_wheel(self):
        from conftest import BUNDLED_PLUGINS

        assert (BUNDLED_PLUGINS / "grok.py").is_file()


@pytest.mark.skipif(shutil.which("grok") is None, reason="grok not installed here")
class TestAgainstTheRealBinary:
    """Runs only where grok exists. Everything above compares strings; this
    checks the strings against the thing they describe."""

    def test_the_permission_mode_values_the_roles_use_are_accepted(self):
        import subprocess

        help_text = subprocess.run(
            ["grok", "--help"], capture_output=True, text=True, check=False
        ).stdout

        assert "--permission-mode" in help_text
        # every noxys role sets exactly this one
        assert "bypassPermissions" in help_text

    def test_the_allow_flag_exists_and_names_its_claude_alias(self):
        import subprocess

        help_text = subprocess.run(
            ["grok", "--help"], capture_output=True, text=True, check=False
        ).stdout

        assert "--allow" in help_text
        assert "allowedTools" in help_text, (
            "the compat alias is why Claude-grammar rules pass through understood"
        )


class TestSingleTakesThePromptAsItsValue:
    """Run 717 on mix: `grok -p --model grok-4.5 -- <prompt>` exited 2 with
    "a value is required for '--single <PROMPT>' but none was supplied".

    Claude's `--print` is a boolean, so putting it next to argv[0] is fine.
    Grok's `-p` / `--single` takes the prompt as its VALUE. The next token
    was `--model`, which is an option, so clap refused. Measured on the box
    2026-08-24: `grok --model grok-4.5 -p hi` works; `grok -p --model …` does
    not.

    `--allow` is one rule per flag. `--allow Read Bash -p hi` fails with
    "the argument '[PROMPT]' cannot be used with '--single <PROMPT>'".
    Repeated `--allow Read --allow Bash` works. Restricted roles (Hugo,
    Victor, Colette) would have hit this next.
    """

    @staticmethod
    def _argv(tmp_path, *, allowed_tools: list[str] | None = None) -> list[str]:
        from hivepilot.config import settings
        from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
        from hivepilot.runners.base import RunnerPayload

        pf = tmp_path / "p.md"
        pf.write_text("do it", encoding="utf-8")
        payload = RunnerPayload(
            project_name="p",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(name="s", runner="grok", prompt_file=str(pf)),
            metadata={},
            secrets={},
        )
        options: dict = {}
        if allowed_tools is not None:
            options["allowed_tools"] = allowed_tools
        definition = RunnerDefinition(
            name="r", kind="grok", command=None, model="grok-4.5", options=options
        )
        args, _ = GrokRunner(definition, settings)._build_invocation(payload)
        return args

    def test_the_prompt_travels_in_a_file_not_argv(self, tmp_path):
        args = self._argv(tmp_path)
        assert "-p" not in args
        i = args.index("--prompt-file")
        path = Path(args[i + 1])
        try:
            assert "do it" in path.read_text(encoding="utf-8")
        finally:
            path.unlink(missing_ok=True)

    def test_model_is_still_on_the_command_line(self, tmp_path):
        args = self._argv(tmp_path)
        assert "--model" in args
        i = args.index("--prompt-file")
        Path(args[i + 1]).unlink(missing_ok=True)

    def test_each_allow_rule_is_its_own_flag(self, tmp_path):
        args = self._argv(tmp_path, allowed_tools=["Read(./**)", "Bash(rtk:*)"])
        try:
            assert args.count("--allow") == 2
            pairs = list(zip(args, args[1:]))
            assert ("--allow", "Read(./**)") in pairs
            assert ("--allow", "Bash(rtk:*)") in pairs
        finally:
            i = args.index("--prompt-file")
            Path(args[i + 1]).unlink(missing_ok=True)

    def test_mcp_config_is_not_emitted(self, tmp_path):
        """Run 718: token-savior wired an mcp json, grok rejected --mcp-config."""
        from hivepilot.config import settings
        from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
        from hivepilot.runners.base import RunnerPayload

        pf = tmp_path / "p.md"
        pf.write_text("do it", encoding="utf-8")
        mcp = tmp_path / "mcp.json"
        mcp.write_text("{}", encoding="utf-8")
        payload = RunnerPayload(
            project_name="p",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(
                name="s",
                runner="grok",
                prompt_file=str(pf),
                metadata={
                    "mcp_config": [str(mcp)],
                    "allowed_tools": ["mcp__token-savior-recall", "WaitForMcpServers"],
                },
            ),
            metadata={},
            secrets={},
        )
        definition = RunnerDefinition(name="r", kind="grok", command=None)
        args, _ = GrokRunner(definition, settings)._build_invocation(payload)
        try:
            assert "--mcp-config" not in args
            assert "--strict-mcp-config" not in args
            assert "--allow" not in args
        finally:
            if "--prompt-file" in args:
                Path(args[args.index("--prompt-file") + 1]).unlink(missing_ok=True)


class TestTheCommandIsNotClaudes:
    """The bug the old cursor `command_name` test exposed, fixed at the base.

    A role-synthesized definition carries `command=None`, and the base's
    fallback chain read `definition.command or settings.claude_command` — so
    `kind: grok` on a role dispatch would have LAUNCHED CLAUDE, silently, with
    only the resolved-model stamp to notice it by. Behavioural, argv[0] from
    the real `_build_invocation`: the source-string version of a test like
    this has already survived a disabled branch once this session."""

    @staticmethod
    def _argv0_for(runner_cls, tmp_path):
        from hivepilot.config import settings
        from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
        from hivepilot.runners.base import RunnerPayload

        pf = tmp_path / "p.md"
        pf.write_text("do it", encoding="utf-8")
        payload = RunnerPayload(
            project_name="p",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(name="s", runner="claude", prompt_file=str(pf)),
            metadata={},
            secrets={},
        )
        definition = RunnerDefinition(name="r", kind="claude", command=None)
        args, _ = runner_cls(definition, settings)._build_invocation(payload)
        if "--prompt-file" in args:
            Path(args[args.index("--prompt-file") + 1]).unlink(missing_ok=True)
        return args[0]

    def test_grok_with_no_command_launches_grok(self, tmp_path):
        assert self._argv0_for(GrokRunner, tmp_path) == "grok"

    def test_cursor_with_no_command_launches_cursor_agent(self, tmp_path):
        from hivepilot.runners.cursor_runner import CursorRunner

        assert self._argv0_for(CursorRunner, tmp_path) == "cursor-agent"

    def test_claude_still_falls_through_to_its_setting(self, tmp_path, monkeypatch):
        """The discriminating half: the fix must not steal claude's own
        fallback. `command_name` is None there, so the setting still decides."""
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "claude_command", "claude-custom", raising=False)

        assert self._argv0_for(ClaudeRunner, tmp_path) == "claude-custom"

    def test_an_explicit_command_beats_the_class_default(self, tmp_path):
        """`definition.command` stays first: an operator naming a binary wins
        over what the class would pick."""
        from hivepilot.config import settings
        from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
        from hivepilot.runners.base import RunnerPayload

        pf = tmp_path / "p.md"
        pf.write_text("x", encoding="utf-8")
        payload = RunnerPayload(
            project_name="p",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(name="s", runner="claude", prompt_file=str(pf)),
            metadata={},
            secrets={},
        )
        definition = RunnerDefinition(name="r", kind="claude", command="/opt/grok-beta")
        args, _ = GrokRunner(definition, settings)._build_invocation(payload)
        try:
            assert args[0] == "/opt/grok-beta"
        finally:
            if "--prompt-file" in args:
                Path(args[args.index("--prompt-file") + 1]).unlink(missing_ok=True)
