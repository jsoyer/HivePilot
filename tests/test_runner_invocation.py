"""Lock the non-interactive CLI invocation contract for each runner.

Each agent CLI needs a different headless invocation (claude/cursor: --print;
gemini: -p <prompt>; codex: exec; opencode: run). A wrong invocation would
launch an interactive UI and hang on a real run.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner
from hivepilot.runners.prompt_cli_runner import CodexRunner, GeminiRunner, OpenCodeRunner


def _payload(tmp_path: Path) -> RunnerPayload:
    pf = tmp_path / "prompt.md"
    pf.write_text("do the thing", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="x", prompt_file=str(pf)),
        metadata={},
        secrets={},
    )


def _cli_args(cls, kind, command, model, tmp_path):
    runner = cls(RunnerDefinition(name=kind, kind=kind, command=command, model=model), settings)
    with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as m:
        runner.run(_payload(tmp_path))
    return m.call_args.args[0]


def test_codex_uses_exec_subcommand(tmp_path: Path) -> None:
    args = _cli_args(CodexRunner, "codex", "codex", None, tmp_path)
    assert args[:2] == ["codex", "exec"]
    assert args[-1] == "do the thing"


def test_gemini_passes_prompt_via_flag(tmp_path: Path) -> None:
    args = _cli_args(GeminiRunner, "gemini", "gemini", None, tmp_path)
    assert args[0] == "gemini"
    assert "-p" in args
    assert args[args.index("-p") + 1] == "do the thing"


def test_opencode_uses_run_subcommand_and_model(tmp_path: Path) -> None:
    args = _cli_args(OpenCodeRunner, "opencode", "opencode", "kimi", tmp_path)
    assert args[:2] == ["opencode", "run"]
    assert "--model" in args and args[args.index("--model") + 1] == "kimi"


def test_claude_uses_print_flag(tmp_path: Path) -> None:
    runner = ClaudeRunner(
        RunnerDefinition(name="claude", kind="claude", command="claude"), settings
    )
    with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
        runner.run(_payload(tmp_path))
    args = m.call_args.args[0]
    assert args[0] == "claude"
    assert "--print" in args
