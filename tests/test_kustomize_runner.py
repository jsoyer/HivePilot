"""
Tests for the kustomize runner (Phase 17c Sprint 2): a builtin runner so
HivePilot agents can render/manage kustomize overlays, with mutating
``edit-*`` operations auto-gating via the step-level approval gate
(``hivepilot.orchestrator.step_requires_approval``).

Covers, mirroring ``tests/test_helm_runner.py``'s pattern:
(a) Registration: `resolve_runner_class("kustomize")` resolves via the real
    RunnerRegistry/RUNNER_MAP, and "kustomize" is advertised in
    KNOWN_RUNNER_KINDS.
(b) argv assembly for every operation (build, edit-set-image,
    edit-set-namespace), including `--enable-helm` on build.
(c) `dir` reaches `cwd` passed to `subprocess.run` (never argv) — kustomize
    `edit` subcommands operate on the kustomization.yaml in the current
    directory, so a non-default `dir` must actually chdir there.
(d) `payload.secrets` land in the env passed to `subprocess.run`.
(e) Unknown operation -> ValueError.
(f) Missing required option (image/namespace) -> clear ValueError.
(g) Missing `kustomize` binary -> RuntimeError (subprocess NOT called).
(h) `is_destructive`: edit-set-image/edit-set-namespace -> True; build ->
    False. Default operation (unset) is `build`, which is not destructive.
(i) No `capture()` method exposed; `run()` always streams live
    (`capture_output=False`).
(j) Gate<->argv agreement test: is_destructive() and the actually-executed
    argv can never disagree about whether an operation mutates the repo's
    kustomization.yaml.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import KNOWN_RUNNER_KINDS, ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.registry import RUNNER_MAP, resolve_runner_class
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.kustomize_runner import KustomizeRunner

_MODULE = "hivepilot.runners.kustomize_runner"


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
        step=TaskStep(name="s", runner="kustomize", command=operation),
        metadata={},
        secrets=secrets or {},
    )


def _definition(options: dict | None = None, env: dict | None = None) -> RunnerDefinition:
    return RunnerDefinition(
        name="kustomize", kind="kustomize", options=options or {}, env=env or {}
    )


class TestRegistration:
    def test_kustomize_resolves(self) -> None:
        assert resolve_runner_class("kustomize") is KustomizeRunner

    def test_kustomize_in_known_runner_kinds(self) -> None:
        assert "kustomize" in KNOWN_RUNNER_KINDS

    def test_kustomize_registered_in_runner_map(self) -> None:
        assert RUNNER_MAP["kustomize"] is KustomizeRunner


class TestArgv:
    def _run(self, tmp_path: Path, operation: str | None, options: dict | None = None):
        runner = KustomizeRunner(_definition(options), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/kustomize"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation=operation))
        return mock_run

    def test_build_default_operation(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, None, {})
        argv = mock_run.call_args.args[0]
        assert argv == ["kustomize", "build", "."]

    def test_build_explicit(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "build", {})
        argv = mock_run.call_args.args[0]
        assert argv == ["kustomize", "build", "."]

    def test_build_with_enable_helm(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "build", {"enable_helm": True})
        argv = mock_run.call_args.args[0]
        assert argv == ["kustomize", "build", ".", "--enable-helm"]

    def test_edit_set_image(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "edit-set-image", {"image": "nginx=nginx:1.21"})
        argv = mock_run.call_args.args[0]
        assert argv == ["kustomize", "edit", "set", "image", "nginx=nginx:1.21"]

    def test_edit_set_namespace(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "edit-set-namespace", {"namespace": "prod"})
        argv = mock_run.call_args.args[0]
        assert argv == ["kustomize", "edit", "set", "namespace", "prod"]

    def test_unknown_operation_raises_value_error(self, tmp_path: Path) -> None:
        runner = KustomizeRunner(_definition(), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/kustomize"),
            patch(f"{_MODULE}.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation="bogus"))


class TestMissingRequiredOptions:
    @pytest.mark.parametrize(
        "operation,options",
        [
            ("edit-set-image", {}),
            ("edit-set-namespace", {}),
        ],
    )
    def test_missing_required_option_raises_value_error(
        self, tmp_path: Path, operation: str, options: dict
    ) -> None:
        runner = KustomizeRunner(_definition(options), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/kustomize"),
            patch(f"{_MODULE}.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation=operation))


class TestDirInCwd:
    def test_default_dir_uses_project_path(self, tmp_path: Path) -> None:
        runner = KustomizeRunner(_definition({}), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/kustomize"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="build"))
        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path)

    def test_custom_dir_reaches_cwd_for_build(self, tmp_path: Path) -> None:
        overlay = tmp_path / "overlays" / "prod"
        overlay.mkdir(parents=True)
        runner = KustomizeRunner(_definition({"dir": "overlays/prod"}), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/kustomize"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="build"))
        assert mock_run.call_args.kwargs["cwd"] == str(overlay)
        argv = mock_run.call_args.args[0]
        assert argv == ["kustomize", "build", "."]

    def test_custom_dir_reaches_cwd_for_edit(self, tmp_path: Path) -> None:
        overlay = tmp_path / "overlays" / "prod"
        overlay.mkdir(parents=True)
        runner = KustomizeRunner(
            _definition({"dir": "overlays/prod", "namespace": "prod"}), settings
        )
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/kustomize"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="edit-set-namespace"))
        assert mock_run.call_args.kwargs["cwd"] == str(overlay)
        argv = mock_run.call_args.args[0]
        assert "overlays/prod" not in argv


class TestMissingBinary:
    def test_missing_binary_raises_runtime_error(self, tmp_path: Path) -> None:
        runner = KustomizeRunner(_definition({}), settings)
        with patch(f"{_MODULE}.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="kustomize CLI not found"):
                runner.run(_payload(tmp_path, operation="build"))

    def test_missing_binary_never_calls_subprocess(self, tmp_path: Path) -> None:
        runner = KustomizeRunner(_definition({}), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value=None),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            with pytest.raises(RuntimeError):
                runner.run(_payload(tmp_path, operation="build"))
        mock_run.assert_not_called()


class TestSecretsReachTheEnvironment:
    def test_secrets_in_env(self, tmp_path: Path) -> None:
        runner = KustomizeRunner(_definition({}), settings)
        secrets = {"KUSTOMIZE_TOKEN": "s3cr3t-value"}
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/kustomize"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="build", secrets=secrets))

        env = mock_run.call_args.kwargs["env"]
        assert env["KUSTOMIZE_TOKEN"] == "s3cr3t-value"

    def test_secrets_win_over_project_and_definition_env(self, tmp_path: Path) -> None:
        definition = _definition({}, env={"KUSTOMIZE_TOKEN": "definition-value"})
        runner = KustomizeRunner(definition, settings)
        payload = RunnerPayload(
            project_name="proj",
            project=ProjectConfig(path=tmp_path, env={"KUSTOMIZE_TOKEN": "project-value"}),
            task_name="t",
            step=TaskStep(name="s", runner="kustomize", command="build"),
            metadata={},
            secrets={"KUSTOMIZE_TOKEN": "secret-value"},
        )
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/kustomize"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(payload)

        env = mock_run.call_args.kwargs["env"]
        assert env["KUSTOMIZE_TOKEN"] == "secret-value"


class TestIsDestructive:
    @pytest.mark.parametrize("operation", ["edit-set-image", "edit-set-namespace"])
    def test_destructive_ops(self, tmp_path: Path, operation: str) -> None:
        options = {"image": "nginx=nginx:1.21", "namespace": "prod"}
        runner = KustomizeRunner(_definition(options), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=operation)) is True

    def test_build_not_destructive(self, tmp_path: Path) -> None:
        runner = KustomizeRunner(_definition({}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="build")) is False

    def test_default_operation_when_unset_is_not_destructive(self, tmp_path: Path) -> None:
        runner = KustomizeRunner(_definition({}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=None)) is False


class TestNoCaptureLeakPath:
    def test_runner_has_no_capture_method(self) -> None:
        runner = KustomizeRunner(_definition(), settings)
        assert not hasattr(runner, "capture")

    def test_run_streams_live_not_captured(self, tmp_path: Path) -> None:
        runner = KustomizeRunner(_definition({}), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/kustomize"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="build"))

        assert mock_run.call_args.kwargs["capture_output"] is False


class TestCaseNormalization:
    def test_case_variant_operation_normalizes_and_works(self, tmp_path: Path) -> None:
        runner = KustomizeRunner(_definition({"namespace": "prod"}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="Edit-Set-Namespace")) is True
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/kustomize"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="Edit-Set-Namespace"))
        argv = mock_run.call_args.args[0]
        assert argv == ["kustomize", "edit", "set", "namespace", "prod"]


class TestGateArgvAgreement:
    """`is_destructive()` and `_build_command()` both resolve the operation
    from the SAME `options` dict via the same helper, so they can never
    disagree about whether an operation mutates the repo's
    kustomization.yaml. For options that make `_build_command` raise
    (missing required option, unknown operation), that's fine — fail-closed
    execution is a valid way to never disagree with a False gate."""

    _MUTATING_OPS = frozenset({"edit-set-image", "edit-set-namespace"})

    def _argv_is_mutating(self, argv: list[str]) -> bool:
        return len(argv) >= 3 and argv[1] == "edit" and argv[2] == "set"

    @pytest.mark.parametrize(
        "operation,options,expect_raises",
        [
            ("build", {}, False),
            ("build", {"enable_helm": True}, False),
            ("edit-set-image", {"image": "nginx=nginx:1.21"}, False),
            ("edit-set-image", {}, True),
            ("edit-set-namespace", {"namespace": "prod"}, False),
            ("edit-set-namespace", {}, True),
        ],
    )
    def test_gate_agrees_with_executed_argv(
        self, tmp_path: Path, operation: str, options: dict, expect_raises: bool
    ) -> None:
        runner = KustomizeRunner(_definition(options), settings)
        gate_result = runner.is_destructive(_payload(tmp_path, operation=operation))

        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/kustomize"),
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
