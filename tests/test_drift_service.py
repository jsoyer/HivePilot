"""
Tests for `hivepilot.services.drift_service` (Phase 20 Sprint D1 — drift_service
core). `detect_drift` runs the IaC drift operation (`tofu`/`terraform plan
--detailed-exitcode -no-color`) exactly the way `hivepilot.runners.iac_runner`
would, but with `capture_output=True` so the plan summary counts can be parsed,
and returns a structured `DriftResult` — never the raw plan stdout/stderr.

Every subprocess call is mocked via `subprocess.run` (monkeypatched) — no real
tofu/terraform binary is ever invoked. Assertions cover:

1. rc == 0 -> no drift, summary is (0, 0, 0).
2. rc == 2 + a "Plan: N to add, N to change, N to destroy" line -> drifted
   True with the parsed summary.
3. rc == 2 WITHOUT a recognizable summary line -> drifted True, summary None
   (never falls back to raw stdout).
4. rc == 1 (and other unexpected codes) -> RuntimeError whose message
   contains NEITHER stdout NOR stderr content.
5. Missing binary (`shutil.which` returns None) -> RuntimeError, subprocess
   never invoked.
6. `subprocess.TimeoutExpired` -> RuntimeError.
7. `runner_kind="pulumi"` -> ValueError (no drift op exists for Pulumi).
8. The argv passed to `subprocess.run` for opentofu/terraform matches
   `iac_runner`'s drift argv exactly (`[binary, "plan", "--detailed-exitcode",
   "-no-color"]`) — this is a structural guarantee because `detect_drift`
   calls the runner's own private `_build_command` helper rather than
   re-implementing argv assembly.
9. Anti-leak guarantee: a secret-looking value embedded in a fake rc2 plan
   stdout NEVER appears in `repr(result)` for the success path, nor in the
   raised exception message for the error path.
10. `payload.secrets` passed to `detect_drift` land in the subprocess `env`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hivepilot.models import ProjectConfig
from hivepilot.services import drift_service
from hivepilot.services.drift_service import DriftResult, DriftSummary, detect_drift

# A secret-looking token that must never appear in the returned DriftResult
# or any raised exception, even though it's embedded in the raw (mocked)
# plan stdout — this is the core anti-leak guarantee the sprint exists for.
_LEAKED_LOOKING_TOKEN = "sk-live-should-never-leak-0123456789"  # noqa: S105


def _fake_completed_process(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _project(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(path=tmp_path)


class TestNoDrift:
    def test_rc0_no_drift(self, tmp_path: Path) -> None:
        with (
            patch.object(drift_service.shutil, "which", return_value="/usr/bin/tofu"),
            patch.object(drift_service.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = _fake_completed_process(stdout="No changes.", returncode=0)
            result = detect_drift(_project(tmp_path), runner_kind="opentofu")

        assert result.drifted is False
        assert result.summary == DriftSummary(to_add=0, to_change=0, to_destroy=0)
        assert result.error is None
        assert result.runner == "opentofu"


class TestDriftDetected:
    def test_rc2_with_summary_line(self, tmp_path: Path) -> None:
        with (
            patch.object(drift_service.shutil, "which", return_value="/usr/bin/tofu"),
            patch.object(drift_service.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = _fake_completed_process(
                stdout="Plan: 1 to add, 2 to change, 3 to destroy.", returncode=2
            )
            result = detect_drift(_project(tmp_path), runner_kind="opentofu")

        assert result.drifted is True
        assert result.summary == DriftSummary(to_add=1, to_change=2, to_destroy=3)

    def test_rc2_without_summary_line_leaves_summary_none(self, tmp_path: Path) -> None:
        """rc2 but the stdout doesn't contain a parseable summary line -- must
        NEVER fall back to raw stdout; summary stays None."""
        with (
            patch.object(drift_service.shutil, "which", return_value="/usr/bin/tofu"),
            patch.object(drift_service.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = _fake_completed_process(
                stdout="some unexpected plan output with no recognizable summary",
                returncode=2,
            )
            result = detect_drift(_project(tmp_path), runner_kind="opentofu")

        assert result.drifted is True
        assert result.summary is None

    def test_rc2_secret_in_stdout_never_leaks(self, tmp_path: Path) -> None:
        with (
            patch.object(drift_service.shutil, "which", return_value="/usr/bin/tofu"),
            patch.object(drift_service.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = _fake_completed_process(
                stdout=(
                    f"resource echoes {_LEAKED_LOOKING_TOKEN}\n"
                    "Plan: 1 to add, 0 to change, 0 to destroy."
                ),
                returncode=2,
            )
            result = detect_drift(_project(tmp_path), runner_kind="opentofu")

        assert result.drifted is True
        assert result.summary == DriftSummary(to_add=1, to_change=0, to_destroy=0)
        assert _LEAKED_LOOKING_TOKEN not in repr(result)


class TestUnexpectedExitCode:
    def test_rc1_raises_runtime_error_without_leaking_output(self, tmp_path: Path) -> None:
        with (
            patch.object(drift_service.shutil, "which", return_value="/usr/bin/tofu"),
            patch.object(drift_service.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = _fake_completed_process(
                stdout=f"boom {_LEAKED_LOOKING_TOKEN}",
                stderr=f"stderr leak {_LEAKED_LOOKING_TOKEN}",
                returncode=1,
            )
            with pytest.raises(RuntimeError) as excinfo:
                detect_drift(_project(tmp_path), runner_kind="opentofu")

        message = str(excinfo.value)
        assert _LEAKED_LOOKING_TOKEN not in message
        assert "boom" not in message
        assert "stderr leak" not in message
        assert "opentofu" in message or "tofu" in message

    def test_other_unexpected_returncode_raises(self, tmp_path: Path) -> None:
        with (
            patch.object(drift_service.shutil, "which", return_value="/usr/bin/terraform"),
            patch.object(drift_service.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = _fake_completed_process(returncode=3)
            with pytest.raises(RuntimeError):
                detect_drift(_project(tmp_path), runner_kind="terraform")


class TestMissingBinary:
    def test_missing_binary_raises_runtime_error(self, tmp_path: Path) -> None:
        with (
            patch.object(drift_service.shutil, "which", return_value=None),
            patch.object(drift_service.subprocess, "run") as mock_run,
        ):
            with pytest.raises(RuntimeError):
                detect_drift(_project(tmp_path), runner_kind="opentofu")
        mock_run.assert_not_called()


class TestTimeout:
    def test_timeout_raises_runtime_error(self, tmp_path: Path) -> None:
        with (
            patch.object(drift_service.shutil, "which", return_value="/usr/bin/tofu"),
            patch.object(drift_service.subprocess, "run") as mock_run,
        ):
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["tofu"], timeout=5)
            with pytest.raises(RuntimeError):
                detect_drift(_project(tmp_path), runner_kind="opentofu", timeout=5)


class TestPulumiUnsupported:
    def test_pulumi_raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            detect_drift(_project(tmp_path), runner_kind="pulumi")


class TestArgvParity:
    def test_opentofu_argv_matches_iac_runner(self, tmp_path: Path) -> None:
        with (
            patch.object(drift_service.shutil, "which", return_value="/usr/bin/tofu"),
            patch.object(drift_service.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = _fake_completed_process(returncode=0)
            detect_drift(_project(tmp_path), runner_kind="opentofu")

        argv = mock_run.call_args.args[0]
        assert argv == ["tofu", "plan", "--detailed-exitcode", "-no-color"]

    def test_terraform_argv_matches_iac_runner(self, tmp_path: Path) -> None:
        with (
            patch.object(drift_service.shutil, "which", return_value="/usr/bin/terraform"),
            patch.object(drift_service.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = _fake_completed_process(returncode=0)
            detect_drift(_project(tmp_path), runner_kind="terraform")

        argv = mock_run.call_args.args[0]
        assert argv == ["terraform", "plan", "--detailed-exitcode", "-no-color"]


class TestSecretsInEnv:
    def test_secrets_land_in_subprocess_env(self, tmp_path: Path) -> None:
        with (
            patch.object(drift_service.shutil, "which", return_value="/usr/bin/tofu"),
            patch.object(drift_service.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = _fake_completed_process(returncode=0)
            detect_drift(
                _project(tmp_path),
                runner_kind="opentofu",
                secrets={"TF_VAR_db_password": "s3cr3t"},
            )

        env = mock_run.call_args.kwargs["env"]
        assert env["TF_VAR_db_password"] == "s3cr3t"


class TestCwd:
    def test_cwd_is_project_path(self, tmp_path: Path) -> None:
        with (
            patch.object(drift_service.shutil, "which", return_value="/usr/bin/tofu"),
            patch.object(drift_service.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = _fake_completed_process(returncode=0)
            detect_drift(_project(tmp_path), runner_kind="opentofu")

        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path)


class TestDataclasses:
    def test_drift_result_is_frozen(self, tmp_path: Path) -> None:
        result = DriftResult(project="p", runner="opentofu", drifted=False)
        with pytest.raises(Exception):
            result.drifted = True  # type: ignore[misc]

    def test_drift_summary_is_frozen(self) -> None:
        summary = DriftSummary(to_add=0, to_change=0, to_destroy=0)
        with pytest.raises(Exception):
            summary.to_add = 1  # type: ignore[misc]


# Sanity: nothing above should have needed real shutil/subprocess module access
# outside the patched context — guard against accidental unmocked import-time
# side effects.
def test_module_imports_cleanly() -> None:
    assert hasattr(drift_service, "detect_drift")
    assert hasattr(drift_service, "DriftResult")
    assert hasattr(drift_service, "DriftSummary")
