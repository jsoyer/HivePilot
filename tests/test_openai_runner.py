"""`openai` — API-only built-in runner for any OpenAI-compatible
`/chat/completions` endpoint (OpenAI, OpenCode Zen, Ollama Cloud, …) — HP-18.

`OpenAiCompatRunner` is a thin subclass of `PromptCliRunner` that reuses the
EXISTING `openai` branch of `PromptCliRunner._run_api` and, exactly like
`OpenRouterRunner`, forces `api_provider="openai"`, is API-only, fails closed
on a missing `OPENAI_API_KEY`, and masks the key AT the runner. The endpoint
is taken from `OPENAI_BASE_URL` (default `https://api.openai.com/v1`), which is
what lets it point at a hosted OSS gateway.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerModeUnsupportedError, RunnerPayload
from hivepilot.runners.openai_runner import OpenAiCompatRunner

_FAKE_KEY = "sk-TESTKEY-abcdef0123456789-do-not-log"


def _payload(tmp_path: Path, metadata: dict | None = None) -> RunnerPayload:
    pf = tmp_path / "prompt.md"
    pf.write_text("do the thing", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="openai", prompt_file=str(pf), metadata=metadata or {}),
        metadata={},
        secrets={},
    )


def _runner(options: dict | None = None, env: dict | None = None) -> OpenAiCompatRunner:
    opts: dict = {"api_model": "glm-5.3-flash"}
    if options:
        opts.update(options)
    return OpenAiCompatRunner(
        RunnerDefinition(name="openai", kind="openai", options=opts, env=env or {}),
        settings,
    )


def _fake_response(json_body: dict, *, ok: bool = True, status_code: int = 200, text: str = ""):
    resp = MagicMock()
    resp.ok = ok
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.content = b"x"
    resp.text = text
    return resp


class TestSupportedModes:
    def test_supported_modes_is_api_only(self) -> None:
        assert OpenAiCompatRunner.supported_modes == frozenset({"api"})


class TestForcesOpenaiProvider:
    def test_api_provider_forced(self) -> None:
        assert _runner().definition.options["api_provider"] == "openai"

    def test_api_provider_forced_even_when_caller_supplied_a_different_one(self) -> None:
        assert _runner(options={"api_provider": "openrouter"}).definition.options["api_provider"] == "openai"

    def test_construction_does_not_mutate_the_original_definition(self) -> None:
        original = RunnerDefinition(name="openai", kind="openai", options={"api_model": "x"})
        OpenAiCompatRunner(original, settings)
        assert "api_provider" not in original.options


class TestCliModeRejected:
    def test_default_cli_mode_rejected_before_any_http(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", _FAKE_KEY)
        runner = _runner()
        payload = _payload(tmp_path)  # no mode -> resolves to "cli"
        with patch("hivepilot.runners.prompt_cli_runner.requests.post") as mock_post:
            with pytest.raises(RunnerModeUnsupportedError, match="openai"):
                runner.capture(payload)
        mock_post.assert_not_called()


class TestApiModeSuccess:
    def test_default_endpoint_and_returns_text(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", _FAKE_KEY)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.delenv("OPENAI_API_BASE", raising=False)
        payload = _payload(tmp_path, metadata={"mode": "api"})
        body = {"choices": [{"message": {"content": "HELLO"}}], "model": "glm-5.3-flash"}
        with patch(
            "hivepilot.runners.prompt_cli_runner.requests.post",
            return_value=_fake_response(body),
        ) as mock_post:
            out = _runner().capture(payload)
        assert out == "HELLO"
        url = mock_post.call_args.kwargs.get("url") or mock_post.call_args.args[0]
        assert url == "https://api.openai.com/v1/chat/completions"
        headers = mock_post.call_args.kwargs.get("headers", {})
        assert headers.get("Authorization") == f"Bearer {_FAKE_KEY}"

    def test_custom_base_url_from_env_targets_the_gateway(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", _FAKE_KEY)
        payload = _payload(tmp_path, metadata={"mode": "api"})
        # Base URL threaded via the runner definition's env (how the concierge
        # passes chatops_concierge_api_base).
        runner = _runner(env={"OPENAI_BASE_URL": "https://opencode.ai/zen/v1"})
        body = {"choices": [{"message": {"content": "ok"}}]}
        with patch(
            "hivepilot.runners.prompt_cli_runner.requests.post",
            return_value=_fake_response(body),
        ) as mock_post:
            runner.capture(payload)
        url = mock_post.call_args.kwargs.get("url") or mock_post.call_args.args[0]
        assert url == "https://opencode.ai/zen/v1/chat/completions"


class TestFailClosedMissingKey:
    def test_missing_key_raises_and_makes_no_http_call(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        payload = _payload(tmp_path, metadata={"mode": "api"})
        with patch("hivepilot.runners.prompt_cli_runner.requests.post") as mock_post:
            with pytest.raises(RuntimeError, match=r"\$\{secret:OPENAI_API_KEY\}"):
                _runner().capture(payload)
        mock_post.assert_not_called()


class TestMaskApiKeyInDetail:
    def test_mask_key_when_provider_echoes_it_back(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", _FAKE_KEY)
        payload = _payload(tmp_path, metadata={"mode": "api"})
        body = {"choices": [{"message": {"content": f"your key {_FAKE_KEY} oops"}}]}
        with patch(
            "hivepilot.runners.prompt_cli_runner.requests.post",
            return_value=_fake_response(body),
        ):
            out = _runner().capture(payload)
        assert _FAKE_KEY not in out
        assert "REDACTED" in out
