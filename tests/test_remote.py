"""SSH remote-execution helper: run an agent's CLI on another machine."""

from __future__ import annotations

from pathlib import Path

from hivepilot.utils.remote import build_invocation, ssh_wrap


def test_ssh_wrap_builds_remote_command() -> None:
    cmd = ssh_wrap(["claude", "--print", "hi"], Path("/repo"), {"FOO": "bar"}, host="user@hostB")
    assert cmd[0] == "ssh"
    assert "BatchMode=yes" in cmd
    assert cmd[-2] == "user@hostB"
    remote = cmd[-1]
    assert remote.startswith("cd /repo &&")
    assert "FOO=bar" in remote
    assert "claude --print hi" in remote


def test_ssh_wrap_quotes_paths_and_values() -> None:
    cmd = ssh_wrap(["x"], Path("/my repo"), {"K": "a b"}, host="h")
    remote = cmd[-1]
    assert "'/my repo'" in remote
    assert "K='a b'" in remote


def test_ssh_wrap_passes_ssh_options() -> None:
    cmd = ssh_wrap(["x"], Path("/r"), {}, host="h", ssh_options=["ConnectTimeout=5"])
    assert "ConnectTimeout=5" in cmd


def test_build_invocation_local_passthrough() -> None:
    args, cwd, env = build_invocation(["x"], Path("/repo"), {"A": "1"}, host=None)
    assert args == ["x"]
    assert cwd == "/repo"
    assert env == {"A": "1"}


def test_build_invocation_remote_runs_ssh_locally() -> None:
    args, cwd, env = build_invocation(["x"], Path("/repo"), {"A": "1"}, host="hostB")
    assert args[0] == "ssh"
    # ssh runs locally with the operator's ambient env (keys/agent), not the task env
    assert cwd is None
    assert env is None
