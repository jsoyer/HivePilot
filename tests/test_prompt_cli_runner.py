"""Unit tests for PromptCliRunner subclass defaults and L1 cache-friendly ordering.

The non-interactive invocation contract (actual argv built per runner) is locked
in test_runner_invocation.py. Here we pin the declared class defaults for the
Mistral ``vibe`` runner, and verify L1 prompt ordering + Anthropic cache_control.
"""

from __future__ import annotations

from pathlib import Path

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.prompt_cli_runner import PromptCliRunner, VibeRunner


# ── VibeRunner defaults ───────────────────────────────────────────────────────

def test_vibe_runner_defaults() -> None:
    assert VibeRunner.command_name == "vibe"
    assert "--auto-approve" in VibeRunner.cli_flags
    assert VibeRunner.prompt_flag == "--prompt"
    # vibe has no subcommand (unlike codex 'exec' / opencode 'run')
    assert VibeRunner.cli_subcommand is None


# ── L1: _augment_prompt ordering + anthropic cache_control ───────────────────

def _cli_payload(tmp_path: Path, metadata: dict) -> RunnerPayload:
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="api"),
        metadata=metadata,
        secrets={},
    )


def _cli_runner() -> PromptCliRunner:
    return PromptCliRunner(
        RunnerDefinition(name="cli", kind="api", command="echo"),
        settings,
    )


def test_augment_prompt_volatile_after_base(tmp_path: Path) -> None:
    """Base prompt_text (stable) must appear before extra_prompt / prior_context."""
    payload = _cli_payload(
        tmp_path,
        {"extra_prompt": "EXTRA_INST", "prior_context": "PRIOR_OUT"},
    )
    runner = _cli_runner()
    result = runner._augment_prompt(payload, "BASE_INSTRUCTIONS")
    idx_base = result.index("BASE_INSTRUCTIONS")
    idx_extra = result.index("EXTRA_INST")
    idx_prior = result.index("PRIOR_OUT")
    assert idx_base < idx_extra, "base prompt must precede extra_prompt"
    assert idx_base < idx_prior, "base prompt must precede prior_context"


def test_augment_prompt_no_volatile_returns_unchanged(tmp_path: Path) -> None:
    """When no volatile metadata is present, prompt_text is returned unchanged."""
    payload = _cli_payload(tmp_path, {})
    runner = _cli_runner()
    result = runner._augment_prompt(payload, "ONLY_BASE")
    assert result == "ONLY_BASE"


def test_anthropic_payload_with_cache_control(tmp_path: Path, monkeypatch) -> None:
    """When anthropic_prompt_cache=True, system block carries cache_control."""
    runner = _cli_runner()
    monkeypatch.setattr(runner.settings, "anthropic_prompt_cache", True, raising=False)

    captured: list[dict] = []

    def fake_post(url, headers, payload, timeout):  # noqa: ANN001
        captured.append({"headers": headers, "payload": payload})

    runner._post_json = fake_post  # type: ignore[method-assign]

    payload = _cli_payload(tmp_path, {})
    env = {"ANTHROPIC_API_KEY": "test-key"}
    runner.definition.options["api_provider"] = "anthropic"
    runner.definition.options["api_model"] = "claude-3-haiku-20240307"
    runner._run_api("test prompt", payload, env)

    assert len(captured) == 1
    sent = captured[0]["payload"]
    assert "system" in sent, "system key must be present with cache_control"
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "anthropic-beta" in captured[0]["headers"]


def test_anthropic_payload_without_cache_control(tmp_path: Path, monkeypatch) -> None:
    """When anthropic_prompt_cache=False, plain messages payload (no cache_control)."""
    runner = _cli_runner()
    monkeypatch.setattr(runner.settings, "anthropic_prompt_cache", False, raising=False)

    captured: list[dict] = []

    def fake_post(url, headers, payload, timeout):  # noqa: ANN001
        captured.append({"headers": headers, "payload": payload})

    runner._post_json = fake_post  # type: ignore[method-assign]

    payload = _cli_payload(tmp_path, {})
    env = {"ANTHROPIC_API_KEY": "test-key"}
    runner.definition.options["api_provider"] = "anthropic"
    runner.definition.options["api_model"] = "claude-3-haiku-20240307"
    runner._run_api("test prompt", payload, env)

    assert len(captured) == 1
    sent = captured[0]["payload"]
    assert "messages" in sent
    assert "system" not in sent
    assert "anthropic-beta" not in captured[0]["headers"]
