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
from hivepilot.runners.kubectl_runner import _KNOWN_ROLLOUT_SUBS, KubectlRunner


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

    def test_rollout_pause(self, tmp_path: Path) -> None:
        options = {"rollout": "pause", "resource": "deployment/foo"}
        mock_run = self._run(tmp_path, "rollout", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "rollout", "pause", "deployment/foo"]

    def test_rollout_resume(self, tmp_path: Path) -> None:
        options = {"rollout": "resume", "resource": "deployment/foo"}
        mock_run = self._run(tmp_path, "rollout", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "rollout", "resume", "deployment/foo"]

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

    @pytest.mark.parametrize("sub", ["restart", "undo", "pause", "resume"])
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


class TestRolloutSubValidation:
    """Phase 28b hardening: the `rollout` branch of `_build_command` must
    validate `sub` against a closed set, failing closed like the top-level
    operation dispatch (`else: raise ValueError`) does."""

    def test_unknown_rollout_sub_raises_value_error(self, tmp_path: Path) -> None:
        runner = KubectlRunner(
            _definition({"rollout": "scale", "resource": "deployment/foo"}), settings
        )
        with (
            patch("hivepilot.runners.kubectl_runner.shutil.which", return_value="/usr/bin/kubectl"),
            patch("hivepilot.runners.kubectl_runner.subprocess.run"),
        ):
            with pytest.raises(ValueError, match="Unknown kubectl rollout subcommand"):
                runner.run(_payload(tmp_path, operation="rollout"))


class TestMissingResourceValidation:
    """Phase 28b hardening: `get`/`describe`/`rollout`/`delete`-by-resource
    must fail fast with a clear `ValueError` when `options.resource` is
    empty/absent, instead of emitting an empty string into argv that
    kubectl would reject cryptically."""

    @pytest.mark.parametrize(
        "operation,options",
        [
            ("get", {}),
            ("describe", {}),
            ("rollout", {"rollout": "status"}),
            ("delete", {"resource": ""}),
        ],
    )
    def test_missing_resource_raises_value_error(
        self, tmp_path: Path, operation: str, options: dict
    ) -> None:
        runner = KubectlRunner(_definition(options), settings)
        with (
            patch("hivepilot.runners.kubectl_runner.shutil.which", return_value="/usr/bin/kubectl"),
            patch("hivepilot.runners.kubectl_runner.subprocess.run"),
        ):
            with pytest.raises(ValueError, match="requires options.resource"):
                runner.run(_payload(tmp_path, operation=operation))

    def test_delete_by_manifest_does_not_require_resource(self, tmp_path: Path) -> None:
        # The `delete -f <manifest>` path never touches `options.resource`.
        runner = KubectlRunner(_definition({"manifest": "deploy.yaml"}), settings)
        with (
            patch("hivepilot.runners.kubectl_runner.shutil.which", return_value="/usr/bin/kubectl"),
            patch("hivepilot.runners.kubectl_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="delete"))
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "delete", "-f", "deploy.yaml"]


class TestCaseNormalization:
    """Phase 28b hardening: operation and rollout-sub values are normalized
    via `.strip().lower()` in one shared spot each (`_resolve_operation` /
    `_resolve_rollout_sub`), used by both `is_destructive` and
    `_build_command`, so a case variant like `"Delete"`/`"Restart"` can
    never make the gate and the executed argv disagree."""

    def test_case_variant_operation_normalizes_and_works(self, tmp_path: Path) -> None:
        runner = KubectlRunner(_definition({"resource": "pod", "name": "foo"}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="Delete")) is True
        with (
            patch("hivepilot.runners.kubectl_runner.shutil.which", return_value="/usr/bin/kubectl"),
            patch("hivepilot.runners.kubectl_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="Delete"))
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "delete", "pod", "foo"]

    def test_case_variant_rollout_sub_normalizes_and_works(self, tmp_path: Path) -> None:
        runner = KubectlRunner(
            _definition({"rollout": "Restart", "resource": "deployment/foo"}), settings
        )
        assert runner.is_destructive(_payload(tmp_path, operation="rollout")) is True
        with (
            patch("hivepilot.runners.kubectl_runner.shutil.which", return_value="/usr/bin/kubectl"),
            patch("hivepilot.runners.kubectl_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="rollout"))
        argv = mock_run.call_args.args[0]
        assert argv == ["kubectl", "rollout", "restart", "deployment/foo"]


class TestGateArgvAgreement:
    """Phase 28b hardening: `is_destructive()` and `_build_command()` are
    both resolved from the SAME `options` dict. This walks every operation
    (and every rollout sub-command) and asserts the two can never disagree:
    `is_destructive()` returning True must correspond exactly to argv that
    kubectl would actually run being a mutating one (apply / delete /
    rollout {restart,undo,pause,resume}), and a non-destructive
    classification must never correspond to a mutating executed argv. A
    future edit to only one of `is_destructive`/`_build_command` that makes
    them diverge should fail this test. For options that make `_build_command`
    raise (unknown rollout sub, missing resource), that's fine — fail-closed
    execution is a valid way to never disagree with a False gate."""

    _MUTATING_ROLLOUT_SUBS = frozenset({"restart", "undo", "pause", "resume"})

    def _argv_is_mutating(self, argv: list[str]) -> bool:
        if argv[1] in ("apply", "delete"):
            return True
        if argv[1] == "rollout":
            return argv[2] in self._MUTATING_ROLLOUT_SUBS
        return False

    @pytest.mark.parametrize(
        "operation,options,expect_raises",
        [
            ("apply", {"manifest": "deploy.yaml"}, False),
            ("delete", {"resource": "pod", "name": "foo"}, False),
            ("delete", {"resource": ""}, True),
            ("get", {"resource": "pods"}, False),
            ("get", {}, True),
            ("diff", {"manifest": "deploy.yaml"}, False),
            ("describe", {"resource": "pod", "name": "foo"}, False),
            ("describe", {}, True),
            *[
                ("rollout", {"rollout": sub, "resource": "deployment/foo"}, False)
                for sub in sorted(_KNOWN_ROLLOUT_SUBS)
            ],
            ("rollout", {"rollout": "scale", "resource": "deployment/foo"}, True),
            ("rollout", {"rollout": "status"}, True),
        ],
    )
    def test_gate_agrees_with_executed_argv(
        self, tmp_path: Path, operation: str, options: dict, expect_raises: bool
    ) -> None:
        runner = KubectlRunner(_definition(options), settings)
        gate_result = runner.is_destructive(_payload(tmp_path, operation=operation))

        with (
            patch("hivepilot.runners.kubectl_runner.shutil.which", return_value="/usr/bin/kubectl"),
            patch("hivepilot.runners.kubectl_runner.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            if expect_raises:
                with pytest.raises(ValueError):
                    runner.run(_payload(tmp_path, operation=operation))
                return
            runner.run(_payload(tmp_path, operation=operation))

        argv = mock_run.call_args.args[0]
        assert gate_result is self._argv_is_mutating(argv)
