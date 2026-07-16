"""
Tests for the packer runner (Phase 17c Sprint 2): a builtin runner so
HivePilot agents can validate/build machine images, with side-effecting
operations (``build``/``fmt``) auto-gating via the step-level approval gate
(``hivepilot.orchestrator.step_requires_approval``).

Covers, mirroring ``tests/test_helm_runner.py``'s pattern:
(a) Registration: `resolve_runner_class("packer")` resolves via the real
    RunnerRegistry/RUNNER_MAP, and "packer" is advertised in
    KNOWN_RUNNER_KINDS.
(b) argv assembly for every operation (validate, fmt, init, inspect, build)
    with var/var_file/only/except/force appended when set.
(c) `payload.secrets` land in the env passed to `subprocess.run` (cloud
    creds via env, never argv).
(d) Unknown operation -> ValueError.
(e) Missing required option (template) -> clear ValueError.
(f) Missing `packer` binary -> RuntimeError (subprocess NOT called).
(g) `is_destructive`: build/fmt -> True; validate/init/inspect -> False.
    Default operation (unset) is `validate`, which is not destructive.
(h) No `capture()` method exposed; `run()` always streams live
    (`capture_output=False`).
(i) Gate<->argv agreement test: is_destructive() and the actually-executed
    argv can never disagree about whether an operation has real
    side-effects (builds an image / mutates source files).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import KNOWN_RUNNER_KINDS, ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.registry import RUNNER_MAP, resolve_runner_class
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.packer_runner import PackerRunner

_MODULE = "hivepilot.runners.packer_runner"


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
        step=TaskStep(name="s", runner="packer", command=operation),
        metadata={},
        secrets=secrets or {},
    )


def _definition(options: dict | None = None, env: dict | None = None) -> RunnerDefinition:
    return RunnerDefinition(name="packer", kind="packer", options=options or {}, env=env or {})


class TestRegistration:
    def test_packer_resolves(self) -> None:
        assert resolve_runner_class("packer") is PackerRunner

    def test_packer_in_known_runner_kinds(self) -> None:
        assert "packer" in KNOWN_RUNNER_KINDS

    def test_packer_registered_in_runner_map(self) -> None:
        assert RUNNER_MAP["packer"] is PackerRunner


class TestArgv:
    def _run(self, tmp_path: Path, operation: str | None, options: dict | None = None):
        runner = PackerRunner(_definition(options), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/packer"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation=operation))
        return mock_run

    def test_validate_default_operation(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, None, {"template": "image.pkr.hcl"})
        argv = mock_run.call_args.args[0]
        assert argv == ["packer", "validate", "image.pkr.hcl"]

    def test_validate_with_vars(self, tmp_path: Path) -> None:
        options = {
            "template": "image.pkr.hcl",
            "var": {"region": "us-east-1"},
            "var_file": "prod.pkrvars.hcl",
            "only": "amazon-ebs.example",
            "except": "docker.example",
        }
        mock_run = self._run(tmp_path, "validate", options)
        argv = mock_run.call_args.args[0]
        assert argv == [
            "packer",
            "validate",
            "-var",
            "region=us-east-1",
            "-var-file",
            "prod.pkrvars.hcl",
            "-only=amazon-ebs.example",
            "-except=docker.example",
            "image.pkr.hcl",
        ]

    def test_validate_with_multiple_vars(self, tmp_path: Path) -> None:
        options = {
            "template": "image.pkr.hcl",
            "var": {"region": "us-east-1", "instance_type": "t3.micro"},
        }
        mock_run = self._run(tmp_path, "validate", options)
        argv = mock_run.call_args.args[0]
        assert argv == [
            "packer",
            "validate",
            "-var",
            "region=us-east-1",
            "-var",
            "instance_type=t3.micro",
            "image.pkr.hcl",
        ]

    def test_fmt(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "fmt", {"template": "image.pkr.hcl"})
        argv = mock_run.call_args.args[0]
        assert argv == ["packer", "fmt", "image.pkr.hcl"]

    def test_init(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "init", {"template": "image.pkr.hcl"})
        argv = mock_run.call_args.args[0]
        assert argv == ["packer", "init", "image.pkr.hcl"]

    def test_inspect(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "inspect", {"template": "image.pkr.hcl"})
        argv = mock_run.call_args.args[0]
        assert argv == ["packer", "inspect", "image.pkr.hcl"]

    def test_build(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "build", {"template": "image.pkr.hcl"})
        argv = mock_run.call_args.args[0]
        assert argv == ["packer", "build", "image.pkr.hcl"]

    def test_build_with_force(self, tmp_path: Path) -> None:
        options = {"template": "image.pkr.hcl", "force": True}
        mock_run = self._run(tmp_path, "build", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["packer", "build", "-force", "image.pkr.hcl"]

    def test_fmt_ignores_var_options(self, tmp_path: Path) -> None:
        options = {"template": "image.pkr.hcl", "var": {"region": "us-east-1"}}
        mock_run = self._run(tmp_path, "fmt", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["packer", "fmt", "image.pkr.hcl"]

    def test_unknown_operation_raises_value_error(self, tmp_path: Path) -> None:
        runner = PackerRunner(_definition({"template": "image.pkr.hcl"}), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/packer"),
            patch(f"{_MODULE}.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation="bogus"))


class TestMissingRequiredOptions:
    @pytest.mark.parametrize(
        "operation",
        ["validate", "fmt", "init", "inspect", "build"],
    )
    def test_missing_template_raises_value_error(self, tmp_path: Path, operation: str) -> None:
        runner = PackerRunner(_definition({}), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/packer"),
            patch(f"{_MODULE}.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation=operation))


class TestMissingBinary:
    def test_missing_binary_raises_runtime_error(self, tmp_path: Path) -> None:
        runner = PackerRunner(_definition({"template": "image.pkr.hcl"}), settings)
        with patch(f"{_MODULE}.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="packer CLI not found"):
                runner.run(_payload(tmp_path, operation="validate"))

    def test_missing_binary_never_calls_subprocess(self, tmp_path: Path) -> None:
        runner = PackerRunner(_definition({"template": "image.pkr.hcl"}), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value=None),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            with pytest.raises(RuntimeError):
                runner.run(_payload(tmp_path, operation="validate"))
        mock_run.assert_not_called()


class TestSecretsReachTheEnvironment:
    def test_secrets_in_env(self, tmp_path: Path) -> None:
        runner = PackerRunner(_definition({"template": "image.pkr.hcl"}), settings)
        secrets = {"AWS_SECRET_ACCESS_KEY": "s3cr3t-value"}
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/packer"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="validate", secrets=secrets))

        env = mock_run.call_args.kwargs["env"]
        assert env["AWS_SECRET_ACCESS_KEY"] == "s3cr3t-value"
        argv = mock_run.call_args.args[0]
        assert "s3cr3t-value" not in argv

    def test_secrets_win_over_project_and_definition_env(self, tmp_path: Path) -> None:
        definition = _definition(
            {"template": "image.pkr.hcl"}, env={"AWS_SECRET_ACCESS_KEY": "definition-value"}
        )
        runner = PackerRunner(definition, settings)
        payload = RunnerPayload(
            project_name="proj",
            project=ProjectConfig(path=tmp_path, env={"AWS_SECRET_ACCESS_KEY": "project-value"}),
            task_name="t",
            step=TaskStep(name="s", runner="packer", command="validate"),
            metadata={},
            secrets={"AWS_SECRET_ACCESS_KEY": "secret-value"},
        )
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/packer"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(payload)

        env = mock_run.call_args.kwargs["env"]
        assert env["AWS_SECRET_ACCESS_KEY"] == "secret-value"


class TestIsDestructive:
    @pytest.mark.parametrize("operation", ["build", "fmt"])
    def test_destructive_ops(self, tmp_path: Path, operation: str) -> None:
        options = {"template": "image.pkr.hcl"}
        runner = PackerRunner(_definition(options), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=operation)) is True

    @pytest.mark.parametrize("operation", ["validate", "init", "inspect"])
    def test_non_destructive_ops(self, tmp_path: Path, operation: str) -> None:
        options = {"template": "image.pkr.hcl"}
        runner = PackerRunner(_definition(options), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=operation)) is False

    def test_default_operation_when_unset_is_not_destructive(self, tmp_path: Path) -> None:
        runner = PackerRunner(_definition({"template": "image.pkr.hcl"}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=None)) is False


class TestNoCaptureLeakPath:
    def test_runner_has_no_capture_method(self) -> None:
        runner = PackerRunner(_definition(), settings)
        assert not hasattr(runner, "capture")

    def test_run_streams_live_not_captured(self, tmp_path: Path) -> None:
        runner = PackerRunner(_definition({"template": "image.pkr.hcl"}), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/packer"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="validate"))

        assert mock_run.call_args.kwargs["capture_output"] is False


class TestCaseNormalization:
    def test_case_variant_operation_normalizes_and_works(self, tmp_path: Path) -> None:
        runner = PackerRunner(_definition({"template": "image.pkr.hcl"}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="Build")) is True
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/packer"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="Build"))
        argv = mock_run.call_args.args[0]
        assert argv == ["packer", "build", "image.pkr.hcl"]


class TestGateArgvAgreement:
    """`is_destructive()` and `_build_command()` both resolve the operation
    from the SAME `options` dict via the same helper, so they can never
    disagree about whether an operation has real side-effects (builds an
    image / mutates source files). For options that make `_build_command`
    raise (missing required option, unknown operation), that's fine —
    fail-closed execution is a valid way to never disagree with a False
    gate."""

    _MUTATING_OPS = frozenset({"build", "fmt"})

    def _argv_is_mutating(self, argv: list[str]) -> bool:
        return argv[1] in self._MUTATING_OPS

    @pytest.mark.parametrize(
        "operation,options,expect_raises",
        [
            ("validate", {"template": "image.pkr.hcl"}, False),
            ("validate", {}, True),
            ("fmt", {"template": "image.pkr.hcl"}, False),
            ("init", {"template": "image.pkr.hcl"}, False),
            ("inspect", {"template": "image.pkr.hcl"}, False),
            ("build", {"template": "image.pkr.hcl"}, False),
            ("build", {}, True),
        ],
    )
    def test_gate_agrees_with_executed_argv(
        self, tmp_path: Path, operation: str, options: dict, expect_raises: bool
    ) -> None:
        runner = PackerRunner(_definition(options), settings)
        gate_result = runner.is_destructive(_payload(tmp_path, operation=operation))

        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/packer"),
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
