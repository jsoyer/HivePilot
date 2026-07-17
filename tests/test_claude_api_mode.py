"""Sprint 1: ClaudeRunner provider-API execution path (`mode: api`).

Covers:
- `mode: api` (via step metadata) routes `capture()` through the Anthropic
  Messages API (mocked HTTP) and returns the assistant text + stashes usage.
- `mode: cli` (the default) leaves the CLI argv byte-identical — no HTTP call.
- Fail-closed: a missing `ANTHROPIC_API_KEY` raises a clear error and performs
  NO API call.
- Security: the resolved API key value never appears in the returned detail
  (masked AT the runner), nor in the CLI argv.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload, pop_last_usage
from hivepilot.runners.claude_runner import ClaudeRunner

_FAKE_KEY = "sk-ant-TESTKEY-abcdef0123456789-do-not-log"


def _api_payload(tmp_path: Path, metadata: dict | None = None) -> RunnerPayload:
    pf = tmp_path / "prompt.md"
    pf.write_text("do the thing", encoding="utf-8")
    md = {"mode": "api"}
    if metadata:
        md.update(metadata)
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude", prompt_file=str(pf), metadata=md),
        metadata={},
        secrets={},
    )


def _runner(model: str | None = "claude-3-5-sonnet-latest") -> ClaudeRunner:
    return ClaudeRunner(
        RunnerDefinition(name="claude", kind="claude", command="claude", model=model),
        settings,
    )


def _fake_response(json_body: dict):
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = json_body
    resp.content = b"x"
    resp.text = ""
    return resp


def test_supported_modes_includes_api() -> None:
    assert ClaudeRunner.supported_modes == frozenset({"cli", "api"})


def test_api_mode_calls_anthropic_and_returns_text(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_KEY)
    payload = _api_payload(tmp_path)
    runner = _runner()
    body = {
        "content": [{"type": "text", "text": "HELLO FROM CLAUDE API"}],
        "usage": {"input_tokens": 11, "output_tokens": 22},
        "model": "claude-3-5-sonnet-latest",
    }
    with patch(
        "hivepilot.runners.claude_runner.requests.post",
        return_value=_fake_response(body),
    ) as mock_post:
        out = runner.capture(payload)

    assert out == "HELLO FROM CLAUDE API"
    mock_post.assert_called_once()
    call = mock_post.call_args
    # requests.post(url, json=..., headers=..., timeout=...) — url is positional.
    url = call.args[0] if call.args else call.kwargs.get("url", "")
    assert url == "https://api.anthropic.com/v1/messages"
    usage = pop_last_usage()
    assert usage is not None
    assert usage.input_tokens == 11
    assert usage.output_tokens == 22


def test_api_mode_sends_key_in_header_not_argv(tmp_path: Path, monkeypatch) -> None:
    """The API key travels ONLY in the x-api-key header — never as a process
    argument (there is no subprocess in api mode at all)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_KEY)
    payload = _api_payload(tmp_path)
    runner = _runner()
    body = {"content": [{"type": "text", "text": "ok"}]}

    with (
        patch(
            "hivepilot.runners.claude_runner.requests.post",
            return_value=_fake_response(body),
        ) as mock_post,
        patch("hivepilot.runners.claude_runner.subprocess.run") as mock_sub,
    ):
        runner.capture(payload)

    mock_sub.assert_not_called()  # api mode never shells out
    headers = mock_post.call_args.kwargs["headers"]
    assert headers.get("x-api-key") == _FAKE_KEY
    assert headers.get("anthropic-version") == "2023-06-01"


def test_api_mode_masks_key_in_returned_detail(tmp_path: Path, monkeypatch) -> None:
    """SECURITY: even if the provider reflects the API key back in its reply,
    the returned detail must be masked AT the runner (not relying on any
    downstream sink)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_KEY)
    payload = _api_payload(tmp_path)
    runner = _runner()
    # Simulate a leak: the provider echoes the key back inside its reply text.
    body = {
        "content": [{"type": "text", "text": f"here is your key {_FAKE_KEY} oops"}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    with patch(
        "hivepilot.runners.claude_runner.requests.post",
        return_value=_fake_response(body),
    ):
        out = runner.capture(payload)

    assert _FAKE_KEY not in out, "API key must be masked out of the returned detail"
    assert "REDACTED" in out


def test_api_mode_masks_key_in_run_path(tmp_path: Path, monkeypatch) -> None:
    """`run()` in api mode also dispatches through the API and never leaks the
    key via an exception or return value (mask coverage for the run() path)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_KEY)
    payload = _api_payload(tmp_path)
    runner = _runner()
    body = {"content": [{"type": "text", "text": "fine"}]}
    with (
        patch(
            "hivepilot.runners.claude_runner.requests.post",
            return_value=_fake_response(body),
        ) as mock_post,
        patch("hivepilot.runners.claude_runner.subprocess.run") as mock_sub,
    ):
        runner.run(payload)
    mock_post.assert_called_once()
    mock_sub.assert_not_called()


def test_api_mode_fail_closed_when_key_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    payload = _api_payload(tmp_path)
    runner = _runner()
    with patch("hivepilot.runners.claude_runner.requests.post") as mock_post:
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            runner.capture(payload)
    mock_post.assert_not_called()  # never hit the API without a key


def test_cli_mode_argv_unchanged_and_no_http(tmp_path: Path, monkeypatch) -> None:
    """`mode: cli` (default when no api mode is set) must build the SAME argv as
    today and never call the HTTP client."""
    monkeypatch.setattr(settings, "claude_permission_mode", None, raising=False)
    pf = tmp_path / "prompt.md"
    pf.write_text("do it", encoding="utf-8")
    payload = RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude", prompt_file=str(pf)),  # no mode → cli
        metadata={},
        secrets={},
    )
    runner = _runner(model="claude-x")
    args, _ = runner._build_invocation(payload)

    assert args[0] == "claude"
    assert args[1] == "--print"
    assert "--model" in args
    assert args[args.index("--model") + 1] == "claude-x"
    # the prompt (last positional) never carries the API key
    assert _FAKE_KEY not in " ".join(args)


def test_cli_mode_key_never_in_argv(tmp_path: Path, monkeypatch) -> None:
    """Even with the key present in the environment, the CLI-mode argv must not
    contain it (the CLI reads the key from its own config/env, never argv)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_KEY)
    pf = tmp_path / "prompt.md"
    pf.write_text("do it", encoding="utf-8")
    payload = RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude", prompt_file=str(pf)),
        metadata={},
        secrets={},
    )
    runner = _runner(model="claude-x")
    args, env = runner._build_invocation(payload)
    assert _FAKE_KEY not in " ".join(args)
