"""MCP servers and named-tool pre-approval for the Claude runner.

HivePilot never passed `--mcp-config`, so no agent could reach an MCP server:
a reviewer asked to use a code-graph server had to answer from plain file reads
and said so in its own report. Wiring the servers alone is not enough — in
headless `--print` mode a tool that is available but not pre-approved hits a
permission prompt claude cannot show, so the call is declined and the agent
silently works without it. `--allowed-tools` is what closes that, and it does
so WITHOUT handing the agent `bypassPermissions`.

Flag names verified against `claude --help`: `--mcp-config <configs...>`,
`--strict-mcp-config`, `--allowedTools, --allowed-tools <tools...>`.
"""

from __future__ import annotations

from pathlib import Path

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner


def _runner(**options) -> ClaudeRunner:
    return ClaudeRunner(
        RunnerDefinition(name="claude", kind="claude", command="claude", options=options),
        settings,
    )


def _payload(tmp_path: Path, metadata: dict | None = None) -> RunnerPayload:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the thing", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude", prompt_file=str(prompt), metadata=metadata or {}),
        metadata={},
        secrets={},
    )


def _args(runner: ClaudeRunner, payload: RunnerPayload) -> list[str]:
    args, _ = runner._build_invocation(payload)
    return args


# ---------------------------------------------------------------------------
# Absent by default
# ---------------------------------------------------------------------------


def test_no_mcp_flags_when_unconfigured(tmp_path):
    """Existing invocations must stay byte-identical."""
    args = _args(_runner(), _payload(tmp_path))
    assert "--mcp-config" not in args
    assert "--allowed-tools" not in args
    assert "--strict-mcp-config" not in args


# ---------------------------------------------------------------------------
# --mcp-config
# ---------------------------------------------------------------------------


def test_mcp_config_is_passed_and_made_absolute(tmp_path):
    """A relative path must resolve against the project, not the cwd."""
    args = _args(_runner(mcp_config="mcp.json"), _payload(tmp_path))
    assert "--mcp-config" in args
    assert str(tmp_path / "mcp.json") in args


def test_an_absolute_mcp_path_is_left_alone(tmp_path):
    args = _args(_runner(mcp_config="/etc/hivepilot/mcp.json"), _payload(tmp_path))
    assert "/etc/hivepilot/mcp.json" in args


def test_several_mcp_configs_are_all_passed(tmp_path):
    args = _args(_runner(mcp_config=["/a.json", "/b.json"]), _payload(tmp_path))
    i = args.index("--mcp-config")
    assert args[i + 1 : i + 3] == ["/a.json", "/b.json"]


def test_strict_mcp_config_only_when_requested(tmp_path):
    assert "--strict-mcp-config" not in _args(_runner(mcp_config="/a.json"), _payload(tmp_path))
    args = _args(_runner(mcp_config="/a.json", strict_mcp_config=True), _payload(tmp_path))
    assert "--strict-mcp-config" in args


def test_strict_alone_does_nothing_without_a_config(tmp_path):
    """The flag is meaningless on its own — never emit it bare."""
    args = _args(_runner(strict_mcp_config=True), _payload(tmp_path))
    assert "--strict-mcp-config" not in args


# ---------------------------------------------------------------------------
# --allowed-tools
# ---------------------------------------------------------------------------


def test_allowed_tools_are_passed_individually(tmp_path):
    tools = ["mcp__graph__query", "mcp__graph__symbols"]
    args = _args(_runner(allowed_tools=tools), _payload(tmp_path))
    i = args.index("--allowed-tools")
    assert args[i + 1 : i + 3] == tools


def test_allowed_tools_do_not_imply_elevated_permissions(tmp_path):
    """The whole point: read-only MCP access without bypassPermissions."""
    args = _args(_runner(allowed_tools=["mcp__g__query"]), _payload(tmp_path))
    assert "--permission-mode" not in args
    assert "--dangerously-skip-permissions" not in args


# ---------------------------------------------------------------------------
# Precedence and hygiene
# ---------------------------------------------------------------------------


def test_step_metadata_wins_over_the_runner_definition(tmp_path):
    args = _args(
        _runner(mcp_config="/from-definition.json"),
        _payload(tmp_path, {"mcp_config": "/from-step.json"}),
    )
    assert "/from-step.json" in args
    assert "/from-definition.json" not in args


def test_blank_entries_never_reach_the_cli(tmp_path):
    """A bare flag with no operand would swallow the NEXT argument as its value."""
    args = _args(_runner(allowed_tools=["", "  ", "mcp__g__query"]), _payload(tmp_path))
    i = args.index("--allowed-tools")
    assert args[i + 1] == "mcp__g__query"
    assert "" not in args


def test_empty_list_emits_no_flag_at_all(tmp_path):
    args = _args(_runner(allowed_tools=[], mcp_config=[]), _payload(tmp_path))
    assert "--allowed-tools" not in args
    assert "--mcp-config" not in args


def test_variadic_flags_never_swallow_the_prompt(tmp_path):
    """`--mcp-config`/`--allowed-tools` are variadic — the `--` separator is
    what stops them consuming the positional prompt (the `--add-dir` class of
    bug, empirically reproduced against the real binary)."""
    args = _args(
        _runner(mcp_config="/a.json", allowed_tools=["mcp__g__query"]),
        _payload(tmp_path),
    )
    assert args[-2] == "--", f"prompt must be preceded by the end-of-options separator: {args[-3:]}"
    assert args[-1]  # the prompt itself survived
