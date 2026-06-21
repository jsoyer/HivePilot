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


def test_permission_mode_flag_when_configured(tmp_path: Path, monkeypatch) -> None:
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
    runner = _runner()
    monkeypatch.setattr(runner.settings, "claude_permission_mode", "acceptEdits", raising=False)
    args, _ = runner._build_invocation(payload)
    assert "--permission-mode" in args
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"


def test_no_permission_flag_by_default(tmp_path: Path, monkeypatch) -> None:
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
    runner = _runner()
    monkeypatch.setattr(runner.settings, "claude_permission_mode", None, raising=False)
    args, _ = runner._build_invocation(payload)
    assert "--permission-mode" not in args


def test_step_metadata_overrides_global_permission_mode(tmp_path: Path, monkeypatch) -> None:
    pf = tmp_path / "p.md"
    pf.write_text("do it", encoding="utf-8")
    payload = RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(
            name="s",
            runner="claude",
            prompt_file=str(pf),
            metadata={"permission_mode": "bypassPermissions"},
        ),
        metadata={},
        secrets={},
    )
    runner = _runner()
    monkeypatch.setattr(runner.settings, "claude_permission_mode", "acceptEdits", raising=False)
    args, _ = runner._build_invocation(payload)
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions"


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


# ── L1: prompt ordering tests ────────────────────────────────────────────────

def test_stable_sections_before_volatile(tmp_path: Path) -> None:
    """knowledge_context (stable) must appear before prior_context (volatile)."""
    payload = _payload(tmp_path, {"prior_context": "PRIOR_DATA"})
    out = _runner()._build_prompt(payload, "INSTRUCTIONS", "KNOWLEDGE_DATA")
    idx_knowledge = out.index("KNOWLEDGE_DATA")
    idx_prior = out.index("PRIOR_DATA")
    assert idx_knowledge < idx_prior, (
        "knowledge_context (stable) should precede prior_context (volatile)"
    )


def test_extra_prompt_after_knowledge_context(tmp_path: Path) -> None:
    """extra_prompt (volatile) must appear after knowledge_context (stable)."""
    payload = _payload(tmp_path, {"extra_prompt": "EXTRA_USER_INSTRUCTIONS"})
    out = _runner()._build_prompt(payload, "INSTRUCTIONS", "KNOWLEDGE_DATA")
    idx_knowledge = out.index("KNOWLEDGE_DATA")
    idx_extra = out.index("EXTRA_USER_INSTRUCTIONS")
    assert idx_knowledge < idx_extra, (
        "knowledge_context (stable) should precede extra_prompt (volatile)"
    )
