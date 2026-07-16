"""
Tests for the kubectl runner (Phase 28b): a builtin runner so HivePilot
agents can operate a Kubernetes cluster, with destructive operations
(``apply``/``delete``/``rollout restart|undo``) auto-gating via the
step-level approval gate (``hivepilot.orchestrator.step_requires_approval``).

Covers, mirroring ``tests/test_iac_runner.py``'s pattern:
(a) Registration: `resolve_runner_class("kubectl")` resolves via the real
    RunnerRegistry/RUNNER_MAP, and "kubectl" is advertised in
    KNOWN_RUNNER_KINDS (the RUNNER_MAP-kinds-are-known invariant test).
(b) argv assembly for every operation (apply -f / apply -k, delete by
    manifest / delete by resource+name, get with -o, diff, rollout
    status/restart/undo, describe), with `-n <namespace>` and
    `--context <ctx>` appended when set.
(c) `KUBECONFIG` reaches the env passed to `subprocess.run` when
    `options.kubeconfig` is set (env, never argv).
(d) `payload.secrets` land in the env passed to `subprocess.run`.
(e) Unknown operation -> ValueError.
(f) Missing `kubectl` binary -> RuntimeError (subprocess NOT called).
(g) `is_destructive`: apply/delete/rollout-restart/rollout-undo -> True;
    get/diff/describe/rollout-status/rollout-history -> False.
(h) No `capture()` method exposed (kubectl output can echo secret-derived
    cluster data via `kubectl get secret -o yaml`; must stream live only).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import KNOWN_RUNNER_KINDS, ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.registry import RUNNER_MAP, resolve_runner_class
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.kubectl_runner import KubectlRunner


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
        step=TaskStep(name="s", runner="kubectl", command=operation),
        metadata={},
        secrets=secrets or {},
    )


def _definition(options: dict | None = None, env: dict | None = None) -> RunnerDefinition:
    return RunnerDefinition(name="kubectl", kind="kubectl", options=options or {}, env=env or {})


class TestRegistration:
    def test_kubectl_resolves(self) -> None:
        assert resolve_runner_class("kubectl") is KubectlRunner

    def test_kubectl_in_known_runner_kinds(self) -> None:
        assert "kubectl" in KNOWN_RUNNER_KINDS

    def test_kubectl_registered_in_runner_map(self) -> None:
        assert RUNNER_MAP["kubectl"] is KubectlRunner


class TestArgv:
    def _run(self, tmp_path: Path, operation: str, options: dict | None = None):
        runner = KubectlRunner(_definition(options), settings)
        with (
            patch("hivepilot.runners.kubectl_runner.shutil.which", return_value="/usr/bin/kubectl"),
            patch("hivepilot.runners.kubectl_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation=operation))
        return mock_run

    def test_apply_with_manifest(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "apply", {"manifest": "deploy.yaml"})
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "apply", "-f", "deploy.yaml"]

    def test_apply_with_kustomize(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "apply", {"kustomize": "overlays/prod"})
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "apply", "-k", "overlays/prod"]

    def test_delete_by_manifest(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "delete", {"manifest": "deploy.yaml"})
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "delete", "-f", "deploy.yaml"]

    def test_delete_by_resource_and_name(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "delete", {"resource": "pod", "name": "foo"})
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "delete", "pod", "foo"]

    def test_get_default_output(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "get", {"resource": "pods"})
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "get", "pods", "-o", "wide"]

    def test_get_with_name_and_output(self, tmp_path: Path) -> None:
        options = {"resource": "pod", "name": "foo", "output": "json"}
        mock_run = self._run(tmp_path, "get", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "get", "pod", "foo", "-o", "json"]

    def test_diff(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "diff", {"manifest": "deploy.yaml"})
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "diff", "-f", "deploy.yaml"]

    def test_rollout_status(self, tmp_path: Path) -> None:
        options = {"rollout": "status", "resource": "deployment/foo"}
        mock_run = self._run(tmp_path, "rollout", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "rollout", "status", "deployment/foo"]

    def test_rollout_restart(self, tmp_path: Path) -> None:
        options = {"rollout": "restart", "resource": "deployment/foo"}
        mock_run = self._run(tmp_path, "rollout", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "rollout", "restart", "deployment/foo"]

    def test_rollout_undo(self, tmp_path: Path) -> None:
        options = {"rollout": "undo", "resource": "deployment/foo"}
        mock_run = self._run(tmp_path, "rollout", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "rollout", "undo", "deployment/foo"]

    def test_describe(self, tmp_path: Path) -> None:
        options = {"resource": "pod", "name": "foo"}
        mock_run = self._run(tmp_path, "describe", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "describe", "pod", "foo"]

    def test_describe_without_name(self, tmp_path: Path) -> None:
        options = {"resource": "nodes"}
        mock_run = self._run(tmp_path, "describe", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "describe", "nodes"]

    def test_namespace_appended(self, tmp_path: Path) -> None:
        options = {"resource": "pods", "namespace": "prod"}
        mock_run = self._run(tmp_path, "get", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "get", "pods", "-o", "wide", "-n", "prod"]

    def test_context_appended(self, tmp_path: Path) -> None:
        options = {"resource": "pods", "context": "prod-cluster"}
        mock_run = self._run(tmp_path, "get", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "get", "pods", "-o", "wide", "--context", "prod-cluster"]

    def test_namespace_and_context_both_appended(self, tmp_path: Path) -> None:
        options = {"manifest": "deploy.yaml", "namespace": "prod", "context": "prod-cluster"}
        mock_run = self._run(tmp_path, "apply", options)
        argv = mock_run.call_args.args[0]
        assert argv == [
            "kubectl",
            "apply",
            "-f",
            "deploy.yaml",
            "-n",
            "prod",
            "--context",
            "prod-cluster",
        ]

    def test_unknown_operation_raises_value_error(self, tmp_path: Path) -> None:
        runner = KubectlRunner(_definition(), settings)
        with (
            patch("hivepilot.runners.kubectl_runner.shutil.which", return_value="/usr/bin/kubectl"),
            patch("hivepilot.runners.kubectl_runner.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation="bogus"))


class TestKubeconfigInEnv:
    def test_kubeconfig_reaches_env(self, tmp_path: Path) -> None:
        options = {"resource": "pods", "kubeconfig": "/home/user/.kube/staging-config"}
        runner = KubectlRunner(_definition(options), settings)
        with (
            patch("hivepilot.runners.kubectl_runner.shutil.which", return_value="/usr/bin/kubectl"),
            patch("hivepilot.runners.kubectl_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="get"))

        env = mock_run.call_args.kwargs["env"]
        assert env["KUBECONFIG"] == "/home/user/.kube/staging-config"
        argv = mock_run.call_args.args[0]
        assert "--kubeconfig" not in argv
        assert "/home/user/.kube/staging-config" not in argv


class TestMissingBinary:
    def test_missing_binary_raises_runtime_error(self, tmp_path: Path) -> None:
        runner = KubectlRunner(_definition({"resource": "pods"}), settings)
        with patch("hivepilot.runners.kubectl_runner.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="kubectl CLI not found"):
                runner.run(_payload(tmp_path, operation="get"))

    def test_missing_binary_never_calls_subprocess(self, tmp_path: Path) -> None:
        runner = KubectlRunner(_definition({"resource": "pods"}), settings)
        with (
            patch("hivepilot.runners.kubectl_runner.shutil.which", return_value=None),
            patch("hivepilot.runners.kubectl_runner.subprocess.run") as mock_run,
        ):
            with pytest.raises(RuntimeError):
                runner.run(_payload(tmp_path, operation="get"))
        mock_run.assert_not_called()


class TestSecretsReachTheEnvironment:
    def test_secrets_in_env(self, tmp_path: Path) -> None:
        runner = KubectlRunner(_definition({"resource": "pods"}), settings)
        secrets = {"KUBE_TOKEN": "s3cr3t-value"}
        with (
            patch("hivepilot.runners.kubectl_runner.shutil.which", return_value="/usr/bin/kubectl"),
            patch("hivepilot.runners.kubectl_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="get", secrets=secrets))

        env = mock_run.call_args.kwargs["env"]
        assert env["KUBE_TOKEN"] == "s3cr3t-value"

    def test_secrets_win_over_project_and_definition_env(self, tmp_path: Path) -> None:
        definition = _definition({"resource": "pods"}, env={"KUBE_TOKEN": "definition-value"})
        runner = KubectlRunner(definition, settings)
        payload = RunnerPayload(
            project_name="proj",
            project=ProjectConfig(path=tmp_path, env={"KUBE_TOKEN": "project-value"}),
            task_name="t",
            step=TaskStep(name="s", runner="kubectl", command="get"),
            metadata={},
            secrets={"KUBE_TOKEN": "secret-value"},
        )
        with (
            patch("hivepilot.runners.kubectl_runner.shutil.which", return_value="/usr/bin/kubectl"),
            patch("hivepilot.runners.kubectl_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(payload)

        env = mock_run.call_args.kwargs["env"]
        assert env["KUBE_TOKEN"] == "secret-value"


class TestIsDestructive:
    """Phase 28b: `is_destructive(payload)` — the optional, structural
    (getattr-discovered) contract the step-level approval gate
    (`hivepilot.orchestrator.step_requires_approval`) queries. Resolves the
    operation (and rollout sub-command) exactly the same way `run()` does."""

    @pytest.mark.parametrize("operation", ["apply", "delete"])
    def test_destructive_ops(self, tmp_path: Path, operation: str) -> None:
        runner = KubectlRunner(_definition({"resource": "pods"}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=operation)) is True

    @pytest.mark.parametrize("operation", ["get", "diff", "describe"])
    def test_non_destructive_ops(self, tmp_path: Path, operation: str) -> None:
        runner = KubectlRunner(_definition({"resource": "pods"}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=operation)) is False

    @pytest.mark.parametrize("sub", ["restart", "undo"])
    def test_rollout_mutating_subs_are_destructive(self, tmp_path: Path, sub: str) -> None:
        runner = KubectlRunner(
            _definition({"rollout": sub, "resource": "deployment/foo"}), settings
        )
        assert runner.is_destructive(_payload(tmp_path, operation="rollout")) is True

    @pytest.mark.parametrize("sub", ["status", "history"])
    def test_rollout_readonly_subs_are_not_destructive(self, tmp_path: Path, sub: str) -> None:
        runner = KubectlRunner(
            _definition({"rollout": sub, "resource": "deployment/foo"}), settings
        )
        assert runner.is_destructive(_payload(tmp_path, operation="rollout")) is False

    def test_default_operation_when_unset_is_not_destructive(self, tmp_path: Path) -> None:
        runner = KubectlRunner(_definition({"resource": "pods"}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=None)) is False


class TestNoCaptureLeakPath:
    """kubectl output (e.g. `kubectl get secret -o yaml`) can base64-dump
    secret data. This runner must never expose a `capture()` method, and
    `run()` must always stream live (`capture_output=False`)."""

    def test_runner_has_no_capture_method(self) -> None:
        runner = KubectlRunner(_definition(), settings)
        assert not hasattr(runner, "capture")

    def test_run_streams_live_not_captured(self, tmp_path: Path) -> None:
        runner = KubectlRunner(_definition({"resource": "pods"}), settings)
        with (
            patch("hivepilot.runners.kubectl_runner.shutil.which", return_value="/usr/bin/kubectl"),
            patch("hivepilot.runners.kubectl_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="get"))

        assert mock_run.call_args.kwargs["capture_output"] is False
