"""Lock the non-interactive CLI invocation contract for each runner.

Each agent CLI needs a different headless invocation (claude/cursor: --print;
gemini: -p <prompt>; codex: exec; opencode: run). A wrong invocation would
launch an interactive UI and hang on a real run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner
from hivepilot.runners.prompt_cli_runner import (
    CodexRunner,
    GeminiRunner,
    OpenCodeRunner,
    VibeRunner,
)


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


def test_vibe_uses_prompt_flag_and_auto_approve(tmp_path: Path) -> None:
    args = _cli_args(VibeRunner, "vibe", "vibe", None, tmp_path)
    assert args[0] == "vibe"
    assert "--auto-approve" in args
    assert "--prompt" in args
    assert args[args.index("--prompt") + 1] == "do the thing"
    # vibe has no --model flag — model comes from its own config; none passed here
    assert "--model" not in args


def test_runner_with_host_wraps_in_ssh(tmp_path: Path) -> None:
    runner = VibeRunner(
        RunnerDefinition(name="vibe", kind="vibe", command="vibe", host="user@hostB"), settings
    )
    with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as m:
        runner.run(_payload(tmp_path))
    args = m.call_args.args[0]
    assert args[0] == "ssh"
    assert "user@hostB" in args
    assert "vibe" in args[-1]  # the agent CLI runs inside the remote command


def test_claude_uses_print_flag(tmp_path: Path) -> None:
    runner = ClaudeRunner(
        RunnerDefinition(name="claude", kind="claude", command="claude"), settings
    )
    with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
        runner.run(_payload(tmp_path))
    args = m.call_args.args[0]
    assert args[0] == "claude"
    assert "--print" in args


class TestPaneModeIsAChoice:
    """The pane flag changes where every step's process lives, so the case that
    matters is the one where it must NOT reach for herdr.

    A box without a herdr server has to keep working exactly as before. A test
    that only checked "flag on -> pane" would pass just as well against a
    runner that always used a pane, which is the failure the operator would
    actually hit.
    """

    @staticmethod
    def _runner():
        return ClaudeRunner(
            RunnerDefinition(name="claude", kind="claude", command="claude"), settings
        )

    @staticmethod
    def _ok():
        return subprocess.CompletedProcess(["claude"], 0, "envelope", "")

    def test_off_by_default_never_touches_a_pane(self, tmp_path: Path) -> None:
        with (
            patch("hivepilot.runners.claude_runner.subprocess.run") as direct,
            patch("hivepilot.runners.claude_runner.run_in_pane") as pane,
        ):
            direct.return_value = self._ok()
            self._runner().capture(_payload(tmp_path))

        assert direct.called
        assert not pane.called

    def test_on_runs_the_agent_in_a_pane_instead(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(settings, "claude_pane_mode", True)

        with (
            patch("hivepilot.runners.claude_runner.subprocess.run") as direct,
            patch("hivepilot.runners.claude_runner.run_in_pane") as pane,
        ):
            pane.return_value = self._ok()
            self._runner().capture(_payload(tmp_path))

        assert pane.called
        assert not direct.called

    def test_the_invocation_itself_is_unchanged(self, tmp_path: Path, monkeypatch) -> None:
        """Same argv, same prompt, same flags. The pane changes where the
        process lives, not what is dispatched -- an argv that drifted between
        the two paths would make every pane run a different experiment."""
        with patch("hivepilot.runners.claude_runner.subprocess.run") as direct:
            direct.return_value = self._ok()
            self._runner().capture(_payload(tmp_path))
        direct_argv = direct.call_args.args[0]

        monkeypatch.setattr(settings, "claude_pane_mode", True)
        with patch("hivepilot.runners.claude_runner.run_in_pane") as pane:
            pane.return_value = self._ok()
            self._runner().capture(_payload(tmp_path))
        pane_argv = pane.call_args.args[0]

        assert pane_argv == direct_argv

    def test_the_pane_lands_in_the_run_s_workspace_when_there_is_one(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Otherwise the step splits `--current` -- whatever workspace the
        operator happens to be looking at."""
        from hivepilot.services import herdr_workspace

        monkeypatch.setattr(settings, "claude_pane_mode", True)
        token = herdr_workspace._CURRENT_PANE.set("w9:p1")
        try:
            with patch("hivepilot.runners.claude_runner.run_in_pane") as pane:
                pane.return_value = self._ok()
                self._runner().capture(_payload(tmp_path))
        finally:
            herdr_workspace._CURRENT_PANE.reset(token)

        assert pane.call_args.kwargs["target_pane"] == "w9:p1"

    def test_with_no_workspace_it_targets_nothing(self, tmp_path: Path, monkeypatch) -> None:
        """The discriminating case. A run without a workspace must keep
        working, and passing an empty target would address nothing."""
        monkeypatch.setattr(settings, "claude_pane_mode", True)

        with patch("hivepilot.runners.claude_runner.run_in_pane") as pane:
            pane.return_value = self._ok()
            self._runner().capture(_payload(tmp_path))

        assert pane.call_args.kwargs["target_pane"] is None

    def test_the_pane_receives_the_environment_the_subprocess_would_have(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The pane inherits the herdr SERVER's environment. Handed anything
        less than the runner's own scrubbed, overlaid env, the agent loses its
        API key and its OTel destination."""
        with patch("hivepilot.runners.claude_runner.subprocess.run") as direct:
            direct.return_value = self._ok()
            self._runner().capture(_payload(tmp_path))
        direct_env = direct.call_args.kwargs["env"]

        monkeypatch.setattr(settings, "claude_pane_mode", True)
        with patch("hivepilot.runners.claude_runner.run_in_pane") as pane:
            pane.return_value = self._ok()
            self._runner().capture(_payload(tmp_path))

        assert pane.call_args.kwargs["env"] == direct_env
        assert pane.call_args.kwargs["cwd"] == direct.call_args.kwargs["cwd"]
