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

    runner._post_json = fake_post  # type: ignore[method-assign,assignment]

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

    runner._post_json = fake_post  # type: ignore[method-assign,assignment]

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


# ── Phase 24 follow-up: non-claude (API-mode) usage capture ──────────────────
#
# _run_api/_post_json now return the parsed response body (previously
# discarded — see docstrings on _extract_api_text/_extract_api_usage below).
# capture() dispatches on mode exactly like run() already did, and — new in
# this sprint — actually performs the API call for mode == "api" (previously
# capture() ignored mode entirely and always took the CLI-subprocess branch,
# so API mode was unreachable from the real step-execution path; only run()
# — which no in-tree caller invokes for prompt-cli kinds — dispatched to
# _run_api). No opt-in flag: the API SDKs already return usage in the same
# request that produces the text, so this is non-invasive.


def _api_payload(tmp_path: Path, metadata: dict | None = None) -> RunnerPayload:
    pf = tmp_path / "p.md"
    pf.write_text("do it", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="api", prompt_file=str(pf)),
        metadata=metadata or {},
        secrets={},
    )


def _api_runner(provider: str, model: str = "gpt-4") -> PromptCliRunner:
    return PromptCliRunner(
        RunnerDefinition(
            name="cli",
            kind="api",
            command="echo",
            options={"mode": "api", "api_provider": provider, "api_model": model},
        ),
        settings,
    )


def _fake_response(json_body):  # noqa: ANN001
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = json_body
    resp.text = ""
    return resp


class TestApiModeCaptureUsage:
    def test_openai_response_sets_text_and_usage(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import patch

        from hivepilot.runners.base import pop_last_usage

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        payload = _api_payload(tmp_path)
        runner = _api_runner("openai", model="gpt-4")
        body = {
            "choices": [{"message": {"content": "HELLO FROM OPENAI"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            "model": "gpt-4-0613",
        }
        with patch(
            "hivepilot.runners.prompt_cli_runner.requests.post",
            return_value=_fake_response(body),
        ):
            out = runner.capture(payload)

        assert out == "HELLO FROM OPENAI"
        usage = pop_last_usage()
        assert usage is not None
        assert usage.input_tokens == 10
        assert usage.output_tokens == 20
        assert usage.cost_usd is None
        assert usage.model == "gpt-4-0613"

    def test_anthropic_response_sets_text_and_usage(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import patch

        from hivepilot.runners.base import pop_last_usage

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        payload = _api_payload(tmp_path)
        runner = _api_runner("anthropic", model="claude-3-haiku-20240307")
        body = {
            "content": [{"type": "text", "text": "HI THERE"}],
            "usage": {"input_tokens": 5, "output_tokens": 7},
            "model": "claude-3-haiku-20240307",
        }
        with patch(
            "hivepilot.runners.prompt_cli_runner.requests.post",
            return_value=_fake_response(body),
        ):
            out = runner.capture(payload)

        assert out == "HI THERE"
        usage = pop_last_usage()
        assert usage is not None
        assert usage.input_tokens == 5
        assert usage.output_tokens == 7
        assert usage.cost_usd is None
        assert usage.model == "claude-3-haiku-20240307"

    def test_google_response_sets_text_and_usage_no_model(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Gemini's generateContent response has no top-level 'model' field —
        must NOT invent one; model stays None."""
        from unittest.mock import patch

        from hivepilot.runners.base import pop_last_usage

        monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
        payload = _api_payload(tmp_path)
        runner = _api_runner("google", model="gemini-pro")
        body = {
            "candidates": [{"content": {"parts": [{"text": "HOLA"}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4},
        }
        with patch(
            "hivepilot.runners.prompt_cli_runner.requests.post",
            return_value=_fake_response(body),
        ):
            out = runner.capture(payload)

        assert out == "HOLA"
        usage = pop_last_usage()
        assert usage is not None
        assert usage.input_tokens == 3
        assert usage.output_tokens == 4
        assert usage.model is None

    def test_response_without_usage_sets_no_usage_output_unchanged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from unittest.mock import patch

        from hivepilot.runners.base import pop_last_usage

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        payload = _api_payload(tmp_path)
        runner = _api_runner("openai")
        body = {"choices": [{"message": {"content": "NO USAGE HERE"}}]}
        with patch(
            "hivepilot.runners.prompt_cli_runner.requests.post",
            return_value=_fake_response(body),
        ):
            out = runner.capture(payload)

        assert out == "NO USAGE HERE"
        assert pop_last_usage() is None

    def test_malformed_usage_field_swallowed_no_crash(self, tmp_path: Path, monkeypatch) -> None:
        """usage field of the wrong type must not crash the step — degrade to
        no usage captured, output text still returned normally."""
        from unittest.mock import patch

        from hivepilot.runners.base import pop_last_usage

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        payload = _api_payload(tmp_path)
        runner = _api_runner("openai")
        body = {"choices": [{"message": {"content": "STILL OK"}}], "usage": "not-a-dict"}
        with patch(
            "hivepilot.runners.prompt_cli_runner.requests.post",
            return_value=_fake_response(body),
        ):
            out = runner.capture(payload)

        assert out == "STILL OK"
        assert pop_last_usage() is None

    def test_malformed_top_level_response_swallowed_no_crash(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A response body that isn't even a dict (unexpected provider shape)
        must not crash the step."""
        from unittest.mock import patch

        from hivepilot.runners.base import pop_last_usage

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        payload = _api_payload(tmp_path)
        runner = _api_runner("openai")
        with patch(
            "hivepilot.runners.prompt_cli_runner.requests.post",
            return_value=_fake_response([1, 2, 3]),
        ):
            out = runner.capture(payload)

        assert out == ""
        assert pop_last_usage() is None

    def test_usage_extraction_warning_has_no_secret_or_response_content(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """When usage extraction hits an unexpected shape and logs a warning,
        the log line must never carry the API key, prompt text, or response
        body content."""
        import logging
        from unittest.mock import patch

        from hivepilot.runners.base import pop_last_usage

        monkeypatch.setenv("OPENAI_API_KEY", "sk-TOTALLY-SECRET-KEY")
        payload = _api_payload(tmp_path)
        runner = _api_runner("openai")

        # A dict subclass whose .get() explodes — simulates a usage envelope
        # that structurally looks fine (isinstance(usage, dict) is True, and
        # it json-serializes fine for the pre-existing api_runner.response
        # info log) but raises when actually read, forcing the exception
        # path inside _extract_api_usage's try/except.
        class _ExplodingDict(dict):
            def get(self, key, default=None):  # noqa: ANN001
                raise RuntimeError("boom")

        body = {
            "choices": [{"message": {"content": "SECRET-RESPONSE-BODY-TEXT"}}],
            "usage": _ExplodingDict({"prompt_tokens": 1}),
        }
        with (
            caplog.at_level(logging.WARNING),
            patch(
                "hivepilot.runners.prompt_cli_runner.requests.post",
                return_value=_fake_response(body),
            ),
        ):
            out = runner.capture(payload)

        assert out == "SECRET-RESPONSE-BODY-TEXT"
        assert pop_last_usage() is None
        log_text = caplog.text
        assert "prompt_cli_runner.api_usage_extraction_failed" in log_text
        assert "sk-TOTALLY-SECRET-KEY" not in log_text
        assert "SECRET-RESPONSE-BODY-TEXT" not in log_text

    def test_output_text_unchanged_regression(self, tmp_path: Path, monkeypatch) -> None:
        """Same OpenAI-shaped body, asserting the returned text is exactly
        the message content — the output contract is unchanged by adding
        usage capture."""
        from unittest.mock import patch

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        payload = _api_payload(tmp_path)
        runner = _api_runner("openai")
        body = {"choices": [{"message": {"content": "EXACT TEXT"}}], "usage": {}}
        with patch(
            "hivepilot.runners.prompt_cli_runner.requests.post",
            return_value=_fake_response(body),
        ):
            out = runner.capture(payload)
        assert out == "EXACT TEXT"


class TestApiModeUsagePersistsViaRecordStep:
    def test_captured_usage_persists_through_record_step(self, tmp_path: Path, monkeypatch) -> None:
        """End-to-end (minus the real HTTP call): a codex-kind runner in API
        mode captures usage through capture(), and the orchestrator's
        existing _record_step_success() (already wired for any runner via
        pop_last_usage()) persists it via the REAL state_service.record_step
        into an isolated temp DB (autouse _isolate_state_db fixture)."""
        from unittest.mock import patch

        from hivepilot.orchestrator import _record_step_success
        from hivepilot.runners.base import pop_last_usage
        from hivepilot.services.state_service import get_steps_for_run

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        payload = _api_payload(tmp_path)
        runner = _api_runner("openai", model="gpt-4")
        body = {
            "choices": [{"message": {"content": "DONE"}}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 8},
            "model": "gpt-4-0613",
        }
        with patch(
            "hivepilot.runners.prompt_cli_runner.requests.post",
            return_value=_fake_response(body),
        ):
            runner.capture(payload)

        usage = pop_last_usage()
        assert usage is not None

        _record_step_success(run_id=1, step_name="s", provider="openai", model=None, usage=usage)

        rows = get_steps_for_run(1)
        assert len(rows) == 1
        row = rows[0]
        assert row["input_tokens"] == 42
        assert row["output_tokens"] == 8
        assert row["model"] == "gpt-4-0613"
        assert row["provider"] == "openai"
