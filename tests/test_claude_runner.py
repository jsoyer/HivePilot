"""Claude runner prompt assembly — incl. the inter-agent hand-off context."""

from __future__ import annotations

from pathlib import Path

from hivepilot.config import settings
from hivepilot.models import EffortLevel, ProjectConfig, RunnerDefinition, TaskStep
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
