"""Claude runner prompt assembly — incl. the inter-agent hand-off context."""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.config import settings
from hivepilot.models import EffortLevel, ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerExecutionError, RunnerPayload
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


def test_assemble_prompt_missing_file_names_task_step_and_searched_dirs(tmp_path: Path) -> None:
    """Real incident: the raw error used to read just ``Prompt file not
    found: /security_review.md`` -- naming neither the offending task/step
    nor the OTHER directories that were also searched. The improved
    message must name all three."""
    payload = RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="pentest",
        step=TaskStep(name="security review", runner="claude", prompt_file="does_not_exist.md"),
        metadata={},
        secrets={},
    )
    runner = _runner()
    with pytest.raises(FileNotFoundError) as excinfo:
        runner._assemble_prompt(payload)
    message = str(excinfo.value)
    assert "pentest" in message
    assert "security review" in message
    assert "does_not_exist.md" in message
    for search_dir in runner.settings.config_path_search_dirs():
        assert str(search_dir) in message, f"{search_dir} missing from: {message}"


def test_build_prompt_tolerates_missing_project() -> None:
    """human_challenge()/agent-request re-invocations build a repo-less
    payload (``project=None`` — it's a Q&A/challenge exchange, not a coding
    task against a real repository). ``_build_prompt`` must not crash on
    ``payload.project.path`` (AttributeError: 'NoneType' object has no
    attribute 'path') and must simply omit the project-specific sections.
    """
    payload = RunnerPayload(
        project_name="p",
        project=None,
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata={"extra_prompt": "Please respond."},
        secrets={},
    )
    out = _runner()._build_prompt(payload, "INSTRUCTIONS", None)
    assert "INSTRUCTIONS" in out
    assert "Please respond." in out
    assert "Repository path" not in out


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


class TestToolsRestriction:
    """`--tools` (verified via `claude --help`: "Specify the list of available
    tools from the built-in set. Use \"\" to disable all tools, \"default\" to
    use all tools, or specify tool names.") lets a caller run a headless
    `claude` session with NO tools available at all — not merely
    permission-gated, structurally absent. Additive: unset by default, so
    existing invocations are byte-identical."""

    def _payload(self, tmp_path: Path, *, step_metadata: dict | None = None) -> RunnerPayload:
        pf = tmp_path / "p.md"
        pf.write_text("do it", encoding="utf-8")
        return RunnerPayload(
            project_name="p",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(
                name="s", runner="claude", prompt_file=str(pf), metadata=step_metadata or {}
            ),
            metadata={},
            secrets={},
        )

    def test_no_tools_flag_by_default(self, tmp_path: Path) -> None:
        """`--tools` itself stays byte-identical to before this feature
        existed when unset. The `--` end-of-options separator is now
        unconditional (see TestToolsPromptDelivery) — always present
        immediately before the prompt regardless of `--tools`."""
        runner = ClaudeRunner(
            RunnerDefinition(name="claude", kind="claude", command="claude"), settings
        )
        args, _ = runner._build_invocation(self._payload(tmp_path))
        assert "--tools" not in args
        assert args[-2] == "--"

    def test_tools_flag_from_definition_options_empty_string_disables_all(
        self, tmp_path: Path
    ) -> None:
        runner = ClaudeRunner(
            RunnerDefinition(name="claude", kind="claude", command="claude", options={"tools": ""}),
            settings,
        )
        args, _ = runner._build_invocation(self._payload(tmp_path))
        assert "--tools" in args
        assert args[args.index("--tools") + 1] == ""

    def test_tools_flag_from_definition_options_list_joined_with_comma(
        self, tmp_path: Path
    ) -> None:
        runner = ClaudeRunner(
            RunnerDefinition(
                name="claude",
                kind="claude",
                command="claude",
                options={"tools": ["Bash", "Edit"]},
            ),
            settings,
        )
        args, _ = runner._build_invocation(self._payload(tmp_path))
        assert args[args.index("--tools") + 1] == "Bash,Edit"

    def test_step_metadata_tools_overrides_definition_options(self, tmp_path: Path) -> None:
        runner = ClaudeRunner(
            RunnerDefinition(
                name="claude", kind="claude", command="claude", options={"tools": "default"}
            ),
            settings,
        )
        payload = self._payload(tmp_path, step_metadata={"tools": ""})
        args, _ = runner._build_invocation(payload)
        assert args[args.index("--tools") + 1] == ""


class TestToolsPromptDelivery:
    """Regression coverage for a production bug: `--tools <tools...>` is
    VARIADIC (per `claude --help`), so `... --tools "" "<prompt>"` makes
    claude's arg parser swallow the positional prompt as ANOTHER `--tools`
    value (tools=["", "<prompt>"]) — no prompt ever reaches `--print`, and
    the real `claude` binary exits 1 with "Input must be provided either
    through stdin or as a prompt argument when using --print". This test
    would FAIL under the old "prompt positional right after variadic
    --tools" argv shape (no `--` separator).

    The end-of-options separator (`--`) is now UNCONDITIONAL — emitted
    immediately before the prompt on EVERY invocation, not only when
    `--tools` is set — because any other variadic flag (e.g. `--add-dir`,
    used for skill scratch dirs) is equally capable of swallowing the
    prompt. See `TestSkillAddDirPromptDelivery` below for that exact case."""

    def _payload(self, tmp_path: Path) -> RunnerPayload:
        pf = tmp_path / "p.md"
        pf.write_text("do it", encoding="utf-8")
        return RunnerPayload(
            project_name="p",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(name="s", runner="claude", prompt_file=str(pf)),
            metadata={},
            secrets={},
        )

    def test_end_of_options_separator_precedes_prompt_when_tools_set(self, tmp_path: Path) -> None:
        runner = ClaudeRunner(
            RunnerDefinition(name="claude", kind="claude", command="claude", options={"tools": ""}),
            settings,
        )
        args, _ = runner._build_invocation(self._payload(tmp_path))
        # The prompt must be the LAST argv element, immediately preceded by
        # a bare `--`, so claude's parser can never re-attach it to the
        # preceding variadic `--tools` flag.
        assert args[-2] == "--"
        assert args[-1] not in ("", "--tools")
        assert args.index("--") > args.index("--tools")

    def test_end_of_options_separator_after_permission_mode_too(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`--` must be the LAST flag emitted — after every other flag,
        including ones added after `--tools` in `_build_invocation` (e.g.
        `--permission-mode`) — not merely right after `--tools`."""
        runner = ClaudeRunner(
            RunnerDefinition(name="claude", kind="claude", command="claude", options={"tools": ""}),
            settings,
        )
        monkeypatch.setattr(runner.settings, "claude_permission_mode", "acceptEdits", raising=False)
        args, _ = runner._build_invocation(self._payload(tmp_path))
        assert args[-2] == "--"
        assert args.index("--") > args.index("--permission-mode")

    def test_separator_present_when_tools_unset_prompt_still_last(self, tmp_path: Path) -> None:
        """No-tools callers still get the unconditional `--` separator
        immediately before the prompt (the prompt itself remains the last
        element, so nothing downstream that only looked at `args[-1]` for
        the prompt is broken by this change)."""
        runner = ClaudeRunner(
            RunnerDefinition(name="claude", kind="claude", command="claude"), settings
        )
        args, _ = runner._build_invocation(self._payload(tmp_path))
        assert args.count("--") == 1
        assert args[-2] == "--"
        assert args[-1] != "--"

    def test_separator_appears_exactly_once_regardless_of_flags(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`--` must appear EXACTLY once, positioned immediately before the
        prompt, no matter how many flags precede it (`--tools` and
        `--permission-mode` here)."""
        runner = ClaudeRunner(
            RunnerDefinition(name="claude", kind="claude", command="claude", options={"tools": ""}),
            settings,
        )
        monkeypatch.setattr(runner.settings, "claude_permission_mode", "acceptEdits", raising=False)
        args, _ = runner._build_invocation(self._payload(tmp_path))
        assert args.count("--") == 1
        assert args[-2] == "--"


class TestSkillAddDirPromptDelivery:
    """Regression coverage for the SAME class of bug as
    `TestToolsPromptDelivery`, but for `--add-dir` (used to grant Claude
    access to a skill's ephemeral scratch directory — see
    `ClaudeRunner.apply_skill`). `--add-dir <dirs...>` is ALSO variadic, so
    without an unconditional `--` separator it would swallow the positional
    prompt exactly like `--tools` did — breaking EVERY task with a skill
    applied (confirmed against the real `claude` binary:
    `claude --print --add-dir /tmp "hi"` fails, `claude --print --add-dir
    /tmp -- "hi"` works)."""

    def _payload_with_skill_scratch(self, tmp_path: Path) -> RunnerPayload:
        pf = tmp_path / "p.md"
        pf.write_text("do it", encoding="utf-8")
        scratch_dir = tmp_path / "scratch"
        scratch_dir.mkdir()
        return RunnerPayload(
            project_name="p",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(name="s", runner="claude", prompt_file=str(pf)),
            metadata={"skill_scratch_dir": str(scratch_dir)},
            secrets={},
        )

    def test_add_dir_flag_does_not_swallow_prompt(self, tmp_path: Path) -> None:
        """A skill-applied payload (skill_scratch_dir set → `--add-dir` in
        argv) must still deliver the prompt: `--` immediately precedes it,
        and the prompt survives as the final argv element."""
        runner = ClaudeRunner(
            RunnerDefinition(name="claude", kind="claude", command="claude"), settings
        )
        payload = self._payload_with_skill_scratch(tmp_path)
        args, _ = runner._build_invocation(payload)

        assert "--add-dir" in args
        assert args.count("--") == 1
        assert args[-2] == "--"
        assert args.index("--") > args.index("--add-dir")
        # The prompt is intact (not merged into --add-dir's variadic list).
        assert args[-1] not in ("", "--add-dir")
        assert "do it" in args[-1]

    def test_plain_payload_no_tools_no_skill_still_ends_with_separator_then_prompt(
        self, tmp_path: Path
    ) -> None:
        """No tools, no skill: argv still ends with `-- <prompt>` and the
        prompt is intact (unconditional separator applies universally)."""
        pf = tmp_path / "p.md"
        pf.write_text("plain task", encoding="utf-8")
        payload = RunnerPayload(
            project_name="p",
            project=ProjectConfig(path=tmp_path),
            task_name="t",
            step=TaskStep(name="s", runner="claude", prompt_file=str(pf)),
            metadata={},
            secrets={},
        )
        runner = ClaudeRunner(
            RunnerDefinition(name="claude", kind="claude", command="claude"), settings
        )
        args, _ = runner._build_invocation(payload)

        assert "--add-dir" not in args
        assert args.count("--") == 1
        assert args[-2] == "--"
        assert "plain task" in args[-1]


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


# ── Explicit failure logs (Part A.1) ────────────────────────────────────────


class TestExplicitFailureLogs:
    """A runner failure must be diagnosable from the log alone: exit code,
    runner kind, resolved model, role, task/stage, project, permission_mode,
    whether a skill was applied, a bounded stderr excerpt, and -- for a KNOWN
    failure signature -- an actionable `hint`."""

    def _failing_payload(self, tmp_path: Path, **step_kwargs) -> RunnerPayload:
        pf = tmp_path / "p.md"
        pf.write_text("do it", encoding="utf-8")
        return RunnerPayload(
            project_name="proj-x",
            project=ProjectConfig(path=tmp_path),
            task_name="task-x",
            step=TaskStep(name="stage-x", runner="claude", prompt_file=str(pf), **step_kwargs),
            metadata={"role": "developer"},
            secrets={},
        )

    def test_capture_failure_raises_with_structured_context(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        payload = self._failing_payload(tmp_path, metadata={"permission_mode": "acceptEdits"})
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=1, stdout="", stderr="some CLI error")
            with pytest.raises(RunnerExecutionError) as excinfo:
                _runner().capture(payload)

        ctx = excinfo.value.context
        assert ctx["exit_code"] == 1
        assert ctx["runner_kind"] == "claude"
        assert ctx["role"] == "developer"
        assert ctx["task"] == "task-x"
        assert ctx["stage"] == "stage-x"
        assert ctx["project"] == "proj-x"
        assert ctx["permission_mode"] == "acceptEdits"
        assert ctx["skill_applied"] is False
        assert "some CLI error" in ctx["stderr_excerpt"]

    def test_stderr_excerpt_bounded_to_300_chars(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        payload = self._failing_payload(tmp_path)
        long_stderr = "x" * 5000
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=1, stdout="", stderr=long_stderr)
            with pytest.raises(RunnerExecutionError) as excinfo:
                _runner().capture(payload)
        assert len(excinfo.value.context["stderr_excerpt"]) == 300

    def test_hint_for_stdin_or_prompt_argument(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        payload = self._failing_payload(tmp_path)
        stderr = (
            "Error: Input must be provided either through stdin or as a "
            "prompt argument when using --print"
        )
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=1, stdout="", stderr=stderr)
            with pytest.raises(RunnerExecutionError) as excinfo:
                _runner().capture(payload)
        assert "hint" in excinfo.value.context
        assert "--" in excinfo.value.context["hint"]
        assert "hint:" in str(excinfo.value)

    def test_hint_for_root_sudo_permission_refusal(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        payload = self._failing_payload(tmp_path)
        stderr = "--dangerously-skip-permissions cannot be used with root/sudo"
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=1, stdout="", stderr=stderr)
            with pytest.raises(RunnerExecutionError) as excinfo:
                _runner().capture(payload)
        hint = excinfo.value.context["hint"]
        # Must point at the two real remedies (non-root user, or IS_SANDBOX=1
        # on a dedicated sandbox) and must NOT recommend acceptEdits as a
        # substitute -- acceptEdits only auto-accepts file edits, the agent
        # still needs (unavailable, headless) approval to run Bash, so the
        # step silently produces nothing.
        assert "non-root" in hint
        assert "IS_SANDBOX" in hint
        assert "acceptEdits is NOT a substitute" in hint

    def test_hint_for_headless_permission_refusal(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        payload = self._failing_payload(tmp_path)
        stderr = "Tool use needs a permission grant to proceed"
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=1, stdout="", stderr=stderr)
            with pytest.raises(RunnerExecutionError) as excinfo:
                _runner().capture(payload)
        assert "interactive approval" in excinfo.value.context["hint"]

    def test_no_hint_for_unrecognized_stderr(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        payload = self._failing_payload(tmp_path)
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=1, stdout="", stderr="totally unrelated error")
            with pytest.raises(RunnerExecutionError) as excinfo:
                _runner().capture(payload)
        assert "hint" not in excinfo.value.context

    def test_run_path_failure_raises_with_context_no_stderr(self, tmp_path: Path) -> None:
        """`run()` never captures stdout/stderr (streams to inherited fds) --
        it must still surface every OTHER structured field."""
        import subprocess as sp
        from unittest.mock import patch

        payload = self._failing_payload(tmp_path)
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.side_effect = sp.CalledProcessError(returncode=2, cmd=["claude"])
            with pytest.raises(RunnerExecutionError) as excinfo:
                _runner().run(payload)
        ctx = excinfo.value.context
        assert ctx["exit_code"] == 2
        assert ctx["runner_kind"] == "claude"
        assert ctx["task"] == "task-x"

    def test_sigkill_run_path_gets_oom_hint_despite_empty_stderr(self, tmp_path: Path) -> None:
        """Bug 1 (run 243, live incident): `claude` was SIGKILLed (exit_code
        -9), `stderr_excerpt` is empty (the process never got to write
        anything) -- the pipeline then recorded the run as `test_failure`.
        The `run()` path never captures stderr at all, so the hint MUST come
        from the exit code alone, not from any stderr signature match."""
        import subprocess as sp
        from unittest.mock import patch

        payload = self._failing_payload(tmp_path)
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.side_effect = sp.CalledProcessError(returncode=-9, cmd=["claude"])
            with pytest.raises(RunnerExecutionError) as excinfo:
                _runner().run(payload)
        ctx = excinfo.value.context
        assert ctx["exit_code"] == -9
        assert "hint" in ctx
        hint = ctx["hint"].lower()
        assert "sigkill" in hint
        assert "oom" in hint or "out-of-memory" in hint or "out of memory" in hint
        assert "available" in hint

    def test_sigkill_capture_path_gets_oom_hint(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        payload = self._failing_payload(tmp_path)
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=-9, stdout="", stderr="")
            with pytest.raises(RunnerExecutionError) as excinfo:
                _runner().capture(payload)
        ctx = excinfo.value.context
        assert ctx["exit_code"] == -9
        assert "hint" in ctx
        assert "sigkill" in ctx["hint"].lower()
        assert "hint:" in str(excinfo.value)

    def test_positive_exit_code_unaffected_by_signal_classification(self, tmp_path: Path) -> None:
        """Regression guard: a positive, non-zero exit code (a genuine
        command failure, e.g. a real test failure) must NEVER get a
        signal-death hint -- only a negative exit code (a signal death) does.
        """
        from unittest.mock import MagicMock, patch

        payload = self._failing_payload(tmp_path)
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=1, stdout="", stderr="totally unrelated error")
            with pytest.raises(RunnerExecutionError) as excinfo:
                _runner().capture(payload)
        assert "hint" not in excinfo.value.context

    def test_registered_secret_redacted_from_stderr_excerpt(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from hivepilot.services import config_provenance

        config_provenance.clear_secret_values()
        marker = "SUPERSECRET-MARKER-abc123-do-not-leak"
        config_provenance.register_secret_value(marker)
        try:
            payload = self._failing_payload(tmp_path)
            with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
                m.return_value = MagicMock(
                    returncode=1, stdout="", stderr=f"auth failed with key {marker}"
                )
                with pytest.raises(RunnerExecutionError) as excinfo:
                    _runner().capture(payload)
            assert marker not in excinfo.value.context["stderr_excerpt"]
            assert marker not in str(excinfo.value)
        finally:
            config_provenance.clear_secret_values()

    def test_usage_capture_path_also_raises_with_context(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock, patch

        payload = self._failing_payload(tmp_path)
        runner = _runner()
        monkeypatch.setattr(runner.settings, "claude_capture_usage", True, raising=False)
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=1, stdout="", stderr="usage-path failure")
            with pytest.raises(RunnerExecutionError) as excinfo:
                runner.capture(payload)
        assert excinfo.value.context["exit_code"] == 1
        assert "usage-path failure" in excinfo.value.context["stderr_excerpt"]


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


def test_build_prompt_substitutes_target_repo(tmp_path: Path) -> None:
    """Ensure {TARGET_REPO} in instructions is replaced with the real project path."""
    payload = RunnerPayload(
        project_name="test-proj",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata={},
        secrets={},
    )
    out = _runner()._build_prompt(payload, "Read {TARGET_REPO}/CLAUDE.md", None)
    assert "{TARGET_REPO}" not in out
    assert str(tmp_path) in out


def test_build_prompt_substitutes_governance_repo(tmp_path: Path, monkeypatch) -> None:
    """Ensure {GOVERNANCE_REPO} is replaced with settings.governance_repo."""
    import hivepilot.runners.claude_runner as cr_mod

    monkeypatch.setattr(cr_mod.settings, "governance_repo", "/some/governance/repo", raising=False)

    payload = RunnerPayload(
        project_name="test-proj",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata={},
        secrets={},
    )
    out = _runner()._build_prompt(payload, "See {GOVERNANCE_REPO}/AGENT-GOVERNANCE.md", None)
    assert "{GOVERNANCE_REPO}" not in out
    assert "/some/governance/repo" in out


# ---------------------------------------------------------------------------
# Phase 24b.2a — opt-in usage capture (tokens/cost/actual-model)
# ---------------------------------------------------------------------------


def _usage_payload(tmp_path: Path) -> RunnerPayload:
    pf = tmp_path / "p.md"
    pf.write_text("do it", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude", prompt_file=str(pf)),
        metadata={},
        secrets={},
    )


class TestUsageCaptureFlagOff:
    """Default (flag off) must be BYTE-IDENTICAL to pre-24b.2a behaviour."""

    def test_no_output_format_json_flag_in_argv(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock, patch

        payload = _usage_payload(tmp_path)
        runner = _runner()
        monkeypatch.setattr(runner.settings, "claude_capture_usage", False, raising=False)
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout="AGENT SAID THIS", returncode=0)
            out = runner.capture(payload)
        assert out == "AGENT SAID THIS"
        assert m.call_count == 1
        argv = m.call_args.args[0]
        assert "--output-format" not in argv

    def test_usage_is_none_when_flag_off(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock, patch

        from hivepilot.runners.base import pop_last_usage

        payload = _usage_payload(tmp_path)
        runner = _runner()
        monkeypatch.setattr(runner.settings, "claude_capture_usage", False, raising=False)
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout="AGENT SAID THIS", returncode=0)
            runner.capture(payload)
        assert pop_last_usage() is None


class TestUsageCaptureFlagOnWellFormed:
    def test_returns_result_field_and_captures_usage(self, tmp_path: Path, monkeypatch) -> None:
        import json
        from unittest.mock import MagicMock, patch

        from hivepilot.runners.base import pop_last_usage

        payload = _usage_payload(tmp_path)
        runner = _runner()
        monkeypatch.setattr(runner.settings, "claude_capture_usage", True, raising=False)
        envelope = json.dumps(
            {
                "type": "result",
                "result": "AGENT SAID THIS",
                "usage": {"input_tokens": 123, "output_tokens": 45},
                "total_cost_usd": 0.0067,
                "model": "claude-sonnet-4-6",
            }
        )
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout=envelope, returncode=0)
            out = runner.capture(payload)

        assert out == "AGENT SAID THIS"
        argv = m.call_args.args[0]
        assert "--output-format" in argv
        assert argv[argv.index("--output-format") + 1] == "json"

        usage = pop_last_usage()
        assert usage is not None
        assert usage.input_tokens == 123
        assert usage.output_tokens == 45
        assert usage.cost_usd == 0.0067
        assert usage.model == "claude-sonnet-4-6"

    def test_only_one_subprocess_call_on_well_formed_json(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import json
        from unittest.mock import MagicMock, patch

        payload = _usage_payload(tmp_path)
        runner = _runner()
        monkeypatch.setattr(runner.settings, "claude_capture_usage", True, raising=False)
        envelope = json.dumps({"result": "TEXT", "usage": {}, "model": "m"})
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout=envelope, returncode=0)
            runner.capture(payload)
        assert m.call_count == 1


class TestUsageCaptureModelUsagePrimary:
    """usage-capture-modelusage fix: a real Claude CLI envelope reports
    empty/zero top-level `usage` (the fields are near-worthless once prompt
    caching is in play) and puts the REAL numbers in `modelUsage`, keyed by
    canonical model id. This must be read as the PRIMARY source."""

    def test_modelusage_only_envelope_yields_correct_tokens_and_canonical_model(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import json
        from unittest.mock import MagicMock, patch

        from hivepilot.runners.base import pop_last_usage

        payload = _usage_payload(tmp_path)
        runner = _runner()
        monkeypatch.setattr(runner.settings, "claude_capture_usage", True, raising=False)
        envelope = json.dumps(
            {
                "result": "AGENT SAID THIS",
                "usage": {
                    "input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 0,
                },
                "modelUsage": {
                    "claude-haiku-4-5-20251001": {
                        "inputTokens": 1304,
                        "outputTokens": 20,
                        "cacheReadInputTokens": 0,
                        "cacheCreationInputTokens": 0,
                        "costUSD": 0.001404,
                        "canonicalModel": "claude-haiku-4-5",
                        "provider": "firstParty",
                    }
                },
            }
        )
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout=envelope, returncode=0)
            out = runner.capture(payload)

        assert out == "AGENT SAID THIS"
        usage = pop_last_usage()
        assert usage is not None
        assert usage.input_tokens == 1304
        assert usage.output_tokens == 20
        assert usage.model == "claude-haiku-4-5", (
            "must use the canonicalModel field (price-map-shaped id), not the dated dict key"
        )
        assert usage.cost_usd == 0.001404
        assert usage.cache_read_tokens == 0
        assert usage.cache_creation_tokens == 0

    def test_cache_read_and_creation_tokens_captured_distinctly(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import json
        from unittest.mock import MagicMock, patch

        from hivepilot.runners.base import pop_last_usage

        payload = _usage_payload(tmp_path)
        runner = _runner()
        monkeypatch.setattr(runner.settings, "claude_capture_usage", True, raising=False)
        envelope = json.dumps(
            {
                "result": "TEXT",
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "modelUsage": {
                    "claude-sonnet-4-5-20250929": {
                        "inputTokens": 10,
                        "outputTokens": 50,
                        "cacheReadInputTokens": 40000,
                        "cacheCreationInputTokens": 2000,
                        "costUSD": 0.05,
                        "canonicalModel": "claude-sonnet-4-5",
                    }
                },
            }
        )
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout=envelope, returncode=0)
            runner.capture(payload)

        usage = pop_last_usage()
        assert usage is not None
        assert usage.cache_read_tokens == 40000
        assert usage.cache_creation_tokens == 2000
        # Cache volume must never be folded into input_tokens.
        assert usage.input_tokens == 10

    def test_legacy_usage_only_envelope_still_works(self, tmp_path: Path, monkeypatch) -> None:
        """Regression: an envelope with NO `modelUsage` key at all (older CLI
        shape) must still be parsed via the top-level `usage` block exactly
        as before this fix."""
        import json
        from unittest.mock import MagicMock, patch

        from hivepilot.runners.base import pop_last_usage

        payload = _usage_payload(tmp_path)
        runner = _runner()
        monkeypatch.setattr(runner.settings, "claude_capture_usage", True, raising=False)
        envelope = json.dumps(
            {
                "result": "AGENT SAID THIS",
                "usage": {"input_tokens": 123, "output_tokens": 45},
                "total_cost_usd": 0.0067,
                "model": "claude-sonnet-4-6",
            }
        )
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout=envelope, returncode=0)
            out = runner.capture(payload)

        assert out == "AGENT SAID THIS"
        usage = pop_last_usage()
        assert usage is not None
        assert usage.input_tokens == 123
        assert usage.output_tokens == 45
        assert usage.cost_usd == 0.0067
        assert usage.model == "claude-sonnet-4-6"
        assert usage.cache_read_tokens is None
        assert usage.cache_creation_tokens is None

    def test_multi_model_envelope_attributed_per_documented_rule(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Attribution rule (documented on `_extract_model_usage`): tokens
        and cost are SUMMED across every model in `modelUsage` (that's the
        real total cost of the step); the step's recorded `model` is the
        DOMINANT one (most input+output+cache tokens) -- here, sonnet."""
        import json
        from unittest.mock import MagicMock, patch

        from hivepilot.runners.base import pop_last_usage

        payload = _usage_payload(tmp_path)
        runner = _runner()
        monkeypatch.setattr(runner.settings, "claude_capture_usage", True, raising=False)
        envelope = json.dumps(
            {
                "result": "TEXT",
                "usage": {},
                "modelUsage": {
                    "claude-haiku-4-5-20251001": {
                        "inputTokens": 100,
                        "outputTokens": 20,
                        "costUSD": 0.001,
                        "canonicalModel": "claude-haiku-4-5",
                    },
                    "claude-sonnet-4-5-20250929": {
                        "inputTokens": 5000,
                        "outputTokens": 3000,
                        "costUSD": 0.05,
                        "canonicalModel": "claude-sonnet-4-5",
                    },
                },
            }
        )
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout=envelope, returncode=0)
            runner.capture(payload)

        usage = pop_last_usage()
        assert usage is not None
        assert usage.input_tokens == 5100
        assert usage.output_tokens == 3020
        assert usage.cost_usd == pytest.approx(0.051)
        assert usage.model == "claude-sonnet-4-5"

    def test_model_missing_from_price_map_never_silently_zero_costed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A `modelUsage` entry with no self-reported `costUSD` AND a model
        id absent from the price map must leave the step's cost UNPRICED
        (None) -- never silently attributed a $0.0 cost."""
        import json
        from unittest.mock import MagicMock, patch

        from hivepilot.runners.base import pop_last_usage

        payload = _usage_payload(tmp_path)
        runner = _runner()
        monkeypatch.setattr(runner.settings, "claude_capture_usage", True, raising=False)
        envelope = json.dumps(
            {
                "result": "TEXT",
                "usage": {},
                "modelUsage": {
                    "some-brand-new-unlisted-model": {
                        "inputTokens": 100,
                        "outputTokens": 20,
                        "canonicalModel": "some-brand-new-unlisted-model",
                    }
                },
            }
        )
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout=envelope, returncode=0)
            runner.capture(payload)

        usage = pop_last_usage()
        assert usage is not None
        assert usage.cost_usd is None
        assert usage.input_tokens == 100
        assert usage.model == "some-brand-new-unlisted-model"


class TestUsageCaptureGracefulDegradation:
    def test_malformed_json_falls_back_to_raw_text_and_null_usage(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from unittest.mock import MagicMock, patch

        from hivepilot.runners.base import pop_last_usage

        payload = _usage_payload(tmp_path)
        runner = _runner()
        monkeypatch.setattr(runner.settings, "claude_capture_usage", True, raising=False)
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout="NOT VALID JSON {{{", returncode=0)
            out = runner.capture(payload)
        assert out == "NOT VALID JSON {{{"
        assert pop_last_usage() is None

    def test_json_missing_result_field_falls_back(self, tmp_path: Path, monkeypatch) -> None:
        import json
        from unittest.mock import MagicMock, patch

        from hivepilot.runners.base import pop_last_usage

        payload = _usage_payload(tmp_path)
        runner = _runner()
        monkeypatch.setattr(runner.settings, "claude_capture_usage", True, raising=False)
        envelope = json.dumps({"usage": {"input_tokens": 1}})
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout=envelope, returncode=0)
            out = runner.capture(payload)
        assert out == envelope
        assert pop_last_usage() is None

    def test_cli_error_on_the_flag_raises_and_never_retries(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A non-zero exit with --output-format json present must RAISE —
        exactly like the flag-off path already does — and must NEVER retry
        the same prompt without the flag. A claude subprocess can exit
        non-zero AFTER doing real work (mid-run crash, OOM/SIGKILL, network
        drop post-push, rate-limit after partial work); for the developer
        role (bypassPermissions) that means files may already be
        edited/committed/pushed. Retrying would duplicate that work, so this
        flag must be "no worse than flag off" (which never retries either) —
        never silently double-run the agent."""
        from unittest.mock import MagicMock, patch

        from hivepilot.runners.base import pop_last_usage

        payload = _usage_payload(tmp_path)
        runner = _runner()
        monkeypatch.setattr(runner.settings, "claude_capture_usage", True, raising=False)

        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=2, stdout="", stderr="error: unknown option")
            with __import__("pytest").raises(RuntimeError, match="error: unknown option"):
                runner.capture(payload)

        assert m.call_count == 1, "must not retry without the flag on a non-zero exit"
        argv = m.call_args.args[0]
        assert "--output-format" in argv
        assert pop_last_usage() is None

    def test_no_secret_or_output_content_in_warning_logs(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock, patch

        payload = _usage_payload(tmp_path)
        runner = _runner()
        monkeypatch.setattr(runner.settings, "claude_capture_usage", True, raising=False)

        with (
            patch("hivepilot.runners.claude_runner.subprocess.run") as m,
            patch("hivepilot.runners.claude_runner.logger") as mock_logger,
        ):
            m.return_value = MagicMock(
                stdout="super-secret-token-abc123 NOT VALID JSON", returncode=0
            )
            runner.capture(payload)

        for call in mock_logger.warning.call_args_list:
            rendered = " ".join(str(a) for a in call.args) + " ".join(
                f"{k}={v}" for k, v in call.kwargs.items()
            )
            assert "super-secret-token-abc123" not in rendered


def test_build_prompt_governance_repo_empty_when_not_configured(
    tmp_path: Path, monkeypatch
) -> None:
    """When governance_repo is None, {GOVERNANCE_REPO} expands to empty string."""
    import hivepilot.runners.claude_runner as cr_mod

    monkeypatch.setattr(cr_mod.settings, "governance_repo", None, raising=False)

    payload = RunnerPayload(
        project_name="test-proj",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata={},
        secrets={},
    )
    out = _runner()._build_prompt(payload, "See {GOVERNANCE_REPO}/AGENT-GOVERNANCE.md", None)
    assert "{GOVERNANCE_REPO}" not in out
    assert "/AGENT-GOVERNANCE.md" in out


# ---------------------------------------------------------------------------
# inline-repo-instructions PRD — `_build_prompt` inlines the CONTENT of a
# project's declared repository instructions file(s) instead of only naming
# them (see hivepilot.services.repo_instructions). Root-cause bug: the old
# line was `f"Repository instructions file: {payload.project.claude_md}"` —
# a bare filename, never read, and silent when the file didn't exist.
# ---------------------------------------------------------------------------


def test_build_prompt_inlines_claude_md_content(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("Never force-push to main.", encoding="utf-8")
    payload = RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path, claude_md="CLAUDE.md"),
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata={},
        secrets={},
    )
    out = _runner()._build_prompt(payload, "INSTRUCTIONS", None)
    assert "Never force-push to main." in out
    assert "Repository instructions file: CLAUDE.md" not in out


def test_build_prompt_inlines_multiple_instruction_files_in_order(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("CLAUDE-CONTENT", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("AGENTS-CONTENT", encoding="utf-8")
    payload = RunnerPayload(
        project_name="p",
        project=ProjectConfig(
            path=tmp_path, claude_md="CLAUDE.md", instruction_files=["AGENTS.md"]
        ),
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata={},
        secrets={},
    )
    out = _runner()._build_prompt(payload, "INSTRUCTIONS", None)
    assert out.index("CLAUDE-CONTENT") < out.index("AGENTS-CONTENT")


def test_build_prompt_warns_visibly_on_missing_instructions_file(tmp_path: Path, caplog) -> None:
    import logging

    payload = RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path, claude_md="DOES-NOT-EXIST.md"),
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata={},
        secrets={},
    )
    with caplog.at_level(logging.WARNING):
        out = _runner()._build_prompt(payload, "INSTRUCTIONS", None)
    assert "DOES-NOT-EXIST.md" in out
    assert "NOT FOUND" in out.upper()
    assert "repo_instructions.missing_file" in caplog.text


def test_build_prompt_no_instructions_section_when_none_declared(tmp_path: Path) -> None:
    payload = RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude"),
        metadata={},
        secrets={},
    )
    out = _runner()._build_prompt(payload, "INSTRUCTIONS", None)
    assert "Repository instructions" not in out


# ---------------------------------------------------------------------------
# Reasoning-effort knob (MAX_THINKING_TOKENS) — ClaudeRunner._resolve_effort /
# _effort_env_overlay, and both env-injection points (_build_invocation's own
# env AND the bwrap-sandbox env_overlay in run()/capture()).
# ---------------------------------------------------------------------------


def _effort_payload(tmp_path: Path, step_effort: EffortLevel | None = None) -> RunnerPayload:
    pf = tmp_path / "p.md"
    pf.write_text("do it", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude", prompt_file=str(pf), effort=step_effort),
        metadata={},
        secrets={},
    )


def _effort_runner(definition_effort: EffortLevel | None = None) -> ClaudeRunner:
    return ClaudeRunner(
        RunnerDefinition(name="claude", kind="claude", command="claude", effort=definition_effort),
        settings,
    )


class TestReasoningEffortRunPath:
    """`run()` path: MAX_THINKING_TOKENS threading through `_build_invocation`'s
    env (both plain and — separately — the bwrap-sandbox env_overlay)."""

    def test_role_effort_high_sets_max_thinking_tokens(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        payload = _effort_payload(tmp_path)
        runner = _effort_runner(definition_effort="high")
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0)
            runner.run(payload)
        env = m.call_args.kwargs["env"]
        assert env["MAX_THINKING_TOKENS"] == "24000"

    def test_role_effort_max_sets_max_thinking_tokens(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        payload = _effort_payload(tmp_path)
        runner = _effort_runner(definition_effort="max")
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0)
            runner.run(payload)
        env = m.call_args.kwargs["env"]
        assert env["MAX_THINKING_TOKENS"] == "63999"

    def test_no_effort_anywhere_leaves_max_thinking_tokens_absent(self, tmp_path: Path) -> None:
        """THE critical regression guard: no effort declared on the role
        (RunnerDefinition.effort=None) nor the step (TaskStep.effort=None)
        must leave MAX_THINKING_TOKENS entirely absent from the subprocess
        env -- byte-identical to every pre-effort config."""
        from unittest.mock import MagicMock, patch

        payload = _effort_payload(tmp_path)
        runner = _effort_runner(definition_effort=None)
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0)
            runner.run(payload)
        env = m.call_args.kwargs["env"]
        assert "MAX_THINKING_TOKENS" not in env

    def test_definition_effort_is_authoritative_over_step(self, tmp_path: Path) -> None:
        """Unified precedence: `RunnerDefinition.effort` (the orchestrator's
        authoritative `policy > stage > role` result) WINS over a per-step
        `TaskStep.effort` — a step must never silently override a stage- or
        policy-mandated effort. (This deliberately reconciles the two
        independently-shipped effort systems: the earlier per-role/step knob let
        the step win; the unified `resolve_runner_effort` makes the definition
        authoritative and treats the step as a fallback only.)"""
        from unittest.mock import MagicMock, patch

        payload = _effort_payload(tmp_path, step_effort="max")
        runner = _effort_runner(definition_effort="low")
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0)
            runner.run(payload)
        env = m.call_args.kwargs["env"]
        assert env["MAX_THINKING_TOKENS"] == "4000"

    def test_step_effort_applies_as_fallback_when_definition_none(self, tmp_path: Path) -> None:
        """A per-step `TaskStep.effort` still drives Claude when nothing was
        resolved upstream (`RunnerDefinition.effort is None`) — the step's
        primary use is preserved."""
        from unittest.mock import MagicMock, patch

        payload = _effort_payload(tmp_path, step_effort="max")
        runner = _effort_runner(definition_effort=None)
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0)
            runner.run(payload)
        env = m.call_args.kwargs["env"]
        assert env["MAX_THINKING_TOKENS"] == "63999"

    def test_effort_xhigh_maps_to_40000(self, tmp_path: Path) -> None:
        """The unified superset level `xhigh` maps to the token budget between
        `high` (24000) and `max` (63999)."""
        from unittest.mock import MagicMock, patch

        payload = _effort_payload(tmp_path)
        runner = _effort_runner(definition_effort="xhigh")
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0)
            runner.run(payload)
        env = m.call_args.kwargs["env"]
        assert env["MAX_THINKING_TOKENS"] == "40000"


class TestReasoningEffortCapturePath:
    """`capture()` path: same MAX_THINKING_TOKENS threading, independent
    subprocess-invocation code path from `run()`."""

    def test_role_effort_high_sets_max_thinking_tokens(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        payload = _effort_payload(tmp_path)
        runner = _effort_runner(definition_effort="high")
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout="OUT", returncode=0)
            runner.capture(payload)
        env = m.call_args.kwargs["env"]
        assert env["MAX_THINKING_TOKENS"] == "24000"

    def test_role_effort_max_sets_max_thinking_tokens(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        payload = _effort_payload(tmp_path)
        runner = _effort_runner(definition_effort="max")
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout="OUT", returncode=0)
            runner.capture(payload)
        env = m.call_args.kwargs["env"]
        assert env["MAX_THINKING_TOKENS"] == "63999"

    def test_no_effort_anywhere_leaves_max_thinking_tokens_absent(self, tmp_path: Path) -> None:
        """Same critical regression guard as the run() path, for capture()."""
        from unittest.mock import MagicMock, patch

        payload = _effort_payload(tmp_path)
        runner = _effort_runner(definition_effort=None)
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout="OUT", returncode=0)
            runner.capture(payload)
        env = m.call_args.kwargs["env"]
        assert "MAX_THINKING_TOKENS" not in env

    def test_definition_effort_is_authoritative_over_step(self, tmp_path: Path) -> None:
        """capture() path: same unified precedence as run() — the
        orchestrator-resolved `RunnerDefinition.effort` wins over the step."""
        from unittest.mock import MagicMock, patch

        payload = _effort_payload(tmp_path, step_effort="max")
        runner = _effort_runner(definition_effort="low")
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout="OUT", returncode=0)
            runner.capture(payload)
        env = m.call_args.kwargs["env"]
        assert env["MAX_THINKING_TOKENS"] == "4000"

    def test_step_effort_applies_as_fallback_when_definition_none(self, tmp_path: Path) -> None:
        """capture() path: step effort still applies when nothing was resolved
        upstream (`RunnerDefinition.effort is None`)."""
        from unittest.mock import MagicMock, patch

        payload = _effort_payload(tmp_path, step_effort="max")
        runner = _effort_runner(definition_effort=None)
        with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
            m.return_value = MagicMock(stdout="OUT", returncode=0)
            runner.capture(payload)
        env = m.call_args.kwargs["env"]
        assert env["MAX_THINKING_TOKENS"] == "63999"


class TestReasoningEffortSandboxOverlay:
    """Regression guard for injection point #2: effort must ALSO survive
    into the bwrap-sandboxed `env_overlay` (`intentional_env`) path, not
    just `_build_invocation`'s own env — these are two separate env dicts
    in this file (see `_apply_sandbox`). Without this, effort would
    silently vanish whenever `dev_sandbox == "bwrap"` AND permission_mode
    is elevated (bypassPermissions/acceptEdits) — the developer role's
    typical config."""

    def test_effort_survives_bwrap_sandboxed_env(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock, patch

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
        runner = _effort_runner(definition_effort="high")
        monkeypatch.setattr(runner.settings, "dev_sandbox", "bwrap", raising=False)
        with (
            patch(
                "hivepilot.runners.claude_runner.wrap_bwrap", side_effect=lambda argv, workdir: argv
            ),
            patch("hivepilot.runners.claude_runner.subprocess.run") as m,
        ):
            m.return_value = MagicMock(returncode=0)
            runner.run(payload)
        env = m.call_args.kwargs["env"]
        assert env["MAX_THINKING_TOKENS"] == "24000"
