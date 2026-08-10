"""Tests for hivepilot.utils.sandbox — env scrubbing and bwrap wrapping."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# scrub_env tests
# ---------------------------------------------------------------------------


def test_scrub_env_keeps_allowlisted() -> None:
    """scrub_env keeps exact names and glob-matched names from the allowlist."""
    from hivepilot.utils.sandbox import scrub_env

    env = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "ANTHROPIC_API_KEY": "sk-ant-abc",
        "CLAUDE_MODEL": "claude-sonnet",  # CLAUDE_* pattern
        "NODE_ENV": "production",  # NODE_* pattern
        "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",  # must be dropped
        "AWS_SECRET_ACCESS_KEY": "secret",  # must be dropped
    }
    result = scrub_env(env)
    assert result["PATH"] == "/usr/bin"
    assert result["HOME"] == "/home/user"
    assert result["ANTHROPIC_API_KEY"] == "sk-ant-abc"
    assert result["CLAUDE_MODEL"] == "claude-sonnet"
    assert result["NODE_ENV"] == "production"


def test_scrub_env_drops_secrets() -> None:
    """scrub_env must drop SSH agent socket, AWS credentials, and random secrets."""
    from hivepilot.utils.sandbox import scrub_env

    env = {
        "PATH": "/usr/bin",
        "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
        "AWS_SECRET_ACCESS_KEY": "AKIASECRET",
        "AWS_ACCESS_KEY_ID": "AKIAID",
        "MY_CUSTOM_SECRET": "hunter2",
    }
    result = scrub_env(env)
    assert "SSH_AUTH_SOCK" not in result
    assert "AWS_SECRET_ACCESS_KEY" not in result
    assert "AWS_ACCESS_KEY_ID" not in result
    assert "MY_CUSTOM_SECRET" not in result
    # PATH should still be kept
    assert "PATH" in result


def test_scrub_env_custom_allowlist() -> None:
    """scrub_env respects a custom allowlist."""
    from hivepilot.utils.sandbox import scrub_env

    env = {"PATH": "/usr/bin", "FOO": "bar", "BAZ": "qux"}
    result = scrub_env(env, allowlist=["FOO", "BAZ"])
    assert result == {"FOO": "bar", "BAZ": "qux"}
    assert "PATH" not in result


def test_scrub_env_empty_env() -> None:
    """scrub_env handles an empty environment gracefully."""
    from hivepilot.utils.sandbox import scrub_env

    assert scrub_env({}) == {}


def test_scrub_env_empty_allowlist_falls_back_to_default() -> None:
    """An empty allowlist [] must fall back to DEFAULT_ALLOWLIST, not strip all env.

    Regression test for the bug where ``if allowlist is None`` allowed an
    explicit ``[]`` to be treated as "allow nothing", stripping PATH,
    ANTHROPIC_API_KEY, and every other key from the subprocess environment.
    """
    from hivepilot.utils.sandbox import scrub_env

    env = {
        "PATH": "/x",
        "ANTHROPIC_API_KEY": "k",
        "SSH_AUTH_SOCK": "s",
    }
    result = scrub_env(env, allowlist=[])

    assert "PATH" in result, "PATH must be kept when allowlist=[] falls back to DEFAULT_ALLOWLIST"
    assert "ANTHROPIC_API_KEY" in result, (
        "ANTHROPIC_API_KEY must be kept when allowlist=[] falls back to DEFAULT_ALLOWLIST"
    )
    assert "SSH_AUTH_SOCK" not in result, (
        "SSH_AUTH_SOCK must still be dropped (not on DEFAULT_ALLOWLIST)"
    )


# ---------------------------------------------------------------------------
# wrap_bwrap tests
# ---------------------------------------------------------------------------


def test_wrap_bwrap_structure(tmp_path: Path) -> None:
    """wrap_bwrap returns argv starting with bwrap and containing required flags."""
    from hivepilot.utils.sandbox import wrap_bwrap

    workdir = str(tmp_path)
    original = ["claude", "--print", "hello"]

    with patch("hivepilot.utils.sandbox.shutil.which", return_value="/usr/bin/bwrap"):
        result = wrap_bwrap(original, workdir=workdir)

    assert result[0] == "bwrap"
    assert "--ro-bind" in result
    # / is bound read-only
    idx = result.index("--ro-bind")
    assert result[idx + 1] == "/"
    assert result[idx + 2] == "/"
    # workdir is bound read-write
    assert "--bind" in result
    bind_pairs = []
    i = 0
    while i < len(result):
        if result[i] == "--bind":
            bind_pairs.append((result[i + 1], result[i + 2]))
            i += 3
        else:
            i += 1
    assert (workdir, workdir) in bind_pairs
    # .ssh is masked with tmpfs
    tmpfs_targets = []
    i = 0
    while i < len(result):
        if result[i] == "--tmpfs":
            tmpfs_targets.append(result[i + 1])
            i += 2
        else:
            i += 1
    home = os.path.expanduser("~")
    assert os.path.join(home, ".ssh") in tmpfs_targets
    assert os.path.join(home, ".aws") in tmpfs_targets
    assert os.path.join(home, ".gnupg") in tmpfs_targets
    # original argv is appended at the end
    assert result[-len(original) :] == original
    # network NOT restricted (no --unshare-net)
    assert "--unshare-net" not in result


def test_wrap_bwrap_absent(tmp_path: Path) -> None:
    """When bwrap is not on PATH, wrap_bwrap returns original argv unchanged."""
    from hivepilot.utils.sandbox import wrap_bwrap

    original = ["claude", "--print", "hello"]
    with patch("hivepilot.utils.sandbox.shutil.which", return_value=None):
        result = wrap_bwrap(original, workdir=str(tmp_path))

    assert result is original or result == original


# ---------------------------------------------------------------------------
# ClaudeRunner sandbox integration tests
# ---------------------------------------------------------------------------


def _make_payload(tmp_path: Path, permission_mode: str | None = None) -> object:
    """Build a minimal RunnerPayload with a real prompt file."""
    from hivepilot.models import ProjectConfig, TaskStep
    from hivepilot.runners.base import RunnerPayload

    pf = tmp_path / "p.md"
    pf.write_text("do it", encoding="utf-8")
    step_meta: dict = {}
    if permission_mode:
        step_meta["permission_mode"] = permission_mode

    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude", prompt_file=str(pf), metadata=step_meta),
        metadata={},
        secrets={},
    )


def _make_runner(dev_sandbox: str = "bwrap", permission_mode: str | None = None) -> object:
    """Build a ClaudeRunner with the given sandbox setting."""
    from hivepilot.config import Settings
    from hivepilot.models import RunnerDefinition
    from hivepilot.runners.claude_runner import ClaudeRunner

    s = Settings()
    s.__dict__["dev_sandbox"] = dev_sandbox
    if permission_mode is not None:
        s.__dict__["claude_permission_mode"] = permission_mode
    return ClaudeRunner(RunnerDefinition(name="claude", kind="claude", command="claude"), s)


def test_claude_runner_sandboxed(tmp_path: Path, monkeypatch) -> None:
    """With dev_sandbox=bwrap + elevated permission_mode, capture() wraps argv with bwrap
    and the subprocess env is scrubbed (no SSH_AUTH_SOCK)."""
    from hivepilot.config import Settings
    from hivepilot.models import RunnerDefinition
    from hivepilot.runners.claude_runner import ClaudeRunner

    pf = tmp_path / "p.md"
    pf.write_text("do it", encoding="utf-8")

    from hivepilot.models import ProjectConfig, TaskStep
    from hivepilot.runners.base import RunnerPayload

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

    s = Settings()
    monkeypatch.setattr(s, "dev_sandbox", "bwrap", raising=False)
    runner = ClaudeRunner(RunnerDefinition(name="claude", kind="claude", command="claude"), s)

    # Inject a noisy env var that must be scrubbed
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")

    captured_calls: list[dict] = []

    def fake_run(*args, **kwargs):
        captured_calls.append({"argv": args[0] if args else kwargs.get("args"), **kwargs})
        return MagicMock(stdout="OK", returncode=0)

    with patch("hivepilot.runners.claude_runner.subprocess.run", side_effect=fake_run):
        with patch("hivepilot.utils.sandbox.shutil.which", return_value="/usr/bin/bwrap"):
            runner.capture(payload)

    assert captured_calls, "subprocess.run was never called"
    call = captured_calls[0]
    argv = call["argv"]
    env = call.get("env") or {}

    assert argv[0] == "bwrap", f"Expected argv to start with 'bwrap', got: {argv[0]!r}"
    assert "SSH_AUTH_SOCK" not in env, "SSH_AUTH_SOCK must be scrubbed from sandbox env"


def test_claude_runner_no_sandbox(tmp_path: Path, monkeypatch) -> None:
    """With dev_sandbox='none', capture() passes argv straight through (no bwrap)."""
    from hivepilot.config import Settings
    from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
    from hivepilot.runners.base import RunnerPayload
    from hivepilot.runners.claude_runner import ClaudeRunner

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

    s = Settings()
    monkeypatch.setattr(s, "dev_sandbox", "none", raising=False)
    runner = ClaudeRunner(RunnerDefinition(name="claude", kind="claude", command="claude"), s)

    captured_calls: list[dict] = []

    def fake_run(*args, **kwargs):
        captured_calls.append({"argv": args[0] if args else kwargs.get("args"), **kwargs})
        return MagicMock(stdout="OK", returncode=0)

    with patch("hivepilot.runners.claude_runner.subprocess.run", side_effect=fake_run):
        runner.capture(payload)

    assert captured_calls
    argv = captured_calls[0]["argv"]
    assert argv[0] != "bwrap", "dev_sandbox=none must NOT wrap with bwrap"


def test_claude_runner_remote_host(tmp_path: Path, monkeypatch) -> None:
    """When definition.host is set (SSH run), bwrap must NOT be applied."""
    from hivepilot.config import Settings
    from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
    from hivepilot.runners.base import RunnerPayload
    from hivepilot.runners.claude_runner import ClaudeRunner

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

    s = Settings()
    monkeypatch.setattr(s, "dev_sandbox", "bwrap", raising=False)
    # remote host set — bwrap must be skipped
    runner = ClaudeRunner(
        RunnerDefinition(name="claude", kind="claude", command="claude", host="dev-server"), s
    )

    captured_calls: list[dict] = []

    def fake_run(*args, **kwargs):
        captured_calls.append({"argv": args[0] if args else kwargs.get("args"), **kwargs})
        return MagicMock(stdout="OK", returncode=0)

    with patch("hivepilot.runners.claude_runner.subprocess.run", side_effect=fake_run):
        with patch("hivepilot.utils.sandbox.shutil.which", return_value="/usr/bin/bwrap"):
            runner.capture(payload)

    assert captured_calls
    argv = captured_calls[0]["argv"]
    # SSH-wrapped argv starts with "ssh", not "bwrap"
    assert argv[0] == "ssh", f"Remote run must use ssh, got: {argv[0]!r}"
    assert "bwrap" not in argv, "bwrap must not appear in SSH-wrapped argv"


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_otel_vars_reach_the_agent():
    """Without OTEL_* on the allowlist telemetry is scrubbed and silently off.

    That failure mode is indistinguishable from "the agent emits no metrics",
    which is how several things here have sat inert unnoticed.
    """
    from hivepilot.utils.sandbox import scrub_env

    kept = scrub_env(
        {
            "OTEL_METRICS_EXPORTER": "console",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "UNRELATED": "x",
        }
    )

    assert kept["OTEL_METRICS_EXPORTER"] == "console"
    assert kept["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert kept["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert "UNRELATED" not in kept


def test_prompt_logging_telemetry_never_passes_even_when_allowlisted():
    """The denial must survive a broadened allowlist.

    ``OTEL_*`` is a reasonable pattern to add, and it also matches the two
    variables that put prompt text — including resolved ``${secret:NAME}``
    values — into a metric stream written to run logs. If this test starts
    failing because someone widened a pattern, the widening is the bug.
    """
    from hivepilot.utils.sandbox import scrub_env

    env = {
        "OTEL_LOG_USER_PROMPTS": "1",
        "OTEL_LOG_RESPONSES": "true",
        "OTEL_METRICS_EXPORTER": "console",
    }

    # Even an allow-everything list must not let them through.
    for allowlist in ([], ["OTEL_*"], ["*"]):
        kept = scrub_env(env, allowlist or None)
        assert "OTEL_LOG_USER_PROMPTS" not in kept, f"leaked with allowlist={allowlist!r}"
        assert "OTEL_LOG_RESPONSES" not in kept, f"leaked with allowlist={allowlist!r}"


def test_telemetry_content_leaks_reports_names_only():
    from hivepilot.utils.sandbox import telemetry_content_leaks

    assert telemetry_content_leaks({"OTEL_LOG_USER_PROMPTS": "1"}) == ["OTEL_LOG_USER_PROMPTS"]
    # Absent, empty, and the explicit off-values are all "not set".
    for off in ("", "0", "false", "no", "  "):
        assert telemetry_content_leaks({"OTEL_LOG_USER_PROMPTS": off}) == []
    assert telemetry_content_leaks({}) == []
