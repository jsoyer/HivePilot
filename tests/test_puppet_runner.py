"""
Tests for the Puppet runner (Phase 17b): a builtin runner so HivePilot
agents can run `puppet apply`/`puppet agent --test`, with the mutating
`apply`/`agent` operations auto-gating via the step-level approval gate
(``hivepilot.orchestrator.step_requires_approval``).

Covers, mirroring ``tests/test_ansible_runner.py``'s pattern:
(a) Registration: `resolve_runner_class("puppet")` resolves via the real
    RunnerRegistry/RUNNER_MAP, and "puppet" is advertised in
    KNOWN_RUNNER_KINDS.
(b) argv assembly for every operation (apply, agent, noop), incl.
    `options.agent` switching `noop`'s underlying command and
    `options.environment`.
(c) `payload.secrets` land in the env passed to `subprocess.run`.
(d) Unknown operation -> ValueError.
(e) Missing required option (manifest for apply / noop-without-agent) ->
    ValueError.
(f) Missing binary (puppet) -> RuntimeError (subprocess NOT called).
(g) `is_destructive`: apply/agent -> True; noop -> False (even with
    options.agent).
(h) No `capture()` method exposed; `run()` always streams live
    (`capture_output=False`).
(i) Gate<->argv agreement test: is_destructive() and the actually-executed
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
from hivepilot.runners.puppet_runner import PuppetRunner

_MODULE = "hivepilot.runners.puppet_runner"


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
        step=TaskStep(name="s", runner="puppet", command=operation),
        metadata={},
        secrets=secrets or {},
    )


def _definition(options: dict | None = None, env: dict | None = None) -> RunnerDefinition:
    return RunnerDefinition(name="puppet", kind="puppet", options=options or {}, env=env or {})


class TestRegistration:
    def test_puppet_resolves(self) -> None:
        assert resolve_runner_class("puppet") is PuppetRunner

    def test_puppet_in_known_runner_kinds(self) -> None:
        assert "puppet" in KNOWN_RUNNER_KINDS

    def test_puppet_registered_in_runner_map(self) -> None:
        assert RUNNER_MAP["puppet"] is PuppetRunner


class TestArgv:
    def _run(self, tmp_path: Path, operation: str | None, options: dict | None = None):
        runner = PuppetRunner(_definition(options), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/puppet"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation=operation))
        return mock_run

    def test_apply_basic(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "apply", {"manifest": "site.pp"})
        argv = mock_run.call_args.args[0]
        assert argv == ["puppet", "apply", "site.pp"]

    def test_apply_with_environment(self, tmp_path: Path) -> None:
        options = {"manifest": "site.pp", "environment": "production"}
        mock_run = self._run(tmp_path, "apply", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["puppet", "apply", "site.pp", "--environment", "production"]

    def test_agent_basic(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "agent", {})
        argv = mock_run.call_args.args[0]
        assert argv == ["puppet", "agent", "--test"]

    def test_agent_with_environment(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "agent", {"environment": "production"})
        argv = mock_run.call_args.args[0]
        assert argv == ["puppet", "agent", "--test", "--environment", "production"]

    def test_noop_uses_apply_by_default(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "noop", {"manifest": "site.pp"})
        argv = mock_run.call_args.args[0]
        assert argv == ["puppet", "apply", "site.pp", "--noop"]

    def test_noop_uses_agent_when_options_agent(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "noop", {"agent": True})
        argv = mock_run.call_args.args[0]
        assert argv == ["puppet", "agent", "--test", "--noop"]

    def test_noop_with_environment(self, tmp_path: Path) -> None:
        options = {"manifest": "site.pp", "environment": "staging"}
        mock_run = self._run(tmp_path, "noop", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["puppet", "apply", "site.pp", "--noop", "--environment", "staging"]

    def test_default_operation_when_unset_is_noop_requires_manifest(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, None, {"manifest": "site.pp"})
        argv = mock_run.call_args.args[0]
        assert argv == ["puppet", "apply", "site.pp", "--noop"]

    def test_unknown_operation_raises_value_error(self, tmp_path: Path) -> None:
        runner = PuppetRunner(_definition(), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/puppet"),
            patch(f"{_MODULE}.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation="bogus"))


class TestMissingRequiredOptions:
    @pytest.mark.parametrize(
        "operation,options",
        [
            ("apply", {}),
            ("noop", {}),
        ],
    )
    def test_missing_manifest_raises_value_error(
        self, tmp_path: Path, operation: str, options: dict
    ) -> None:
        runner = PuppetRunner(_definition(options), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/puppet"),
            patch(f"{_MODULE}.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation=operation))


class TestMissingBinary:
    @pytest.mark.parametrize(
        "operation,options",
        [
            ("apply", {"manifest": "site.pp"}),
            ("agent", {}),
            ("noop", {"manifest": "site.pp"}),
        ],
    )
    def test_missing_binary_raises_runtime_error(
        self, tmp_path: Path, operation: str, options: dict
    ) -> None:
        runner = PuppetRunner(_definition(options), settings)
        with patch(f"{_MODULE}.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="not found"):
                runner.run(_payload(tmp_path, operation=operation))

    def test_missing_binary_never_calls_subprocess(self, tmp_path: Path) -> None:
        runner = PuppetRunner(_definition({"manifest": "site.pp"}), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value=None),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            with pytest.raises(RuntimeError):
                runner.run(_payload(tmp_path, operation="apply"))
        mock_run.assert_not_called()


class TestSecretsReachTheEnvironment:
    def test_secrets_in_env(self, tmp_path: Path) -> None:
        runner = PuppetRunner(_definition({"manifest": "site.pp"}), settings)
        secrets = {"PUPPET_RUN_TOKEN": "sekret"}
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/puppet"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="apply", secrets=secrets))

        env = mock_run.call_args.kwargs["env"]
        assert env["PUPPET_RUN_TOKEN"] == "sekret"


class TestIsDestructive:
    def test_apply_is_destructive(self, tmp_path: Path) -> None:
        runner = PuppetRunner(_definition({"manifest": "site.pp"}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="apply")) is True

    def test_agent_is_destructive(self, tmp_path: Path) -> None:
        runner = PuppetRunner(_definition({}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="agent")) is True

    def test_noop_is_not_destructive(self, tmp_path: Path) -> None:
        runner = PuppetRunner(_definition({"manifest": "site.pp"}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="noop")) is False

    def test_noop_with_agent_option_is_not_destructive(self, tmp_path: Path) -> None:
        runner = PuppetRunner(_definition({"agent": True}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="noop")) is False

    def test_default_operation_when_unset_is_not_destructive(self, tmp_path: Path) -> None:
        runner = PuppetRunner(_definition(), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=None)) is False


class TestNoCaptureLeakPath:
    def test_runner_has_no_capture_method(self) -> None:
        runner = PuppetRunner(_definition(), settings)
        assert not hasattr(runner, "capture")

    def test_run_streams_live_not_captured(self, tmp_path: Path) -> None:
        runner = PuppetRunner(_definition({"manifest": "site.pp"}), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/puppet"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="apply"))

        assert mock_run.call_args.kwargs["capture_output"] is False


class TestCaseNormalization:
    def test_case_variant_operation_normalizes_and_works(self, tmp_path: Path) -> None:
        runner = PuppetRunner(_definition({"manifest": "site.pp"}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="Apply")) is True
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/puppet"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="Apply"))
        argv = mock_run.call_args.args[0]
        assert argv == ["puppet", "apply", "site.pp"]


class TestGateArgvAgreement:
    """Phase 17b: `is_destructive()` and `_build_command()` both resolve the
    operation from `_resolve_operation`, so they can never disagree about
    whether an operation mutates the node. For options that make
    `_build_command` raise (missing manifest), that's fine — fail-closed
    execution is a valid way to never disagree with a False gate."""

    def _argv_is_mutating(self, argv: list[str]) -> bool:
        return "--noop" not in argv

    @pytest.mark.parametrize(
        "operation,options,expect_raises",
        [
            ("apply", {"manifest": "site.pp"}, False),
            ("apply", {}, True),
            ("agent", {}, False),
            ("noop", {"manifest": "site.pp"}, False),
            ("noop", {"agent": True}, False),
            ("noop", {}, True),
        ],
    )
    def test_gate_agrees_with_executed_argv(
        self, tmp_path: Path, operation: str, options: dict, expect_raises: bool
    ) -> None:
        runner = PuppetRunner(_definition(options), settings)
        gate_result = runner.is_destructive(_payload(tmp_path, operation=operation))

        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/puppet"),
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
