"""Container runner: docker/podman runtime (configurable) + volume safety."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.container_runner import ContainerRunner, _validate_volume


def _payload(tmp_path: Path) -> RunnerPayload:
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="container"),
        metadata={},
        secrets={},
    )


def _runner(options: dict) -> ContainerRunner:
    return ContainerRunner(RunnerDefinition(name="c", kind="container", options=options), settings)


def _run_cmd(runner: ContainerRunner, tmp_path: Path):

    with patch("hivepilot.runners.container_runner.subprocess.run") as m:
        runner.run(_payload(tmp_path))
    return m.call_args.args[0]


def test_defaults_to_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "container_runtime", "docker", raising=False)
    cmd = _run_cmd(_runner({"image": "img", "command": "echo hi"}), tmp_path)
    assert cmd[0] == "docker"
    assert cmd[1] == "run"
    assert cmd[-4:] == ["img", "bash", "-lc", "echo hi"]


def test_podman_via_runner_option(tmp_path: Path) -> None:
    cmd = _run_cmd(_runner({"image": "img", "command": "echo hi", "runtime": "podman"}), tmp_path)
    assert cmd[0] == "podman"


def test_global_setting_selects_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "container_runtime", "podman", raising=False)
    cmd = _run_cmd(_runner({"image": "img", "command": "echo hi"}), tmp_path)
    assert cmd[0] == "podman"


def test_option_overrides_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "container_runtime", "podman", raising=False)
    cmd = _run_cmd(_runner({"image": "img", "command": "echo hi", "runtime": "docker"}), tmp_path)
    assert cmd[0] == "docker"


def test_invalid_runtime_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="runtime"):
        _run_cmd(_runner({"image": "img", "command": "echo hi", "runtime": "rocket"}), tmp_path)


def test_requires_image_and_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _run_cmd(_runner({"image": "img"}), tmp_path)


def test_volume_validation_blocks_sensitive_paths() -> None:
    with pytest.raises(ValueError):
        _validate_volume("/etc:/x")


def test_docker_remote_sets_docker_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "container_runtime", "docker", raising=False)
    r = _runner({"image": "img", "command": "echo hi", "host": "ssh://user@hostB"})
    with patch("hivepilot.runners.container_runner.subprocess.run") as m:
        r.run(_payload(tmp_path))
    assert m.call_args.args[0][0] == "docker"
    assert m.call_args.kwargs["env"]["DOCKER_HOST"] == "ssh://user@hostB"


def test_podman_remote_uses_url_flags(tmp_path: Path) -> None:
    url = "ssh://user@hostB/run/podman/podman.sock"
    r = _runner({"image": "img", "command": "echo hi", "runtime": "podman", "host": url})
    with patch("hivepilot.runners.container_runner.subprocess.run") as m:
        r.run(_payload(tmp_path))
    cmd = m.call_args.args[0]
    assert cmd[0] == "podman"
    assert "--remote" in cmd
    assert cmd[cmd.index("--url") + 1] == url
    assert cmd.index("--url") < cmd.index("run")  # remote flags precede the subcommand


def test_local_engine_has_no_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "container_runtime", "docker", raising=False)
    r = _runner({"image": "img", "command": "echo hi"})
    with patch("hivepilot.runners.container_runner.subprocess.run") as m:
        r.run(_payload(tmp_path))
    assert m.call_args.kwargs["env"] is None


def test_invalid_engine_host_rejected(tmp_path: Path) -> None:
    r = _runner({"image": "img", "command": "echo hi", "host": "-evil"})
    with patch("hivepilot.runners.container_runner.subprocess.run"):
        with pytest.raises(ValueError, match="engine host"):
            r.run(_payload(tmp_path))
