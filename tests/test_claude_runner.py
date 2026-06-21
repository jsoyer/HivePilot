"""Claude runner prompt assembly — incl. the inter-agent hand-off context."""

from __future__ import annotations

from pathlib import Path

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner


def _payload(tmp_path: Path, metadata: dict) -> RunnerPayload:
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata=metadata,
        secrets={},
    )


def _runner() -> ClaudeRunner:
    return ClaudeRunner(RunnerDefinition(name="claude", kind="claude", command="claude"), settings)


def test_build_prompt_includes_prior_context(tmp_path: Path) -> None:
    payload = _payload(tmp_path, {"prior_context": "CTO proposed Y"})
    out = _runner()._build_prompt(payload, "INSTRUCTIONS", None)
    assert "CTO proposed Y" in out
    assert "INSTRUCTIONS" in out


def test_build_prompt_without_prior_context_is_clean(tmp_path: Path) -> None:
    payload = _payload(tmp_path, {})
    out = _runner()._build_prompt(payload, "INSTRUCTIONS", None)
    assert "previous agents" not in out.lower()
    assert "INSTRUCTIONS" in out


def test_capture_returns_agent_stdout(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

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
    with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
        m.return_value = MagicMock(stdout="AGENT SAID THIS", returncode=0)
        out = _runner().capture(payload)
    assert out == "AGENT SAID THIS"
    assert m.call_args.kwargs["capture_output"] is True


def test_capture_surfaces_stderr_on_failure(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

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
    with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
        m.return_value = MagicMock(returncode=1, stdout="", stderr="boom: bad model")
        with __import__("pytest").raises(RuntimeError, match="boom: bad model"):
            _runner().capture(payload)
