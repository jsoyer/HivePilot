"""
Tests for the Atlantis IaC runner (Phase 17c): `AtlantisRunner`.

Mirrors `tests/test_iac_runner.py`'s structure for the sibling
terraform/opentofu/pulumi runners.

Covers:
(a) Registration: `resolve_runner_class("atlantis")` resolves to
    `AtlantisRunner` via the real RunnerRegistry/RUNNER_MAP.
(b) argv assembly for `plan` (default) / `apply`, plus `-p`/`--dir`
    project/dir flags and free-form `args` passthrough.
(c) `payload.secrets` land in the `env` mapping passed to `subprocess.run`
    (never in argv) -- same secrets-reach-the-environment contract as the
    other IaC runners.
(d) Missing binary -> RuntimeError, and `subprocess.run` is never called.
(e) Unknown operation -> ValueError.
(f) `is_destructive`: `apply` -> True, `plan` -> False.
(g) `run()` always executes with `capture_output=False` (live streaming)
    and the runner exposes no `capture()` method -- Atlantis wraps
    terraform under the hood, so plan/apply output can echo secret var
    values and must never be captured, returned, or persisted.
(h) cli-only: `supported_modes` is `{"cli"}` (mode:api fails closed at
    orchestrator validation, mirroring every other IaC runner).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.registry import resolve_runner_class
from hivepilot.runners.atlantis_runner import AtlantisRunner
from hivepilot.runners.base import RunnerPayload


def _payload(
    tmp_path: Path,
    *,
    operation: str | None = None,
    options: dict | None = None,
    secrets: dict[str, str] | None = None,
) -> RunnerPayload:
    return RunnerPayload(
        project_name="proj",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="atlantis", command=operation),
        metadata={},
        secrets=secrets or {},
    )


def _definition(options: dict | None = None) -> RunnerDefinition:
    return RunnerDefinition(name="atlantis", kind="atlantis", options=options or {})


class TestRegistration:
    def test_atlantis_resolves(self) -> None:
        assert resolve_runner_class("atlantis") is AtlantisRunner


class TestAtlantisArgv:
    def _run(self, tmp_path: Path, operation: str | None, options: dict | None = None):
        runner = AtlantisRunner(_definition(options), settings)
        with (
            patch(
                "hivepilot.runners.atlantis_runner.shutil.which",
                return_value="/usr/bin/atlantis",
            ),
            patch("hivepilot.runners.atlantis_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            runner.run(_payload(tmp_path, operation=operation, options=options))
        return mock_run

    def test_default_operation_is_plan(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, None)
        argv = mock_run.call_args.args[0]
        assert argv == ["atlantis", "plan"]

    def test_plan_explicit(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "plan")
        argv = mock_run.call_args.args[0]
        assert argv == ["atlantis", "plan"]

    def test_apply(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "apply")
        argv = mock_run.call_args.args[0]
        assert argv == ["atlantis", "apply"]

    def test_plan_with_project(self, tmp_path: Path) -> None:
        options = {"project": "prod-vpc"}
        mock_run = self._run(tmp_path, "plan", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["atlantis", "plan", "-p", "prod-vpc"]

    def test_plan_with_dir(self, tmp_path: Path) -> None:
        options = {"dir": "envs/prod"}
        mock_run = self._run(tmp_path, "plan", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["atlantis", "plan", "--dir", "envs/prod"]

    def test_plan_with_project_and_dir(self, tmp_path: Path) -> None:
        options = {"project": "prod-vpc", "dir": "envs/prod"}
        mock_run = self._run(tmp_path, "plan", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["atlantis", "plan", "-p", "prod-vpc", "--dir", "envs/prod"]

    def test_args_passthrough(self, tmp_path: Path) -> None:
        options = {"args": ["--verbose", "--no-color"]}
        mock_run = self._run(tmp_path, "plan", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["atlantis", "plan", "--verbose", "--no-color"]

    def test_apply_with_project_dir_and_args(self, tmp_path: Path) -> None:
        options = {"project": "prod-vpc", "dir": "envs/prod", "args": ["--verbose"]}
        mock_run = self._run(tmp_path, "apply", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["atlantis", "apply", "-p", "prod-vpc", "--dir", "envs/prod", "--verbose"]

    def test_unknown_operation_raises_value_error(self, tmp_path: Path) -> None:
        runner = AtlantisRunner(_definition(), settings)
        with (
            patch(
                "hivepilot.runners.atlantis_runner.shutil.which",
                return_value="/usr/bin/atlantis",
            ),
            patch("hivepilot.runners.atlantis_runner.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation="frobnicate"))

    def test_args_smuggled_apply_token_does_not_flip_destructive_gate(self, tmp_path: Path) -> None:
        """Adversarial regression: a YAML author could set operation=plan with
        options.args containing an "apply" token, hoping to sneak a
        destructive subcommand past the approval gate via a free-form
        passthrough flag. The gate MUST resolve strictly off `operation`
        (never scan `args`), and the smuggled tokens must land AFTER the
        `plan` subcommand in argv -- never replace it."""
        options = {"args": ["apply", "--auto-approve"]}
        runner = AtlantisRunner(_definition(options), settings)

        assert runner.is_destructive(_payload(tmp_path, operation="plan", options=options)) is (
            False
        )

        mock_run = self._run(tmp_path, "plan", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["atlantis", "plan", "apply", "--auto-approve"]
        assert argv[1] == "plan"

    def test_args_wrong_type_raises_value_error(self, tmp_path: Path) -> None:
        """`args` must be a list of strings -- a bare string (e.g. YAML
        `args: "apply"`) would silently iterate character-by-character
        (['a', 'p', 'p', 'l', 'y']) if appended without a type guard."""
        options = {"args": "apply"}
        runner = AtlantisRunner(_definition(options), settings)
        with (
            patch(
                "hivepilot.runners.atlantis_runner.shutil.which",
                return_value="/usr/bin/atlantis",
            ),
            patch("hivepilot.runners.atlantis_runner.subprocess.run") as mock_run,
        ):
            with pytest.raises(ValueError, match="args.*must be a list"):
                runner.run(_payload(tmp_path, operation="plan", options=options))
        mock_run.assert_not_called()


class TestMissingBinary:
    def test_missing_binary_raises_runtime_error(self, tmp_path: Path) -> None:
        runner = AtlantisRunner(_definition(), settings)
        with patch("hivepilot.runners.atlantis_runner.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="atlantis CLI not found"):
                runner.run(_payload(tmp_path, operation="plan"))

    def test_missing_binary_never_calls_subprocess(self, tmp_path: Path) -> None:
        runner = AtlantisRunner(_definition(), settings)
        with (
            patch("hivepilot.runners.atlantis_runner.shutil.which", return_value=None),
            patch("hivepilot.runners.atlantis_runner.subprocess.run") as mock_run,
        ):
            with pytest.raises(RuntimeError):
                runner.run(_payload(tmp_path, operation="plan"))
        mock_run.assert_not_called()


class TestSecretsReachTheEnvironment:
    """Regression-shape test mirroring the Tf/Pulumi runners: `payload.secrets`
    must land in the `env` dict passed to `subprocess.run`, never in argv."""

    def test_secrets_in_env_not_argv(self, tmp_path: Path) -> None:
        runner = AtlantisRunner(_definition(), settings)
        secrets = {"ATLANTIS_GH_TOKEN": "s3cr3t-value"}
        with (
            patch(
                "hivepilot.runners.atlantis_runner.shutil.which",
                return_value="/usr/bin/atlantis",
            ),
            patch("hivepilot.runners.atlantis_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            runner.run(_payload(tmp_path, operation="plan", secrets=secrets))

        env = mock_run.call_args.kwargs["env"]
        argv = mock_run.call_args.args[0]
        assert env["ATLANTIS_GH_TOKEN"] == "s3cr3t-value"
        assert "s3cr3t-value" not in argv

    def test_secrets_win_over_project_and_definition_env(self, tmp_path: Path) -> None:
        definition = RunnerDefinition(
            name="atlantis",
            kind="atlantis",
            env={"ATLANTIS_GH_TOKEN": "definition-value"},
        )
        runner = AtlantisRunner(definition, settings)
        payload = RunnerPayload(
            project_name="proj",
            project=ProjectConfig(path=tmp_path, env={"ATLANTIS_GH_TOKEN": "project-value"}),
            task_name="t",
            step=TaskStep(name="s", runner="atlantis", command="plan"),
            metadata={},
            secrets={"ATLANTIS_GH_TOKEN": "secret-value"},
        )
        with (
            patch(
                "hivepilot.runners.atlantis_runner.shutil.which",
                return_value="/usr/bin/atlantis",
            ),
            patch("hivepilot.runners.atlantis_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            runner.run(payload)

        env = mock_run.call_args.kwargs["env"]
        assert env["ATLANTIS_GH_TOKEN"] == "secret-value"


class TestNoCaptureLeakPath:
    """Atlantis wraps terraform under the hood -- plan/apply output can echo
    `TF_VAR_*`/secret values, and the Phase 10c redaction choke point
    (`redact_text`) only masks values explicitly registered via `${secret:}`
    resolution. `run()` must always execute live (`capture_output=False`)
    and the runner must expose no `capture()` method, exactly like the
    terraform/opentofu/pulumi/helm runners."""

    def test_runner_has_no_capture_method(self) -> None:
        runner = AtlantisRunner(_definition(), settings)
        assert not hasattr(runner, "capture")

    def test_run_streams_live_not_captured(self, tmp_path: Path) -> None:
        runner = AtlantisRunner(_definition(), settings)
        with (
            patch(
                "hivepilot.runners.atlantis_runner.shutil.which",
                return_value="/usr/bin/atlantis",
            ),
            patch("hivepilot.runners.atlantis_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="some plan output")
            runner.run(_payload(tmp_path, operation="plan"))

        assert mock_run.call_args.kwargs["capture_output"] is False


class TestIsDestructive:
    """`is_destructive(payload)` -- the optional, structural (getattr-
    discovered, like `capture`) contract the step-level approval gate
    (`hivepilot.orchestrator.step_requires_approval`) queries. Resolves the
    operation exactly the same way `run()`/`_execute()` does."""

    def test_apply_is_destructive(self, tmp_path: Path) -> None:
        runner = AtlantisRunner(_definition(), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="apply")) is True

    def test_plan_is_not_destructive(self, tmp_path: Path) -> None:
        runner = AtlantisRunner(_definition(), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="plan")) is False

    def test_default_operation_when_unset_is_not_destructive(self, tmp_path: Path) -> None:
        runner = AtlantisRunner(_definition(), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=None)) is False


class TestModeApiFailsClosed:
    def test_supported_modes_is_cli_only(self) -> None:
        runner = AtlantisRunner(_definition(), settings)
        assert runner.supported_modes == frozenset({"cli"})
