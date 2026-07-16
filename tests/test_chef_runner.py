"""
Tests for the Chef runner (Phase 17b): a builtin runner so HivePilot agents
can run `chef-client` converges / why-run dry-runs, with the mutating
`converge` operation auto-gating via the step-level approval gate
(``hivepilot.orchestrator.step_requires_approval``).

Covers, mirroring ``tests/test_ansible_runner.py``'s pattern:
(a) Registration: `resolve_runner_class("chef")` resolves via the real
    RunnerRegistry/RUNNER_MAP, and "chef" is advertised in
    KNOWN_RUNNER_KINDS.
(b) argv assembly for both operations (converge, why-run), incl.
    config/runlist/local_mode options.
(c) `payload.secrets` land in the env passed to `subprocess.run`.
(d) Unknown operation -> ValueError.
(e) Missing binary (chef-client) -> RuntimeError (subprocess NOT called).
(f) `is_destructive`: converge -> True; why-run -> False.
(g) No `capture()` method exposed; `run()` always streams live
    (`capture_output=False`).
(h) Gate<->argv agreement test: is_destructive() and the actually-executed
    argv can never disagree about whether an operation mutates the node.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import KNOWN_RUNNER_KINDS, ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.registry import RUNNER_MAP, resolve_runner_class
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.chef_runner import ChefRunner

_MODULE = "hivepilot.runners.chef_runner"


def _payload(
    tmp_path: Path,
    *,
    operation: str | None = None,
    secrets: dict[str, str] | None = None,
) -> RunnerPayload:
    return RunnerPayload(
        project_name="proj",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="chef", command=operation),
        metadata={},
        secrets=secrets or {},
    )


def _definition(options: dict | None = None, env: dict | None = None) -> RunnerDefinition:
    return RunnerDefinition(name="chef", kind="chef", options=options or {}, env=env or {})


class TestRegistration:
    def test_chef_resolves(self) -> None:
        assert resolve_runner_class("chef") is ChefRunner

    def test_chef_in_known_runner_kinds(self) -> None:
        assert "chef" in KNOWN_RUNNER_KINDS

    def test_chef_registered_in_runner_map(self) -> None:
        assert RUNNER_MAP["chef"] is ChefRunner


class TestArgv:
    def _run(self, tmp_path: Path, operation: str | None, options: dict | None = None):
        runner = ChefRunner(_definition(options), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/chef-client"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation=operation))
        return mock_run

    def test_converge_basic(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "converge", {})
        argv = mock_run.call_args.args[0]
        assert argv == ["chef-client"]

    def test_why_run_basic(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "why-run", {})
        argv = mock_run.call_args.args[0]
        assert argv == ["chef-client", "--why-run"]

    def test_default_operation_when_unset_is_why_run(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, None, {})
        argv = mock_run.call_args.args[0]
        assert argv == ["chef-client", "--why-run"]

    def test_converge_with_config_runlist_local_mode(self, tmp_path: Path) -> None:
        options = {
            "config": "/etc/chef/client.rb",
            "runlist": "recipe[base]",
            "local_mode": True,
        }
        mock_run = self._run(tmp_path, "converge", options)
        argv = mock_run.call_args.args[0]
        assert argv == [
            "chef-client",
            "-c",
            "/etc/chef/client.rb",
            "--override-runlist",
            "recipe[base]",
            "-z",
        ]

    def test_why_run_with_config(self, tmp_path: Path) -> None:
        options = {"config": "/etc/chef/client.rb"}
        mock_run = self._run(tmp_path, "why-run", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["chef-client", "--why-run", "-c", "/etc/chef/client.rb"]

    def test_unknown_operation_raises_value_error(self, tmp_path: Path) -> None:
        runner = ChefRunner(_definition(), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/chef-client"),
            patch(f"{_MODULE}.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation="bogus"))


class TestMissingBinary:
    @pytest.mark.parametrize("operation", ["converge", "why-run"])
    def test_missing_binary_raises_runtime_error(self, tmp_path: Path, operation: str) -> None:
        runner = ChefRunner(_definition(), settings)
        with patch(f"{_MODULE}.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="not found"):
                runner.run(_payload(tmp_path, operation=operation))

    def test_missing_binary_never_calls_subprocess(self, tmp_path: Path) -> None:
        runner = ChefRunner(_definition(), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value=None),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            with pytest.raises(RuntimeError):
                runner.run(_payload(tmp_path, operation="converge"))
        mock_run.assert_not_called()


class TestSecretsReachTheEnvironment:
    def test_secrets_in_env(self, tmp_path: Path) -> None:
        runner = ChefRunner(_definition(), settings)
        secrets = {"CHEF_CLIENT_KEY": "sekret"}
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/chef-client"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="why-run", secrets=secrets))

        env = mock_run.call_args.kwargs["env"]
        assert env["CHEF_CLIENT_KEY"] == "sekret"


class TestIsDestructive:
    def test_converge_is_destructive(self, tmp_path: Path) -> None:
        runner = ChefRunner(_definition(), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="converge")) is True

    def test_why_run_is_not_destructive(self, tmp_path: Path) -> None:
        runner = ChefRunner(_definition(), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="why-run")) is False

    def test_default_operation_when_unset_is_not_destructive(self, tmp_path: Path) -> None:
        runner = ChefRunner(_definition(), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=None)) is False


class TestNoCaptureLeakPath:
    def test_runner_has_no_capture_method(self) -> None:
        runner = ChefRunner(_definition(), settings)
        assert not hasattr(runner, "capture")

    def test_run_streams_live_not_captured(self, tmp_path: Path) -> None:
        runner = ChefRunner(_definition(), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/chef-client"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="converge"))

        assert mock_run.call_args.kwargs["capture_output"] is False


class TestCaseNormalization:
    def test_case_variant_operation_normalizes_and_works(self, tmp_path: Path) -> None:
        runner = ChefRunner(_definition(), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="Converge")) is True
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/chef-client"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="Converge"))
        argv = mock_run.call_args.args[0]
        assert argv == ["chef-client"]


class TestGateArgvAgreement:
    """Phase 17b: `is_destructive()` and `_build_command()` both resolve the
    operation from `_resolve_operation`, so they can never disagree about
    whether an operation mutates the node."""

    def _argv_is_mutating(self, argv: list[str]) -> bool:
        return "--why-run" not in argv

    @pytest.mark.parametrize(
        "operation,options,expect_raises",
        [
            ("converge", {}, False),
            ("why-run", {}, False),
            ("converge", {"config": "/etc/chef/client.rb"}, False),
        ],
    )
    def test_gate_agrees_with_executed_argv(
        self, tmp_path: Path, operation: str, options: dict, expect_raises: bool
    ) -> None:
        runner = ChefRunner(_definition(options), settings)
        gate_result = runner.is_destructive(_payload(tmp_path, operation=operation))

        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/chef-client"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            if expect_raises:
                with pytest.raises(ValueError):
                    runner.run(_payload(tmp_path, operation=operation))
                return
            runner.run(_payload(tmp_path, operation=operation))

        argv = mock_run.call_args.args[0]
        assert gate_result is self._argv_is_mutating(argv)
