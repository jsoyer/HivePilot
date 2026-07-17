"""Sprint 2 (runner-defaults-plugins-mode PRD): `openrouter` — new API-only
built-in runner.

`OpenRouterRunner` is a thin subclass of `PromptCliRunner` that reuses the
EXISTING OpenRouter branch of `PromptCliRunner._run_api` (unchanged,
untouched invocation logic) and adds:

- `supported_modes = frozenset({"api"})` — openrouter has no CLI binary of
  its own, so a `mode: cli` step (explicit OR the system-wide default when
  no mode is configured at all) must fail validation via the Sprint-1
  `validate_runner_mode` contract, BEFORE any HTTP call.
- `api_provider` is force-set to `"openrouter"` at construction, regardless
  of any stray `options["api_provider"]` a caller might supply.
- Fail-closed: a missing `OPENROUTER_API_KEY` raises a clear error naming
  `${secret:OPENROUTER_API_KEY}` and performs NO HTTP call.
- Security: the resolved key is masked out of the returned/raised text AT
  the runner (mirrors `ClaudeRunner._run_api`'s `register_secret_value` +
  `redact_text` choke point) — never relies on a downstream sink alone,
  since `RunResult.detail` is known-unredacted at that choke point.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerModeUnsupportedError, RunnerPayload, pop_last_usage
from hivepilot.runners.openrouter_runner import OpenRouterRunner

_FAKE_KEY = "sk-or-TESTKEY-abcdef0123456789-do-not-log"


def _payload(
    tmp_path: Path, metadata: dict | None = None, secrets: dict | None = None
) -> RunnerPayload:
    pf = tmp_path / "prompt.md"
    pf.write_text("do the thing", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="openrouter", prompt_file=str(pf), metadata=metadata or {}),
        metadata={},
        secrets=secrets or {},
    )


def _runner(options: dict | None = None, model: str | None = "openai/gpt-4o") -> OpenRouterRunner:
    opts: dict = {"api_model": model}
    if options:
        opts.update(options)
    return OpenRouterRunner(
        RunnerDefinition(name="openrouter", kind="openrouter", options=opts),
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
        assert OpenRouterRunner.supported_modes == frozenset({"api"})

    def test_supported_modes_does_not_include_cli(self) -> None:
        assert "cli" not in OpenRouterRunner.supported_modes


class TestForcesOpenrouterProvider:
    def test_api_provider_forced_by_default(self) -> None:
        runner = _runner()
        assert runner.definition.options["api_provider"] == "openrouter"

    def test_api_provider_forced_even_when_caller_supplied_a_different_one(self) -> None:
        runner = _runner(options={"api_provider": "openai"})
        assert runner.definition.options["api_provider"] == "openrouter"

    def test_construction_does_not_mutate_the_original_definition(self) -> None:
        original = RunnerDefinition(
            name="openrouter", kind="openrouter", options={"api_model": "x"}
        )
        OpenRouterRunner(original, settings)
        assert "api_provider" not in original.options


class TestCliModeRejectedAtValidation:
    """Acceptance: a mode:cli (or default cli) resolved for an openrouter step
    fails validation via the Sprint-1 supported_modes check."""

    def test_explicit_cli_mode_rejected_on_capture(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_KEY)
        runner = _runner()
        payload = _payload(tmp_path, metadata={"mode": "cli"})
        with pytest.raises(RunnerModeUnsupportedError, match="openrouter"):
            runner.capture(payload)

    def test_default_mode_with_no_config_at_all_is_rejected_on_capture(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_KEY)
        runner = _runner()
        payload = _payload(tmp_path)  # no mode metadata anywhere -> resolves to "cli"
        with pytest.raises(RunnerModeUnsupportedError, match="openrouter"):
            runner.capture(payload)

    def test_explicit_cli_mode_rejected_on_run(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_KEY)
        runner = _runner()
        payload = _payload(tmp_path, metadata={"mode": "cli"})
        with pytest.raises(RunnerModeUnsupportedError, match="openrouter"):
            runner.run(payload)

    def test_rejection_message_names_supported_modes(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_KEY)
        runner = _runner()
        payload = _payload(tmp_path, metadata={"mode": "cli"})
        with pytest.raises(RunnerModeUnsupportedError) as exc_info:
            runner.capture(payload)
        message = str(exc_info.value)
        assert "cli" in message
        assert "api" in message

    def test_cli_rejection_never_performs_an_http_call(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_KEY)
        runner = _runner()
        payload = _payload(tmp_path, metadata={"mode": "cli"})
        with patch("hivepilot.runners.prompt_cli_runner.requests.post") as mock_post:
            with pytest.raises(RunnerModeUnsupportedError):
                runner.capture(payload)
        mock_post.assert_not_called()


class TestApiModeSuccess:
    def test_capture_calls_openrouter_endpoint_and_returns_text(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_KEY)
        payload = _payload(tmp_path, metadata={"mode": "api"})
        runner = _runner()
        body = {
            "choices": [{"message": {"content": "HELLO FROM OPENROUTER"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 9},
            "model": "openai/gpt-4o",
        }
        with patch(
            "hivepilot.runners.prompt_cli_runner.requests.post",
            return_value=_fake_response(body),
        ) as mock_post:
            out = runner.capture(payload)

        assert out == "HELLO FROM OPENROUTER"
        call = mock_post.call_args
        url = call.args[0] if call.args else call.kwargs.get("url", "")
        assert url == "https://openrouter.ai/api/v1/chat/completions"
        usage = pop_last_usage()
        assert usage is not None
        assert usage.input_tokens == 5
        assert usage.output_tokens == 9

    def test_key_travels_only_in_header_never_a_subprocess(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_KEY)
        payload = _payload(tmp_path, metadata={"mode": "api"})
        runner = _runner()
        body = {"choices": [{"message": {"content": "ok"}}]}
        with (
            patch(
                "hivepilot.runners.prompt_cli_runner.requests.post",
                return_value=_fake_response(body),
            ) as mock_post,
            patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_sub,
        ):
            runner.capture(payload)
        mock_sub.assert_not_called()
        headers = mock_post.call_args.kwargs.get("headers", {})
        assert headers.get("Authorization") == f"Bearer {_FAKE_KEY}"

    def test_run_path_also_dispatches_through_the_api(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_KEY)
        payload = _payload(tmp_path, metadata={"mode": "api"})
        runner = _runner()
        body = {"choices": [{"message": {"content": "ok"}}]}
        with (
            patch(
                "hivepilot.runners.prompt_cli_runner.requests.post",
                return_value=_fake_response(body),
            ) as mock_post,
            patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_sub,
        ):
            runner.run(payload)
        mock_post.assert_called_once()
        mock_sub.assert_not_called()


class TestFailClosedMissingKey:
    def test_missing_key_raises_clear_error_and_makes_no_http_call(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        payload = _payload(tmp_path, metadata={"mode": "api"})
        runner = _runner()
        with patch("hivepilot.runners.prompt_cli_runner.requests.post") as mock_post:
            with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
                runner.capture(payload)
        mock_post.assert_not_called()

    def test_missing_key_error_names_the_secret_ref_syntax(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        payload = _payload(tmp_path, metadata={"mode": "api"})
        runner = _runner()
        with pytest.raises(RuntimeError, match=r"\$\{secret:OPENROUTER_API_KEY\}"):
            runner.capture(payload)

    def test_missing_key_also_fails_closed_on_run(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        payload = _payload(tmp_path, metadata={"mode": "api"})
        runner = _runner()
        with patch("hivepilot.runners.prompt_cli_runner.requests.post") as mock_post:
            with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
                runner.run(payload)
        mock_post.assert_not_called()


class TestMaskOpenrouterApiKeyInDetail:
    """SECURITY: even if the provider reflects the API key back in its reply,
    or an HTTP failure echoes it, the text that would become RunResult.detail
    must be masked AT the runner — never relying on a downstream sink."""

    def test_mask_key_when_provider_echoes_it_back_in_reply(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_KEY)
        payload = _payload(tmp_path, metadata={"mode": "api"})
        runner = _runner()
        body = {"choices": [{"message": {"content": f"here is your key {_FAKE_KEY} oops"}}]}
        with patch(
            "hivepilot.runners.prompt_cli_runner.requests.post",
            return_value=_fake_response(body),
        ):
            out = runner.capture(payload)

        assert _FAKE_KEY not in out, "API key must be masked out of the returned detail"
        assert "REDACTED" in out

    def test_mask_key_in_http_failure_exception_message(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", _FAKE_KEY)
        payload = _payload(tmp_path, metadata={"mode": "api"})
        runner = _runner()
        resp = _fake_response(
            {}, ok=False, status_code=401, text=f"invalid credentials for key {_FAKE_KEY}"
        )
        with patch(
            "hivepilot.runners.prompt_cli_runner.requests.post",
            return_value=resp,
        ):
            with pytest.raises(RuntimeError) as exc_info:
                runner.capture(payload)

        assert _FAKE_KEY not in str(exc_info.value), (
            "API key must never leak via an HTTP-failure exception message"
        )
