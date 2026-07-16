"""
Tests for the Helm runner (Phase 17c): a builtin runner so HivePilot agents
can manage Helm releases, with mutating operations (``install``/``upgrade``/
``rollback``/``uninstall``) auto-gating via the step-level approval gate
(``hivepilot.orchestrator.step_requires_approval``).

Covers, mirroring ``tests/test_kubectl_runner.py``'s pattern:
(a) Registration: `resolve_runner_class("helm")` resolves via the real
    RunnerRegistry/RUNNER_MAP, and "helm" is advertised in
    KNOWN_RUNNER_KINDS (the RUNNER_MAP-kinds-are-known invariant test).
(b) argv assembly for every operation (install, upgrade, rollback,
    uninstall, template, lint, list, status) with values/version/namespace/
    context appended when set.
(c) `KUBECONFIG` reaches the env passed to `subprocess.run` when
    `options.kubeconfig` is set (env, never argv).
(d) `payload.secrets` land in the env passed to `subprocess.run`.
(e) Unknown operation -> ValueError.
(f) Missing required option (release/chart) -> clear ValueError.
(g) Missing `helm` binary -> RuntimeError (subprocess NOT called).
(h) `is_destructive`: install/upgrade/rollback/uninstall -> True;
    template/lint/list/status -> False.
(i) No `capture()` method exposed; `run()` always streams live
    (`capture_output=False`).
(j) Gate<->argv agreement test: is_destructive() and the actually-executed
    argv can never disagree about whether an operation mutates the cluster.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import KNOWN_RUNNER_KINDS, ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.registry import RUNNER_MAP, resolve_runner_class
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.helm_runner import HelmRunner

_MODULE = "hivepilot.runners.helm_runner"


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
        step=TaskStep(name="s", runner="helm", command=operation),
        metadata={},
        secrets=secrets or {},
    )


def _definition(options: dict | None = None, env: dict | None = None) -> RunnerDefinition:
    return RunnerDefinition(name="helm", kind="helm", options=options or {}, env=env or {})


class TestRegistration:
    def test_helm_resolves(self) -> None:
        assert resolve_runner_class("helm") is HelmRunner

    def test_helm_in_known_runner_kinds(self) -> None:
        assert "helm" in KNOWN_RUNNER_KINDS

    def test_helm_registered_in_runner_map(self) -> None:
        assert RUNNER_MAP["helm"] is HelmRunner


class TestArgv:
    def _run(self, tmp_path: Path, operation: str, options: dict | None = None):
        runner = HelmRunner(_definition(options), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/helm"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation=operation))
        return mock_run

    def test_install(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "install", {"release": "myapp", "chart": "./chart"})
        argv = mock_run.call_args.args[0]
        assert argv == ["helm", "install", "myapp", "./chart"]

    def test_install_with_values_version_namespace(self, tmp_path: Path) -> None:
        options = {
            "release": "myapp",
            "chart": "./chart",
            "values": ["values.yaml", "values-prod.yaml"],
            "version": "1.2.3",
            "namespace": "prod",
        }
        mock_run = self._run(tmp_path, "install", options)
        argv = mock_run.call_args.args[0]
        assert argv == [
            "helm",
            "install",
            "myapp",
            "./chart",
            "-f",
            "values.yaml",
            "-f",
            "values-prod.yaml",
            "--version",
            "1.2.3",
            "-n",
            "prod",
        ]

    def test_install_with_single_values_string(self, tmp_path: Path) -> None:
        options = {"release": "myapp", "chart": "./chart", "values": "values.yaml"}
        mock_run = self._run(tmp_path, "install", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["helm", "install", "myapp", "./chart", "-f", "values.yaml"]

    def test_upgrade(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "upgrade", {"release": "myapp", "chart": "./chart"})
        argv = mock_run.call_args.args[0]
        assert argv == ["helm", "upgrade", "myapp", "./chart"]

    def test_rollback_with_revision(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "rollback", {"release": "myapp", "revision": 3})
        argv = mock_run.call_args.args[0]
        assert argv == ["helm", "rollback", "myapp", "3"]

    def test_rollback_without_revision(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "rollback", {"release": "myapp"})
        argv = mock_run.call_args.args[0]
        assert argv == ["helm", "rollback", "myapp"]

    def test_uninstall(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "uninstall", {"release": "myapp"})
        argv = mock_run.call_args.args[0]
        assert argv == ["helm", "uninstall", "myapp"]

    def test_template(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "template", {"release": "myapp", "chart": "./chart"})
        argv = mock_run.call_args.args[0]
        assert argv == ["helm", "template", "myapp", "./chart"]

    def test_lint(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "lint", {"chart": "./chart"})
        argv = mock_run.call_args.args[0]
        assert argv == ["helm", "lint", "./chart"]

    def test_list(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "list", {})
        argv = mock_run.call_args.args[0]
        assert argv == ["helm", "list"]

    def test_list_with_namespace(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "list", {"namespace": "prod"})
        argv = mock_run.call_args.args[0]
        assert argv == ["helm", "list", "-n", "prod"]

    def test_status(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "status", {"release": "myapp"})
        argv = mock_run.call_args.args[0]
        assert argv == ["helm", "status", "myapp"]

    def test_context_appended(self, tmp_path: Path) -> None:
        options = {"release": "myapp", "context": "prod-cluster"}
        mock_run = self._run(tmp_path, "status", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["helm", "status", "myapp", "--kube-context", "prod-cluster"]

    def test_unknown_operation_raises_value_error(self, tmp_path: Path) -> None:
        runner = HelmRunner(_definition(), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/helm"),
            patch(f"{_MODULE}.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation="bogus"))


class TestMissingRequiredOptions:
    @pytest.mark.parametrize(
        "operation,options",
        [
            ("install", {"chart": "./chart"}),
            ("install", {"release": "myapp"}),
            ("upgrade", {"chart": "./chart"}),
            ("upgrade", {"release": "myapp"}),
            ("rollback", {}),
            ("uninstall", {}),
            ("template", {"chart": "./chart"}),
            ("template", {"release": "myapp"}),
            ("lint", {}),
            ("status", {}),
        ],
    )
    def test_missing_required_option_raises_value_error(
        self, tmp_path: Path, operation: str, options: dict
    ) -> None:
        runner = HelmRunner(_definition(options), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/helm"),
            patch(f"{_MODULE}.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation=operation))


class TestKubeconfigInEnv:
    def test_kubeconfig_reaches_env(self, tmp_path: Path) -> None:
        options = {"release": "myapp", "kubeconfig": "/home/user/.kube/staging-config"}
        runner = HelmRunner(_definition(options), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/helm"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="status"))

        env = mock_run.call_args.kwargs["env"]
        assert env["KUBECONFIG"] == "/home/user/.kube/staging-config"
        argv = mock_run.call_args.args[0]
        assert "--kubeconfig" not in argv
        assert "/home/user/.kube/staging-config" not in argv


class TestMissingBinary:
    def test_missing_binary_raises_runtime_error(self, tmp_path: Path) -> None:
        runner = HelmRunner(_definition({"release": "myapp"}), settings)
        with patch(f"{_MODULE}.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="helm CLI not found"):
                runner.run(_payload(tmp_path, operation="status"))

    def test_missing_binary_never_calls_subprocess(self, tmp_path: Path) -> None:
        runner = HelmRunner(_definition({"release": "myapp"}), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value=None),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            with pytest.raises(RuntimeError):
                runner.run(_payload(tmp_path, operation="status"))
        mock_run.assert_not_called()


class TestSecretsReachTheEnvironment:
    def test_secrets_in_env(self, tmp_path: Path) -> None:
        runner = HelmRunner(_definition({"release": "myapp"}), settings)
        secrets = {"HELM_TOKEN": "s3cr3t-value"}
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/helm"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="status", secrets=secrets))

        env = mock_run.call_args.kwargs["env"]
        assert env["HELM_TOKEN"] == "s3cr3t-value"

    def test_secrets_win_over_project_and_definition_env(self, tmp_path: Path) -> None:
        definition = _definition({"release": "myapp"}, env={"HELM_TOKEN": "definition-value"})
        runner = HelmRunner(definition, settings)
        payload = RunnerPayload(
            project_name="proj",
            project=ProjectConfig(path=tmp_path, env={"HELM_TOKEN": "project-value"}),
            task_name="t",
            step=TaskStep(name="s", runner="helm", command="status"),
            metadata={},
            secrets={"HELM_TOKEN": "secret-value"},
        )
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/helm"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(payload)

        env = mock_run.call_args.kwargs["env"]
        assert env["HELM_TOKEN"] == "secret-value"


class TestIsDestructive:
    @pytest.mark.parametrize("operation", ["install", "upgrade", "rollback", "uninstall"])
    def test_destructive_ops(self, tmp_path: Path, operation: str) -> None:
        options = {"release": "myapp", "chart": "./chart"}
        runner = HelmRunner(_definition(options), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=operation)) is True

    @pytest.mark.parametrize("operation", ["template", "lint", "list", "status"])
    def test_non_destructive_ops(self, tmp_path: Path, operation: str) -> None:
        options = {"release": "myapp", "chart": "./chart"}
        runner = HelmRunner(_definition(options), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=operation)) is False

    def test_default_operation_when_unset_is_not_destructive(self, tmp_path: Path) -> None:
        runner = HelmRunner(_definition({"release": "myapp"}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=None)) is False


class TestNoCaptureLeakPath:
    def test_runner_has_no_capture_method(self) -> None:
        runner = HelmRunner(_definition(), settings)
        assert not hasattr(runner, "capture")

    def test_run_streams_live_not_captured(self, tmp_path: Path) -> None:
        runner = HelmRunner(_definition({"release": "myapp"}), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/helm"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="status"))

        assert mock_run.call_args.kwargs["capture_output"] is False


class TestCaseNormalization:
    def test_case_variant_operation_normalizes_and_works(self, tmp_path: Path) -> None:
        runner = HelmRunner(_definition({"release": "myapp"}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="Uninstall")) is True
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/helm"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="Uninstall"))
        argv = mock_run.call_args.args[0]
        assert argv == ["helm", "uninstall", "myapp"]


class TestGateArgvAgreement:
    """Phase 17c: `is_destructive()` and `_build_command()` both resolve the
    operation from the SAME `options` dict via the same helper, so they can
    never disagree about whether an operation mutates the cluster. For
    options that make `_build_command` raise (missing required option,
    unknown operation), that's fine — fail-closed execution is a valid way
    to never disagree with a False gate."""

    _MUTATING_OPS = frozenset({"install", "upgrade", "rollback", "uninstall"})

    def _argv_is_mutating(self, argv: list[str]) -> bool:
        return argv[1] in self._MUTATING_OPS

    @pytest.mark.parametrize(
        "operation,options,expect_raises",
        [
            ("install", {"release": "myapp", "chart": "./chart"}, False),
            ("install", {"chart": "./chart"}, True),
            ("upgrade", {"release": "myapp", "chart": "./chart"}, False),
            ("rollback", {"release": "myapp"}, False),
            ("rollback", {}, True),
            ("uninstall", {"release": "myapp"}, False),
            ("template", {"release": "myapp", "chart": "./chart"}, False),
            ("lint", {"chart": "./chart"}, False),
            ("list", {}, False),
            ("status", {"release": "myapp"}, False),
            ("status", {}, True),
        ],
    )
    def test_gate_agrees_with_executed_argv(
        self, tmp_path: Path, operation: str, options: dict, expect_raises: bool
    ) -> None:
        runner = HelmRunner(_definition(options), settings)
        gate_result = runner.is_destructive(_payload(tmp_path, operation=operation))

        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/helm"),
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
