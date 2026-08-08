"""A step that fails still spent the money. Today none of it is recorded.

Measured on the production box, 2026-08-08:

    SELECT COUNT(*) FROM steps WHERE status='failed';                  -> 211
    SELECT COUNT(*) FROM steps WHERE status='failed' AND cost_usd IS NULL; -> 211

Every failed step in the history of the deployment, without exception. So
every cost total the system reports is an underestimate of unknown size, and
the runs it under-reports are exactly the ones an operator most wants costed.

The cause is not what it looks like. The cost is not "discarded when the step
fails" — it is *never extracted*. On a non-zero exit `capture()` raises at the
top of its JSON branch, well before the two lines that read the envelope:

    if json_result.returncode != 0:
        ...
        raise RunnerExecutionError(...)      # <- leaves here
    denied = extract_permission_denials(...)  # <- never reached on failure
    parsed = _parse_usage_envelope(...)       # <- never reached on failure

Both of those matter, and the second one is the ironic half:
`extract_permission_denials`' own docstring says "the caller reports these
whether the dispatch succeeded or not", and its motivating example (run 356's
CTO, refused `rtk fd`, dead at 84 969 input tokens) is a *failing* dispatch.
The feature does not fire on the case it was written for.

The envelope below is not invented. It is the real stdout of step 393
(run 360, `cto review`), read back out of the production database — a failing
`claude` dispatch reports `is_error: true` **and** a complete `modelUsage`
block, `permission_denials`, and `"result": "Prompt is too long"`. That last
field is what makes this fixable at all: `_parse_usage_envelope` is
text-first and returns None without it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerExecutionError, RunnerPayload, pop_last_usage
from hivepilot.runners.claude_runner import ClaudeRunner

# Verbatim shape of step 393's stdout (ids shortened, token counts real).
_REAL_FAILURE_ENVELOPE = json.dumps(
    {
        "is_error": True,
        "duration_api_ms": 31520,
        "num_turns": 3,
        "stop_reason": "stop_sequence",
        "total_cost_usd": 1.167931,
        "usage": {
            "input_tokens": 102179,
            "cache_creation_input_tokens": 65903,
            "cache_read_input_tokens": 71082,
            "output_tokens": 1377,
        },
        "modelUsage": {
            "claude-opus-5": {
                "inputTokens": 102181,
                "outputTokens": 1399,
                "cacheReadInputTokens": 71082,
                "cacheCreationInputTokens": 91870,
                "costUSD": 1.167931,
                "canonicalModel": "claude-opus-5",
                "provider": "firstParty",
            }
        },
        "permission_denials": [
            {
                "tool_name": "Bash",
                "tool_use_id": "toolu_011wL5r52een2tHQesALozfE",
                "tool_input": {
                    "command": "rtk fd . surfaces/agent -t d -d 3",
                    "description": "List agent surface directory structure",
                },
            }
        ],
        "terminal_reason": "blocking_limit",
        "subtype": "success",
        "result": "Prompt is too long",
        "type": "result",
    }
)


def _payload(tmp_path: Path) -> RunnerPayload:
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata={"extra_prompt": "do the thing"},
        secrets={},
    )


def _runner() -> ClaudeRunner:
    return ClaudeRunner(RunnerDefinition(name="claude", kind="claude", command="claude"), settings)


def _capture_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, stderr: str = ""
):
    """Drive `capture()` through a non-zero exit and return the raised error."""
    runner = _runner()
    monkeypatch.setattr(runner.settings, "claude_capture_usage", True, raising=False)
    pop_last_usage()  # clear any stash left by an earlier test
    with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
        m.return_value = MagicMock(returncode=1, stdout=stdout, stderr=stderr)
        with pytest.raises(RunnerExecutionError) as excinfo:
            runner.capture(_payload(tmp_path))
    return excinfo.value


class TestTheCostSurvivesTheFailure:
    def test_cost_is_captured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """$1.167931 was really spent. Recording None for it is a lie the
        rest of the cost reporting is built on."""
        _capture_failure(tmp_path, monkeypatch, _REAL_FAILURE_ENVELOPE)

        usage = pop_last_usage()
        assert usage is not None, "a failing dispatch must still stash its usage"
        assert usage.cost_usd == pytest.approx(1.167931)

    def test_cache_tokens_are_captured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cache reads bill at 0.1x and cache creation at 1.25x, so folding
        them into the base counts would misprice the step even when the
        total is right."""
        _capture_failure(tmp_path, monkeypatch, _REAL_FAILURE_ENVELOPE)

        usage = pop_last_usage()
        assert usage is not None
        assert usage.cache_read_tokens == 71082
        assert usage.cache_creation_tokens == 91870

    def test_model_is_captured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Which model burned the money is half the diagnosis — an opus
        failure and a haiku failure are not the same finding."""
        _capture_failure(tmp_path, monkeypatch, _REAL_FAILURE_ENVELOPE)

        usage = pop_last_usage()
        assert usage is not None
        assert usage.model == "claude-opus-5"

    def test_still_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Capturing the cost must not turn a failure into a success. The
        step failed; only its accounting changes."""
        err = _capture_failure(tmp_path, monkeypatch, _REAL_FAILURE_ENVELOPE)

        assert err.context["exit_code"] == 1


class TestNothingIsInvented:
    def test_plain_stderr_failure_stashes_no_usage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash with no envelope has no cost to report. Reporting 0.0
        would be worse than reporting nothing — it reads as 'this was free'."""
        _capture_failure(tmp_path, monkeypatch, stdout="", stderr="boom")

        assert pop_last_usage() is None

    def test_envelope_without_result_stashes_no_usage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_parse_usage_envelope` is text-first by design. A shape it
        refuses must stay refused on the failure path too — the failure path
        does not get its own, looser parser."""
        no_result = json.dumps({"is_error": True, "total_cost_usd": 9.99})
        _capture_failure(tmp_path, monkeypatch, no_result)

        assert pop_last_usage() is None


class TestDenialsAreReportedOnTheFailingDispatch:
    def test_denied_tool_reaches_the_error_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Run 356 died because `rtk fd` was refused and nothing said so.
        A log line stays on the box; the error context reaches `detail` in
        the database, where the diagnosis actually gets read."""
        err = _capture_failure(tmp_path, monkeypatch, _REAL_FAILURE_ENVELOPE)

        assert any("rtk fd" in d for d in err.context["denied_tools"])

    def test_the_report_names_the_role_it_tells_you_to_fix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The remediation text says "add the pattern to *this role's*
        allowed_tools" — so it has to say which role. Every denial ever
        reported logged `"role": null`, because the reporting read
        `payload.step.metadata` while the orchestrator writes the role to
        `payload.metadata`."""
        runner = _runner()
        monkeypatch.setattr(runner.settings, "claude_capture_usage", True, raising=False)
        payload = RunnerPayload(
            project_name="p",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(name="s", runner="claude"),
            metadata={"extra_prompt": "go", "role": "chief_of_staff"},
            secrets={},
        )
        pop_last_usage()
        with caplog.at_level("WARNING"):
            with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
                m.return_value = MagicMock(returncode=1, stdout=_REAL_FAILURE_ENVELOPE, stderr="")
                with pytest.raises(RunnerExecutionError):
                    runner.capture(payload)
        pop_last_usage()

        assert "chief_of_staff" in caplog.text

    def test_no_denials_is_an_empty_list_not_a_missing_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Consumers should not have to distinguish 'no denials' from
        'this build does not report denials'."""
        clean = json.dumps({"is_error": True, "result": "nope", "total_cost_usd": 0.5})
        err = _capture_failure(tmp_path, monkeypatch, clean)

        assert err.context["denied_tools"] == []


class TestTheOrchestratorRecordsIt:
    """Capturing usage is half the fix; the failure path must also thread it
    into `record_step`, exactly as `_record_step_success` does."""

    def test_failed_step_records_cost(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hivepilot import orchestrator
        from hivepilot.runners.base import UsageInfo

        seen: dict[str, Any] = {}

        def _spy(*args: Any, **kwargs: Any) -> None:
            seen["args"] = args
            seen["kwargs"] = kwargs

        monkeypatch.setattr(orchestrator.state_service, "record_step", _spy)
        orchestrator._record_step_failure(
            run_id=1,
            step_name="cto review",
            detail="claude exited 1",
            provider="claude",
            model=None,
            usage=UsageInfo(
                input_tokens=102181,
                output_tokens=1399,
                cost_usd=1.167931,
                model="claude-opus-5",
                cache_read_tokens=71082,
                cache_creation_tokens=91870,
            ),
            role="cto",
        )

        assert seen["args"][2] == "failed"
        assert seen["kwargs"]["cost_usd"] == pytest.approx(1.167931)
        assert seen["kwargs"]["cache_read_tokens"] == 71082
        assert seen["kwargs"]["model"] == "claude-opus-5"

    def test_failed_step_without_usage_is_byte_compatible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-claude runners and flag-off installs must keep issuing the
        exact call they issued before — no new None-valued columns."""
        from hivepilot import orchestrator

        seen: dict[str, Any] = {}

        def _spy(*args: Any, **kwargs: Any) -> None:
            seen["args"] = args
            seen["kwargs"] = kwargs

        monkeypatch.setattr(orchestrator.state_service, "record_step", _spy)
        orchestrator._record_step_failure(
            run_id=1,
            step_name="s",
            detail="boom",
            provider=None,
            model=None,
            usage=None,
            role=None,
        )

        assert "cost_usd" not in seen["kwargs"]
        assert seen["args"][2] == "failed"
