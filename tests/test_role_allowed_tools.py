"""A role must be able to use the plugins it needs, without bypassPermissions.

Before this, a role had exactly two states and both were wrong:

- No `permission_mode`: headless `claude --print` cannot display a permission
  prompt, so EVERY tool call is declined. The agent answers by asking for
  approval, and the false-green guard fails the step with *"produced no work —
  agent response reads like a permission/approval request"*. Observed in
  production on `cto review` and `groomer-scan`; it is what stalled the whole
  pipeline, and it is why the CISO reported that `rtk` was refused.
- `bypassPermissions`: Bash and Edit granted wholesale — an unacceptable
  prompt-injection surface for exactly the roles that read untrusted code.

`allowed_tools` is the third state: name the tools, grant those, nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.roles import Role
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner


def _role(**overrides: Any) -> Role:
    base: dict[str, Any] = {
        "name": "ciso",
        "title": "CISO",
        "prompt_file": "ciso.md",
        "model_profile": "architecture",
        "inputs": [],
        "outputs": [],
        "can_block": True,
        "order": 1,
    }
    base.update(overrides)
    return Role.model_validate(base)


def _args_for(options: dict, tmp_path: Path) -> list[str]:
    prompt = tmp_path / "p.md"
    prompt.write_text("do it", encoding="utf-8")
    runner = ClaudeRunner(
        RunnerDefinition(name="claude", kind="claude", command="claude", options=options),
        settings,
    )
    payload = RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude", prompt_file=str(prompt)),
        metadata={},
        secrets={},
    )
    args, _ = runner._build_invocation(payload)
    return args


# ---------------------------------------------------------------------------
# The Role model
# ---------------------------------------------------------------------------


def test_a_role_can_declare_the_tools_it_needs():
    role = _role(allowed_tools=["Bash(rtk:*)", "Bash(gh:*)"])
    assert role.allowed_tools == ["Bash(rtk:*)", "Bash(gh:*)"]


def test_the_field_is_absent_by_default():
    """Every existing roles.yaml must keep behaving exactly as before."""
    role = _role(name="qa", title="QA", prompt_file="qa.md")
    assert role.allowed_tools is None


# ---------------------------------------------------------------------------
# Role -> runner options -> CLI flags
# ---------------------------------------------------------------------------


def test_declared_tools_reach_the_claude_invocation(tmp_path):
    args = _args_for({"allowed_tools": ["Bash(rtk:*)", "Bash(gh:*)"]}, tmp_path)
    i = args.index("--allowed-tools")
    assert args[i + 1 : i + 3] == ["Bash(rtk:*)", "Bash(gh:*)"]


def test_granting_tools_does_not_grant_elevated_permissions(tmp_path):
    """The whole point: plugins WITHOUT Bash-and-Edit wholesale."""
    args = _args_for({"allowed_tools": ["Bash(rtk:*)"]}, tmp_path)
    assert "--permission-mode" not in args
    assert "--dangerously-skip-permissions" not in args


def test_a_role_may_still_combine_both_when_it_genuinely_needs_to(tmp_path):
    """The developer writes code autonomously; the two are not exclusive."""
    args = _args_for({"allowed_tools": ["Bash(rtk:*)"], "permission_mode": "acceptEdits"}, tmp_path)
    assert "--allowed-tools" in args
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"


def test_no_flag_when_a_role_declares_nothing(tmp_path):
    assert "--allowed-tools" not in _args_for({}, tmp_path)


def test_the_prompt_survives_the_variadic_flag(tmp_path):
    """`--allowed-tools` is variadic — the `--` separator must still guard the prompt."""
    args = _args_for({"allowed_tools": ["Bash(rtk:*)"]}, tmp_path)
    assert args[-2] == "--"
    assert args[-1]
