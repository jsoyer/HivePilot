"""Tests for hivepilot.runners.base — UsageInfo + last-usage stash helpers.

Phase 24b.2a — opt-in usage capture. The stash (ContextVar-backed) lets a
runner's ``capture()`` hand token/cost/model usage back to its caller without
changing ``capture()``'s ``str`` return contract.
"""

from __future__ import annotations

from pathlib import Path

from hivepilot.config import Settings
from hivepilot.models import EffectiveLessonsConfig, ProjectConfig, TaskStep
from hivepilot.runners.base import (
    RunnerExecutionError,
    RunnerPayload,
    UsageInfo,
    classify_signal_exit,
    detect_noop_permission_response,
    pop_last_usage,
    prompt_file_not_found_message,
    set_last_usage,
)


class TestClassifySignalExit:
    """Bug 1 (run 243, live incident): the `claude` subprocess was SIGKILLed
    (exit_code=-9, empty stderr since the OS never let it write anything) and
    the pipeline recorded the run as `test_failure` -- actively misleading,
    since no test ever ran. `classify_signal_exit` distinguishes a POSIX
    signal death (a negative exit code, `-N` == signal N per the
    `subprocess`/`os.WIFSIGNALED` convention) -- an INFRASTRUCTURE failure --
    from the command actually running and reporting its own failure via a
    positive exit code, which must classify exactly as before."""

    def test_none_for_missing_exit_code(self) -> None:
        assert classify_signal_exit(None) is None

    def test_none_for_zero_exit_code(self) -> None:
        assert classify_signal_exit(0) is None

    def test_none_for_positive_exit_code(self) -> None:
        """A command that ran and reported its own (genuine) failure via a
        positive exit code is NEVER a signal death -- regression guard for
        "do not paper over real test failures"."""
        assert classify_signal_exit(1) is None
        assert classify_signal_exit(2) is None
        assert classify_signal_exit(127) is None

    def test_sigkill_is_not_deliberate_and_names_the_signal(self) -> None:
        death = classify_signal_exit(-9)
        assert death is not None
        assert death.signal_number == 9
        assert death.signal_name == "SIGKILL"
        assert death.deliberate is False

    def test_sigkill_message_mentions_oom_and_available_memory(self) -> None:
        """The message must be actionable (WHAT/WHY/FIX spirit): name the
        signal, point at the OOM killer, and tell the operator to check
        AVAILABLE memory (not total/free) and container memory limits."""
        death = classify_signal_exit(-9)
        assert death is not None
        message = death.message.lower()
        assert "sigkill" in message
        assert "oom" in message or "out-of-memory" in message or "out of memory" in message
        assert "available" in message
        assert "memory" in message

    def test_sigterm_is_deliberate_and_not_reported_as_a_crash(self) -> None:
        death = classify_signal_exit(-15)
        assert death is not None
        assert death.signal_number == 15
        assert death.signal_name == "SIGTERM"
        assert death.deliberate is True
        assert "crash" not in death.message.lower()

    def test_sigabrt_and_sigsegv_are_genuine_crashes(self) -> None:
        for code, name in ((-6, "SIGABRT"), (-11, "SIGSEGV")):
            death = classify_signal_exit(code)
            assert death is not None
            assert death.signal_name == name
            assert death.deliberate is False

    def test_unknown_negative_signal_falls_back_gracefully(self) -> None:
        """A signal number Python's `signal` module doesn't recognise must
        never raise -- it degrades to a generic (still non-deliberate,
        still-classified) SignalDeath rather than crashing the classifier
        itself while handling an already-unusual failure."""
        death = classify_signal_exit(-987)
        assert death is not None
        assert death.deliberate is False
        assert "987" in death.signal_name or "987" in death.message


def _payload(**overrides: object) -> RunnerPayload:
    base = dict(
        project_name="p",
        project=ProjectConfig(path=Path(".")),
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata={},
    )
    base.update(overrides)
    return RunnerPayload(**base)  # type: ignore[arg-type]


def test_runner_payload_lessons_defaults_to_none() -> None:
    """Per-pipeline-lessons-yaml PRD, Sprint 2: `RunnerPayload.lessons` is
    OPTIONAL and defaults to `None` -- backward-compatible for every
    existing call site that doesn't pass it (falls back to the settings
    floor at the consumption site, see `knowledge_service.
    build_lessons_context`)."""
    payload = _payload()
    assert payload.lessons is None


def test_runner_payload_accepts_explicit_effective_lessons_config() -> None:
    effective = EffectiveLessonsConfig(
        enable_distillation=True,
        enable_semantic=False,
        distill_runner="claude",
        distill_model=None,
        min_score=0.5,
        inject_limit=5,
    )
    payload = _payload(lessons=effective)
    assert payload.lessons is effective


def test_usage_info_defaults_all_none() -> None:
    usage = UsageInfo()
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.cost_usd is None
    assert usage.model is None
    assert usage.cache_read_tokens is None
    assert usage.cache_creation_tokens is None


def test_usage_info_accepts_cache_token_fields() -> None:
    """Cache read/creation tokens (prompt caching) are billed at DIFFERENT
    rates than base input/output tokens -- they must be tracked as distinct
    fields, never folded into input_tokens (see the usage-capture-modelusage
    fix)."""
    usage = UsageInfo(
        input_tokens=1,
        output_tokens=2,
        cache_read_tokens=100,
        cache_creation_tokens=200,
    )
    assert usage.cache_read_tokens == 100
    assert usage.cache_creation_tokens == 200


def test_usage_info_is_frozen() -> None:
    usage = UsageInfo(input_tokens=1)
    try:
        usage.input_tokens = 2  # type: ignore[misc]
        raised = False
    except Exception:
        raised = True
    assert raised, "UsageInfo must be immutable (frozen dataclass)"


def test_pop_last_usage_defaults_to_none() -> None:
    """Nothing stashed yet -> None, never an invented value."""
    assert pop_last_usage() is None


def test_set_then_pop_returns_the_stashed_usage() -> None:
    usage = UsageInfo(input_tokens=10, output_tokens=20, cost_usd=0.01, model="claude-x")
    set_last_usage(usage)
    assert pop_last_usage() is usage


def test_pop_clears_the_stash() -> None:
    """A second pop after the first must return None -- no stale leakage
    into whatever step reads next."""
    set_last_usage(UsageInfo(input_tokens=1))
    pop_last_usage()
    assert pop_last_usage() is None


def test_set_none_clears_stash() -> None:
    set_last_usage(UsageInfo(input_tokens=1))
    set_last_usage(None)
    assert pop_last_usage() is None


class TestRunnerExecutionError:
    """Explicit-failure-logs sprint, Part A.1: a runner failure carries
    structured `context` a caller can merge into a log call, alongside a
    plain-text `str()` for backward-compat callers."""

    def test_str_is_the_plain_message(self) -> None:
        exc = RunnerExecutionError("claude exited 1: boom", context={"exit_code": 1})
        assert str(exc) == "claude exited 1: boom"

    def test_context_defaults_to_empty_dict(self) -> None:
        exc = RunnerExecutionError("boom")
        assert exc.context == {}

    def test_context_carries_structured_fields(self) -> None:
        exc = RunnerExecutionError(
            "claude exited 1: boom",
            context={"exit_code": 1, "runner_kind": "claude", "hint": "some hint"},
        )
        assert exc.context["exit_code"] == 1
        assert exc.context["runner_kind"] == "claude"
        assert exc.context["hint"] == "some hint"

    def test_is_a_runtime_error(self) -> None:
        assert isinstance(RunnerExecutionError("boom"), RuntimeError)


class TestDetectNoopPermissionResponse:
    """Bug 2 (live): a `developer` stage replied "I need your approval to run
    shell commands... Should I proceed?", wrote no files, and `claude` still
    exited 0 -- the orchestrator recorded the step as a SUCCESS. This helper
    is the conservative detector that turns that class of response into a
    failure signal, wired at the orchestrator's step-recording path."""

    def test_none_for_empty_or_none_text(self) -> None:
        assert detect_noop_permission_response("") is None
        assert detect_noop_permission_response(None) is None  # type: ignore[arg-type]

    def test_matches_the_exact_live_incident_text(self) -> None:
        text = "I need your approval to run shell commands in this session. Should I proceed?"
        reason = detect_noop_permission_response(text)
        assert reason is not None
        assert "permission" in reason.lower() or "approval" in reason.lower()

    def test_matches_various_positive_phrasings_case_insensitively(self) -> None:
        positives = [
            "I NEED YOUR APPROVAL before touching the filesystem.",
            "I would need permission to modify these files.",
            "This action requires approval before I can continue.",
            "That command cannot be used with root privileges here.",
            "Should I proceed with deleting the branch?",
            "The tool call needs a permission grant from the operator.",
            "Unfortunately I don't have permission to execute this.",
        ]
        for text in positives:
            assert detect_noop_permission_response(text) is not None, text

    def test_does_not_false_positive_on_approval_workflow_prose(self) -> None:
        """A document that merely DISCUSSES an approval workflow (never asks
        for one) must still be treated as real, completed work."""
        text = (
            "# Approval Workflow Spec\n\n"
            "This document describes the approval workflow used by the "
            "orchestrator: a checkpoint pauses the run and notifies the "
            "operator via Telegram, who can approve, deny, or challenge it.\n"
            "See docs/approvals.md for the full design."
        )
        assert detect_noop_permission_response(text) is None

    def test_does_not_false_positive_on_ordinary_prose_mentioning_approval(self) -> None:
        text = "Added a new `approval` field to the RunResult model and wrote tests for it."
        assert detect_noop_permission_response(text) is None

    def test_returns_a_distinct_reason_per_pattern(self) -> None:
        reason_a = detect_noop_permission_response("I need your approval to continue.")
        reason_b = detect_noop_permission_response("Should I proceed with this change?")
        assert reason_a is not None
        assert reason_b is not None


class TestPromptFileNotFoundMessage:
    """`prompt_file_not_found_message()` -- the shared error-message builder
    for a step's unresolved `prompt_file`, used by both `ClaudeRunner.
    _assemble_prompt` and `PromptCliRunner._load_prompt` so the two runners
    never drift into different wording. Real incident: the raw message used
    to be just ``f"Prompt file not found: {prompt_path}"`` -- e.g. ``Prompt
    file not found: /security_review.md`` -- which names neither the
    offending task/step nor any of the OTHER directories that were also
    searched (a service running with ``cwd=/`` makes the base_dir-tier
    guess look like the only place ever checked)."""

    def _payload(self, tmp_path: Path) -> RunnerPayload:
        return RunnerPayload(
            project_name="demo",
            project=ProjectConfig(path=tmp_path),
            task_name="pentest",
            step=TaskStep(name="security review", runner="claude"),
            metadata={},
        )

    def test_names_task_and_step(self, tmp_path: Path) -> None:
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        s.config_repo = None
        s.base_dir = tmp_path
        message = prompt_file_not_found_message(
            self._payload(tmp_path), s, "security_review.md", tmp_path / "security_review.md"
        )
        assert "pentest" in message
        assert "security review" in message
        assert "security_review.md" in message

    def test_lists_every_searched_directory(self, tmp_path: Path) -> None:
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        s.config_repo = None
        s.base_dir = tmp_path / "base"
        message = prompt_file_not_found_message(
            self._payload(tmp_path),
            s,
            "security_review.md",
            tmp_path / "base" / "security_review.md",
        )
        for search_dir in s.config_path_search_dirs():
            assert str(search_dir) in message, f"{search_dir} missing from: {message}"
