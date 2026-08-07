"""`allowed_tools` is a grant SET — combine it, never override it.

Every other runner option resolves "step metadata beats runner definition",
which is right for a scalar: one model, one permission mode, last writer
wins. It is wrong for a whitelist. Overriding a set of grants does not
refine it, it REVOKES everything the other layer granted.

Run 347 is what that costs. `token_savior` appended
`mcp__token-savior-recall` to the step's `allowed_tools`, which -- because
step metadata won -- replaced the reviewer role's entire grant list. The
reviewer then had its own tools denied:

    permission_denials: rtk git status --short ...        DENIED
                        for f in surfaces/agent/... do ]  DENIED

and the dispatch died on

    API Error: 400 Tool reference 'WaitForMcpServers' not found in available tools

because a `--mcp-config` invocation also needs Claude Code's own MCP
bootstrap tool in the list, and the list had been narrowed to one entry.

$0.56 spent, the stage failed, and the pipeline fail-fasted with five stages
left. A plugin that only meant to add one tool silently took away all the
others.

The plugin's own test claimed to cover this -- "existing allowed_tools
survive" -- and passed, because it seeded the existing grants in step
METADATA. The real grants live in the runner DEFINITION's options. The test
agreed with the code about where to look, and both were looking in the wrong
place.
"""

from __future__ import annotations

from pathlib import Path

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner

_ROLE_GRANT = "Bash(rtk git status:*)"
_PLUGIN_GRANT = "mcp__token-savior-recall"


def _runner(**options) -> ClaudeRunner:
    return ClaudeRunner(
        RunnerDefinition(name="claude", kind="claude", command=None, options=dict(options)),
        settings,
    )


def _payload(tmp_path: Path, **metadata) -> RunnerPayload:
    prompt = tmp_path / "p.md"
    prompt.write_text("x", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude", prompt_file=str(prompt), metadata=dict(metadata)),
        metadata={},
        secrets={},
    )


def _tools(runner: ClaudeRunner, payload: RunnerPayload) -> list[str]:
    args = runner._build_invocation(payload)[0]
    if "--allowed-tools" not in args:
        return []
    start = args.index("--allowed-tools") + 1
    out = []
    for item in args[start:]:
        if item.startswith("--"):
            break
        out.append(item)
    return out


class TestGrantsCombineAcrossLayers:
    def test_a_step_grant_does_not_revoke_the_role_grant(self, tmp_path) -> None:
        """The exact run-347 defect: a plugin adds one tool, the role loses
        all of its own."""
        tools = _tools(
            _runner(allowed_tools=[_ROLE_GRANT]),
            _payload(tmp_path, allowed_tools=[_PLUGIN_GRANT]),
        )

        assert _ROLE_GRANT in tools
        assert _PLUGIN_GRANT in tools

    def test_the_definition_alone_still_works(self, tmp_path) -> None:
        assert _tools(_runner(allowed_tools=[_ROLE_GRANT]), _payload(tmp_path)) == [_ROLE_GRANT]

    def test_the_step_alone_still_works(self, tmp_path) -> None:
        assert _tools(_runner(), _payload(tmp_path, allowed_tools=[_PLUGIN_GRANT])) == [
            _PLUGIN_GRANT
        ]

    def test_neither_means_no_flag(self, tmp_path) -> None:
        """Absent must stay absent: an empty `--allowed-tools` would restrict
        the agent to nothing at all."""
        assert _tools(_runner(), _payload(tmp_path)) == []

    def test_a_duplicate_grant_appears_once(self, tmp_path) -> None:
        tools = _tools(
            _runner(allowed_tools=[_ROLE_GRANT]),
            _payload(tmp_path, allowed_tools=[_ROLE_GRANT]),
        )

        assert tools.count(_ROLE_GRANT) == 1

    def test_order_is_stable(self, tmp_path) -> None:
        """Definition first, then the step's additions — so a config file's
        grants read in the order the operator wrote them."""
        tools = _tools(
            _runner(allowed_tools=[_ROLE_GRANT]),
            _payload(tmp_path, allowed_tools=[_PLUGIN_GRANT]),
        )

        assert tools == [_ROLE_GRANT, _PLUGIN_GRANT]


class TestScalarOptionsKeepTheirPrecedence:
    def test_a_step_still_overrides_the_model(self, tmp_path) -> None:
        """Only the SET combines. A scalar still has a last writer, or every
        per-step override in the system would stop working."""
        args = _runner(model="opus")._build_invocation(_payload(tmp_path, model="sonnet"))[0]

        assert "sonnet" in args
        assert "opus" not in args
