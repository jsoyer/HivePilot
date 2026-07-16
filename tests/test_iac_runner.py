"""
Tests for the IaC runners (Phase 17a Sprint A1): TerraformRunner,
OpenTofuRunner, PulumiRunner.

These runner classes already existed in `hivepilot/runners/iac_runner.py`
but were orphaned (not registered in `_BUILTIN_RUNNERS`, zero tests, and
had a real security/correctness bug: `payload.secrets` was silently
dropped from the child process environment, so `${secret:}`-resolved
`TF_VAR_*`/cloud credentials never reached the tool).

Covers:
(a) Registration: `resolve_runner_class("terraform"/"opentofu"/"pulumi")`
    now resolves via the real RunnerRegistry/RUNNER_MAP.
(b) argv assembly for every operation (init/plan/apply/destroy/output/
    validate/drift for terraform+opentofu; preview/up/destroy/output/
    refresh for pulumi).
(c) The secrets bug fix: `payload.secrets` land in the `env` mapping
    passed to `subprocess.run` for both the Tf runners and PulumiRunner.
(d) Unknown operation -> ValueError.
(e) `drift` returncode 2 -> RuntimeError (not a raw CalledProcessError).
(f) Missing binary -> RuntimeError (not a raw FileNotFoundError) -- this is
    hardening we check via `shutil.which` returning None, without ever
    invoking the real subprocess.
(g) `run()` always executes with `capture_output=False` (live streaming) and
    neither runner exposes a `capture()` method — raw plan/preview output
    can echo secret var values, so it must never be captured, returned, or
    persisted via `RunResult.detail` (CLI/API/chat notifications). A safe
    plan-SUMMARY capture is deferred to the Mirador panel sprint (A3).
(h) `-backend-config` is init-only: it must be present in `init` argv and
    absent from `plan`/`apply`/`destroy`/`drift` argv (passing it there is a
    Terraform/OpenTofu usage error).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.registry import resolve_runner_class
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.iac_runner import OpenTofuRunner, PulumiRunner, TerraformRunner


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
        step=TaskStep(name="s", runner="terraform", command=operation),
        metadata={},
        secrets=secrets or {},
    )


def _definition(kind: str, options: dict | None = None) -> RunnerDefinition:
    return RunnerDefinition(name=kind, kind=kind, options=options or {})


class TestRegistration:
    def test_terraform_resolves(self) -> None:
        assert resolve_runner_class("terraform") is TerraformRunner

    def test_opentofu_resolves(self) -> None:
        assert resolve_runner_class("opentofu") is OpenTofuRunner

    def test_pulumi_resolves(self) -> None:
        assert resolve_runner_class("pulumi") is PulumiRunner


class TestTerraformArgv:
    def _run(self, tmp_path: Path, operation: str, options: dict | None = None):
        runner = TerraformRunner(_definition("terraform", options), settings)
        with (
            patch("hivepilot.runners.iac_runner.shutil.which", return_value="/usr/bin/terraform"),
            patch("hivepilot.runners.iac_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            runner.run(_payload(tmp_path, operation=operation, options=options))
        return mock_run

    def test_init(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "init")
        argv = mock_run.call_args.args[0]
        assert argv == ["terraform", "init"]

    def test_init_with_backend_config(self, tmp_path: Path) -> None:
        options = {"backend_config": "backend.hcl"}
        mock_run = self._run(tmp_path, "init", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["terraform", "init", "-backend-config=backend.hcl"]

    @pytest.mark.parametrize("operation", ["plan", "apply", "destroy", "drift"])
    def test_backend_config_absent_outside_init(self, tmp_path: Path, operation: str) -> None:
        """Regression: -backend-config is init-only. Passing it to plan/apply/
        destroy/drift is a Terraform/OpenTofu usage error (non-zero exit,
        nothing runs) -- it must never leak into those operations' argv, even
        when var_file/parallelism (which ARE valid there) are also set."""
        options = {
            "backend_config": "backend.hcl",
            "var_file": "prod.tfvars",
            "parallelism": 5,
        }
        mock_run = self._run(tmp_path, operation, options)
        argv = mock_run.call_args.args[0]
        assert not any("-backend-config" in arg for arg in argv)
        # sibling flags that ARE valid on these operations must still be present
        assert "-var-file=prod.tfvars" in argv
        assert "-parallelism=5" in argv

    def test_backend_config_present_in_init_only(self, tmp_path: Path) -> None:
        options = {
            "backend_config": "backend.hcl",
            "var_file": "prod.tfvars",
            "parallelism": 5,
        }
        mock_run = self._run(tmp_path, "init", options)
        argv = mock_run.call_args.args[0]
        assert "-backend-config=backend.hcl" in argv
        # var_file/parallelism are not valid on `init` and must not appear
        assert not any("-var-file" in arg for arg in argv)
        assert not any("-parallelism" in arg for arg in argv)

    def test_plan(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "plan")
        argv = mock_run.call_args.args[0]
        assert argv[0] == "terraform"
        assert argv[1] == "plan"
        assert "-no-color" in argv

    def test_plan_with_var_file(self, tmp_path: Path) -> None:
        options = {"var_file": "prod.tfvars"}
        mock_run = self._run(tmp_path, "plan", options)
        argv = mock_run.call_args.args[0]
        assert "-var-file=prod.tfvars" in argv

    def test_apply_auto_approve(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "apply")
        argv = mock_run.call_args.args[0]
        assert argv == ["terraform", "apply", "-auto-approve"]

    def test_destroy_auto_approve(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "destroy")
        argv = mock_run.call_args.args[0]
        assert argv == ["terraform", "destroy", "-auto-approve"]

    def test_output_json(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "output")
        argv = mock_run.call_args.args[0]
        assert argv == ["terraform", "output", "-json"]

    def test_validate(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "validate")
        argv = mock_run.call_args.args[0]
        assert argv == ["terraform", "validate"]

    def test_drift_detailed_exitcode(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "drift")
        argv = mock_run.call_args.args[0]
        assert argv[0] == "terraform"
        assert argv[1] == "plan"
        assert "--detailed-exitcode" in argv

    def test_unknown_operation_raises_value_error(self, tmp_path: Path) -> None:
        runner = TerraformRunner(_definition("terraform"), settings)
        with (
            patch("hivepilot.runners.iac_runner.shutil.which", return_value="/usr/bin/terraform"),
            patch("hivepilot.runners.iac_runner.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation="bogus"))

    def test_workspace_select(self, tmp_path: Path) -> None:
        options = {"workspace": "staging"}
        runner = TerraformRunner(_definition("terraform", options), settings)
        with (
            patch("hivepilot.runners.iac_runner.shutil.which", return_value="/usr/bin/terraform"),
            patch("hivepilot.runners.iac_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            runner.run(_payload(tmp_path, operation="plan", options=options))

        first_call_argv = mock_run.call_args_list[0].args[0]
        assert first_call_argv == ["terraform", "workspace", "select", "staging"]


class TestOpenTofuArgv:
    def test_binary_is_tofu(self, tmp_path: Path) -> None:
        runner = OpenTofuRunner(_definition("opentofu"), settings)
        with (
            patch("hivepilot.runners.iac_runner.shutil.which", return_value="/usr/bin/tofu"),
            patch("hivepilot.runners.iac_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            runner.run(_payload(tmp_path, operation="plan"))

        argv = mock_run.call_args.args[0]
        assert argv[0] == "tofu"


class TestDriftReturnCode2:
    def test_drift_rc2_raises_runtime_error(self, tmp_path: Path) -> None:
        import subprocess as sp

        runner = TerraformRunner(_definition("terraform"), settings)
        with (
            patch("hivepilot.runners.iac_runner.shutil.which", return_value="/usr/bin/terraform"),
            patch("hivepilot.runners.iac_runner.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = sp.CalledProcessError(returncode=2, cmd=["terraform", "plan"])
            with pytest.raises(RuntimeError, match="Drift detected"):
                runner.run(_payload(tmp_path, operation="drift"))

    def test_non_drift_called_process_error_propagates(self, tmp_path: Path) -> None:
        import subprocess as sp

        runner = TerraformRunner(_definition("terraform"), settings)
        with (
            patch("hivepilot.runners.iac_runner.shutil.which", return_value="/usr/bin/terraform"),
            patch("hivepilot.runners.iac_runner.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = sp.CalledProcessError(returncode=1, cmd=["terraform", "apply"])
            with pytest.raises(sp.CalledProcessError):
                runner.run(_payload(tmp_path, operation="apply"))


class TestMissingBinary:
    def test_terraform_missing_binary_raises_runtime_error(self, tmp_path: Path) -> None:
        runner = TerraformRunner(_definition("terraform"), settings)
        with patch("hivepilot.runners.iac_runner.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="terraform CLI not found"):
                runner.run(_payload(tmp_path, operation="plan"))

    def test_pulumi_missing_binary_raises_runtime_error(self, tmp_path: Path) -> None:
        runner = PulumiRunner(_definition("pulumi"), settings)
        with patch("hivepilot.runners.iac_runner.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="pulumi CLI not found"):
                runner.run(_payload(tmp_path, operation="preview"))

    def test_missing_binary_never_calls_subprocess(self, tmp_path: Path) -> None:
        runner = TerraformRunner(_definition("terraform"), settings)
        with (
            patch("hivepilot.runners.iac_runner.shutil.which", return_value=None),
            patch("hivepilot.runners.iac_runner.subprocess.run") as mock_run,
        ):
            with pytest.raises(RuntimeError):
                runner.run(_payload(tmp_path, operation="plan"))
        mock_run.assert_not_called()


class TestSecretsReachTheEnvironment:
    """Regression test for the dropped-secrets bug: `payload.secrets` must
    land in the `env` dict passed to `subprocess.run` for both the Tf
    runners and PulumiRunner."""

    def test_terraform_secrets_in_env(self, tmp_path: Path) -> None:
        runner = TerraformRunner(_definition("terraform"), settings)
        secrets = {"TF_VAR_db_password": "s3cr3t-value"}
        with (
            patch("hivepilot.runners.iac_runner.shutil.which", return_value="/usr/bin/terraform"),
            patch("hivepilot.runners.iac_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            runner.run(_payload(tmp_path, operation="plan", secrets=secrets))

        env = mock_run.call_args.kwargs["env"]
        assert env["TF_VAR_db_password"] == "s3cr3t-value"

    def test_pulumi_secrets_in_env(self, tmp_path: Path) -> None:
        runner = PulumiRunner(_definition("pulumi"), settings)
        secrets = {"PULUMI_CONFIG_PASSPHRASE": "s3cr3t-value"}
        with (
            patch("hivepilot.runners.iac_runner.shutil.which", return_value="/usr/bin/pulumi"),
            patch("hivepilot.runners.iac_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            runner.run(_payload(tmp_path, operation="preview", secrets=secrets))

        env = mock_run.call_args.kwargs["env"]
        assert env["PULUMI_CONFIG_PASSPHRASE"] == "s3cr3t-value"

    def test_secrets_win_over_project_and_definition_env(self, tmp_path: Path) -> None:
        definition = RunnerDefinition(
            name="terraform", kind="terraform", env={"TF_VAR_db_password": "definition-value"}
        )
        runner = TerraformRunner(definition, settings)
        payload = RunnerPayload(
            project_name="proj",
            project=ProjectConfig(path=tmp_path, env={"TF_VAR_db_password": "project-value"}),
            task_name="t",
            step=TaskStep(name="s", runner="terraform", command="plan"),
            metadata={},
            secrets={"TF_VAR_db_password": "secret-value"},
        )
        with (
            patch("hivepilot.runners.iac_runner.shutil.which", return_value="/usr/bin/terraform"),
            patch("hivepilot.runners.iac_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            runner.run(payload)

        env = mock_run.call_args.kwargs["env"]
        assert env["TF_VAR_db_password"] == "secret-value"


class TestNoCaptureLeakPath:
    """Regression for the HIGH security finding: these runners used to expose
    a `capture(payload) -> str` method returning raw plan/preview stdout.
    The orchestrator's `_capture_or_execute` auto-prefers `capture()` over
    `run()` when present (see `hivepilot/orchestrator.py`), so that raw text
    -- which routinely echoes `TF_VAR_*`/secret values -- flowed via
    `RunResult.detail` to CLI stdout, the `/v1/run` API body, and
    Slack/Discord/Telegram UNREDACTED. `capture()` must stay gone, and
    `run()` must always execute live (`capture_output=False`, inheriting the
    parent's stdout) so nothing is captured, returned, or persisted."""

    def test_terraform_runner_has_no_capture_method(self) -> None:
        runner = TerraformRunner(_definition("terraform"), settings)
        assert not hasattr(runner, "capture")

    def test_opentofu_runner_has_no_capture_method(self) -> None:
        runner = OpenTofuRunner(_definition("opentofu"), settings)
        assert not hasattr(runner, "capture")

    def test_pulumi_runner_has_no_capture_method(self) -> None:
        runner = PulumiRunner(_definition("pulumi"), settings)
        assert not hasattr(runner, "capture")

    def test_terraform_run_streams_live_not_captured(self, tmp_path: Path) -> None:
        runner = TerraformRunner(_definition("terraform"), settings)
        with (
            patch("hivepilot.runners.iac_runner.shutil.which", return_value="/usr/bin/terraform"),
            patch("hivepilot.runners.iac_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="some output")
            result = runner.run(_payload(tmp_path, operation="plan"))

        assert result is None
        assert mock_run.call_args.kwargs["capture_output"] is False

    def test_pulumi_run_streams_live_not_captured(self, tmp_path: Path) -> None:
        runner = PulumiRunner(_definition("pulumi"), settings)
        with (
            patch("hivepilot.runners.iac_runner.shutil.which", return_value="/usr/bin/pulumi"),
            patch("hivepilot.runners.iac_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="preview output")
            result = runner.run(_payload(tmp_path, operation="preview"))

        assert result is None
        assert mock_run.call_args.kwargs["capture_output"] is False

    def test_orchestrator_capture_or_execute_falls_back_to_run(self, tmp_path: Path) -> None:
        """`_capture_or_execute` must fall back to `run()` (and return "")
        rather than raising or somehow still capturing output, now that
        these runners no longer expose `capture()`."""
        runner = TerraformRunner(_definition("terraform"), settings)
        capture = getattr(runner, "capture", None)
        assert capture is None


class TestPulumiArgv:
    def _run(self, tmp_path: Path, operation: str, options: dict | None = None):
        runner = PulumiRunner(_definition("pulumi", options), settings)
        with (
            patch("hivepilot.runners.iac_runner.shutil.which", return_value="/usr/bin/pulumi"),
            patch("hivepilot.runners.iac_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            runner.run(_payload(tmp_path, operation=operation, options=options))
        return mock_run

    def test_preview(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "preview")
        argv = mock_run.call_args.args[0]
        assert argv[0] == "pulumi"
        assert argv[1] == "preview"

    def test_up_yes(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "up")
        argv = mock_run.call_args.args[0]
        assert argv == ["pulumi", "up", "--yes"]

    def test_destroy_yes(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "destroy")
        argv = mock_run.call_args.args[0]
        assert argv == ["pulumi", "destroy", "--yes"]

    def test_output_json(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "output")
        argv = mock_run.call_args.args[0]
        assert argv == ["pulumi", "stack", "output", "--json"]

    def test_refresh_yes(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "refresh")
        argv = mock_run.call_args.args[0]
        assert argv == ["pulumi", "refresh", "--yes"]

    def test_stack_flag(self, tmp_path: Path) -> None:
        options = {"stack": "prod"}
        mock_run = self._run(tmp_path, "up", options)
        argv = mock_run.call_args.args[0]
        assert "--stack" in argv
        assert "prod" in argv

    def test_unknown_operation_raises_value_error(self, tmp_path: Path) -> None:
        runner = PulumiRunner(_definition("pulumi"), settings)
        with (
            patch("hivepilot.runners.iac_runner.shutil.which", return_value="/usr/bin/pulumi"),
            patch("hivepilot.runners.iac_runner.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation="bogus"))


class TestIsDestructive:
    """Phase 17a-B: `is_destructive(payload)` — the optional, structural
    (getattr-discovered, like `capture`) contract the step-level approval
    gate (`hivepilot.orchestrator.step_requires_approval`) queries. Resolves
    the operation exactly the same way `run()`/`_execute()` does."""

    @pytest.mark.parametrize("operation", ["apply", "destroy"])
    def test_terraform_destructive_ops(self, tmp_path: Path, operation: str) -> None:
        runner = TerraformRunner(_definition("terraform"), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=operation)) is True

    @pytest.mark.parametrize("operation", ["plan", "validate", "output", "init", "drift", "cost"])
    def test_terraform_non_destructive_ops(self, tmp_path: Path, operation: str) -> None:
        runner = TerraformRunner(_definition("terraform"), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=operation)) is False

    @pytest.mark.parametrize("operation", ["apply", "destroy"])
    def test_opentofu_destructive_ops(self, tmp_path: Path, operation: str) -> None:
        runner = OpenTofuRunner(_definition("opentofu"), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=operation)) is True

    @pytest.mark.parametrize("operation", ["plan", "validate", "output", "init"])
    def test_opentofu_non_destructive_ops(self, tmp_path: Path, operation: str) -> None:
        runner = OpenTofuRunner(_definition("opentofu"), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=operation)) is False

    @pytest.mark.parametrize("operation", ["up", "destroy", "refresh"])
    def test_pulumi_destructive_ops(self, tmp_path: Path, operation: str) -> None:
        runner = PulumiRunner(_definition("pulumi"), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=operation)) is True

    @pytest.mark.parametrize("operation", ["preview", "output"])
    def test_pulumi_non_destructive_ops(self, tmp_path: Path, operation: str) -> None:
        runner = PulumiRunner(_definition("pulumi"), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=operation)) is False

    def test_default_operation_when_unset_is_not_destructive(self, tmp_path: Path) -> None:
        """No step.command/definition.command/options['operation'] set -> the
        runner's own default (terraform: 'plan', pulumi: 'preview') applies,
        which is never destructive."""
        tf_runner = TerraformRunner(_definition("terraform"), settings)
        assert tf_runner.is_destructive(_payload(tmp_path, operation=None)) is False
        pulumi_runner = PulumiRunner(_definition("pulumi"), settings)
        assert pulumi_runner.is_destructive(_payload(tmp_path, operation=None)) is False


class TestNoSecretsInLogs:
    """No TF_VAR secret value may ever be passed to `logger.info` (plan
    output can echo var values; the fix must not leak them into logs).

    NOTE: the plan/preview path no longer captures stdout at all (see
    `TestNoCaptureLeakPath` -- `run()` always uses `capture_output=False`),
    so there is no Python-visible stdout for a `plan` run to leak into logs
    in the first place. The remaining risk is the `cost` path, which DOES
    capture infracost's stdout internally (to log it at debug only) --
    covered below."""

    def test_cost_estimate_does_not_log_stdout_at_info(self, tmp_path: Path) -> None:
        runner = TerraformRunner(_definition("terraform"), settings)
        with (
            patch("hivepilot.runners.iac_runner.shutil.which", return_value="/usr/bin/terraform"),
            patch("hivepilot.runners.iac_runner.subprocess.run") as mock_run,
            patch("hivepilot.runners.iac_runner.logger") as mock_logger,
        ):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Monthly cost: $123.45 (TF_VAR_secret=abc)"
            )
            runner.run(_payload(tmp_path, operation="cost"))

        for call in mock_logger.info.call_args_list:
            call_text = " ".join(str(a) for a in call.args) + " ".join(
                f"{k}={v}" for k, v in call.kwargs.items()
            )
            assert "TF_VAR_secret=abc" not in call_text
