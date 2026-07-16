"""
Tests for the Ansible runner (Phase 17b): a builtin runner so HivePilot
agents can run Ansible playbooks/ad-hoc commands, with mutating operations
(``playbook`` when not in check-mode, ``adhoc``) auto-gating via the
step-level approval gate (``hivepilot.orchestrator.step_requires_approval``).

Covers, mirroring ``tests/test_kubectl_runner.py``'s pattern:
(a) Registration: `resolve_runner_class("ansible")` resolves via the real
    RunnerRegistry/RUNNER_MAP, and "ansible" is advertised in
    KNOWN_RUNNER_KINDS (the RUNNER_MAP-kinds-are-known invariant test).
(b) argv assembly for every operation (playbook, check, adhoc, lint,
    galaxy-install) with extra_vars/limit/tags appended when set.
(c) `payload.secrets` (e.g. ANSIBLE_VAULT_PASSWORD_FILE) land in the env
    passed to `subprocess.run`.
(d) Unknown operation -> ValueError.
(e) Missing required option (playbook/inventory/pattern/module/requirements)
    -> clear ValueError.
(f) Missing binary -> RuntimeError (subprocess NOT called).
(g) `is_destructive`: playbook (real run) / adhoc -> True; check (dry-run,
    incl. playbook op with options.check=True) / lint / galaxy-install ->
    False.
(h) No `capture()` method exposed; `run()` always streams live
    (`capture_output=False`).
(i) Gate<->argv agreement test: is_destructive() and the actually-executed
    argv can never disagree about whether an operation mutates hosts.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import KNOWN_RUNNER_KINDS, ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.registry import RUNNER_MAP, resolve_runner_class
from hivepilot.runners.ansible_runner import AnsibleRunner
from hivepilot.runners.base import RunnerPayload

_MODULE = "hivepilot.runners.ansible_runner"


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
        step=TaskStep(name="s", runner="ansible", command=operation),
        metadata={},
        secrets=secrets or {},
    )


def _definition(options: dict | None = None, env: dict | None = None) -> RunnerDefinition:
    return RunnerDefinition(name="ansible", kind="ansible", options=options or {}, env=env or {})


class TestRegistration:
    def test_ansible_resolves(self) -> None:
        assert resolve_runner_class("ansible") is AnsibleRunner

    def test_ansible_in_known_runner_kinds(self) -> None:
        assert "ansible" in KNOWN_RUNNER_KINDS

    def test_ansible_registered_in_runner_map(self) -> None:
        assert RUNNER_MAP["ansible"] is AnsibleRunner


class TestArgv:
    def _run(self, tmp_path: Path, operation: str, options: dict | None = None):
        runner = AnsibleRunner(_definition(options), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/ansible-playbook"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation=operation))
        return mock_run

    def test_playbook_basic(self, tmp_path: Path) -> None:
        mock_run = self._run(
            tmp_path, "playbook", {"inventory": "hosts.ini", "playbook": "site.yml"}
        )
        argv = mock_run.call_args.args[0]
        assert argv == ["ansible-playbook", "-i", "hosts.ini", "site.yml"]

    def test_playbook_with_extra_vars_limit_tags(self, tmp_path: Path) -> None:
        options = {
            "inventory": "hosts.ini",
            "playbook": "site.yml",
            "extra_vars": "env=prod",
            "limit": "web",
            "tags": "deploy",
        }
        mock_run = self._run(tmp_path, "playbook", options)
        argv = mock_run.call_args.args[0]
        assert argv == [
            "ansible-playbook",
            "-i",
            "hosts.ini",
            "site.yml",
            "--extra-vars",
            "env=prod",
            "--limit",
            "web",
            "--tags",
            "deploy",
        ]

    def test_playbook_with_check_option_adds_check_flag(self, tmp_path: Path) -> None:
        options = {"inventory": "hosts.ini", "playbook": "site.yml", "check": True}
        mock_run = self._run(tmp_path, "playbook", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["ansible-playbook", "-i", "hosts.ini", "site.yml", "--check"]

    def test_check_operation(self, tmp_path: Path) -> None:
        options = {"inventory": "hosts.ini", "playbook": "site.yml"}
        mock_run = self._run(tmp_path, "check", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["ansible-playbook", "-i", "hosts.ini", "site.yml", "--check"]

    def test_adhoc_basic(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "adhoc", {"pattern": "web", "module": "ping"})
        argv = mock_run.call_args.args[0]
        assert argv == ["ansible", "web", "-m", "ping"]

    def test_adhoc_with_args(self, tmp_path: Path) -> None:
        options = {"pattern": "web", "module": "shell", "args": "uptime"}
        mock_run = self._run(tmp_path, "adhoc", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["ansible", "web", "-m", "shell", "-a", "uptime"]

    def test_adhoc_with_inventory(self, tmp_path: Path) -> None:
        options = {"pattern": "web", "module": "ping", "inventory": "hosts.ini"}
        mock_run = self._run(tmp_path, "adhoc", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["ansible", "web", "-m", "ping", "-i", "hosts.ini"]

    def test_lint(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "lint", {"playbook": "site.yml"})
        argv = mock_run.call_args.args[0]
        assert argv == ["ansible-lint", "site.yml"]

    def test_galaxy_install(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "galaxy-install", {"requirements": "requirements.yml"})
        argv = mock_run.call_args.args[0]
        assert argv == ["ansible-galaxy", "install", "-r", "requirements.yml"]

    def test_unknown_operation_raises_value_error(self, tmp_path: Path) -> None:
        runner = AnsibleRunner(_definition(), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/ansible-playbook"),
            patch(f"{_MODULE}.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation="bogus"))


class TestMissingRequiredOptions:
    @pytest.mark.parametrize(
        "operation,options",
        [
            ("playbook", {"inventory": "hosts.ini"}),
            ("playbook", {"playbook": "site.yml"}),
            ("check", {"inventory": "hosts.ini"}),
            ("adhoc", {"module": "ping"}),
            ("adhoc", {"pattern": "web"}),
            ("lint", {}),
            ("galaxy-install", {}),
        ],
    )
    def test_missing_required_option_raises_value_error(
        self, tmp_path: Path, operation: str, options: dict
    ) -> None:
        runner = AnsibleRunner(_definition(options), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/ansible"),
            patch(f"{_MODULE}.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation=operation))


class TestMissingBinary:
    def test_missing_binary_raises_runtime_error(self, tmp_path: Path) -> None:
        options = {"inventory": "hosts.ini", "playbook": "site.yml"}
        runner = AnsibleRunner(_definition(options), settings)
        with patch(f"{_MODULE}.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="not found"):
                runner.run(_payload(tmp_path, operation="playbook"))

    def test_missing_binary_never_calls_subprocess(self, tmp_path: Path) -> None:
        options = {"inventory": "hosts.ini", "playbook": "site.yml"}
        runner = AnsibleRunner(_definition(options), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value=None),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            with pytest.raises(RuntimeError):
                runner.run(_payload(tmp_path, operation="playbook"))
        mock_run.assert_not_called()


class TestSecretsReachTheEnvironment:
    def test_secrets_in_env(self, tmp_path: Path) -> None:
        options = {"inventory": "hosts.ini", "playbook": "site.yml"}
        runner = AnsibleRunner(_definition(options), settings)
        secrets = {"ANSIBLE_VAULT_PASSWORD_FILE": "/secure/vault-pass"}
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/ansible-playbook"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="playbook", secrets=secrets))

        env = mock_run.call_args.kwargs["env"]
        assert env["ANSIBLE_VAULT_PASSWORD_FILE"] == "/secure/vault-pass"

    def test_secrets_win_over_project_and_definition_env(self, tmp_path: Path) -> None:
        options = {"inventory": "hosts.ini", "playbook": "site.yml"}
        definition = _definition(options, env={"ANSIBLE_VAULT_PASSWORD_FILE": "definition-value"})
        runner = AnsibleRunner(definition, settings)
        payload = RunnerPayload(
            project_name="proj",
            project=ProjectConfig(
                path=tmp_path, env={"ANSIBLE_VAULT_PASSWORD_FILE": "project-value"}
            ),
            task_name="t",
            step=TaskStep(name="s", runner="ansible", command="playbook"),
            metadata={},
            secrets={"ANSIBLE_VAULT_PASSWORD_FILE": "secret-value"},
        )
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/ansible-playbook"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(payload)

        env = mock_run.call_args.kwargs["env"]
        assert env["ANSIBLE_VAULT_PASSWORD_FILE"] == "secret-value"


class TestIsDestructive:
    def test_playbook_is_destructive(self, tmp_path: Path) -> None:
        options = {"inventory": "hosts.ini", "playbook": "site.yml"}
        runner = AnsibleRunner(_definition(options), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="playbook")) is True

    def test_adhoc_is_destructive(self, tmp_path: Path) -> None:
        options = {"pattern": "web", "module": "shell"}
        runner = AnsibleRunner(_definition(options), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="adhoc")) is True

    def test_check_operation_is_not_destructive(self, tmp_path: Path) -> None:
        options = {"inventory": "hosts.ini", "playbook": "site.yml"}
        runner = AnsibleRunner(_definition(options), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="check")) is False

    def test_playbook_with_check_option_is_not_destructive(self, tmp_path: Path) -> None:
        options = {"inventory": "hosts.ini", "playbook": "site.yml", "check": True}
        runner = AnsibleRunner(_definition(options), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="playbook")) is False

    @pytest.mark.parametrize("operation", ["lint", "galaxy-install"])
    def test_readonly_ops_are_not_destructive(self, tmp_path: Path, operation: str) -> None:
        runner = AnsibleRunner(_definition(), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=operation)) is False

    def test_default_operation_when_unset_is_destructive(self, tmp_path: Path) -> None:
        # Default operation is "playbook" (a real run), matching the runner's
        # documented default in `_resolve_operation`.
        runner = AnsibleRunner(_definition(), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=None)) is True


class TestNoCaptureLeakPath:
    def test_runner_has_no_capture_method(self) -> None:
        runner = AnsibleRunner(_definition(), settings)
        assert not hasattr(runner, "capture")

    def test_run_streams_live_not_captured(self, tmp_path: Path) -> None:
        options = {"inventory": "hosts.ini", "playbook": "site.yml"}
        runner = AnsibleRunner(_definition(options), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/ansible-playbook"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="playbook"))

        assert mock_run.call_args.kwargs["capture_output"] is False


class TestCaseNormalization:
    def test_case_variant_operation_normalizes_and_works(self, tmp_path: Path) -> None:
        options = {"inventory": "hosts.ini", "playbook": "site.yml"}
        runner = AnsibleRunner(_definition(options), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="Check")) is False
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/ansible-playbook"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="Check"))
        argv = mock_run.call_args.args[0]
        assert argv == ["ansible-playbook", "-i", "hosts.ini", "site.yml", "--check"]


class TestGateArgvAgreement:
    """Phase 17b: `is_destructive()` and `_build_command()` both resolve the
    operation (and check-mode) from the SAME `options` dict via the same
    helpers, so they can never disagree about whether an operation mutates
    hosts. For options that make `_build_command` raise (missing required
    option, unknown operation), that's fine — fail-closed execution is a
    valid way to never disagree with a False gate."""

    def _argv_is_mutating(self, argv: list[str]) -> bool:
        if argv[0] == "ansible-playbook":
            return "--check" not in argv
        if argv[0] == "ansible":
            return True
        return False

    @pytest.mark.parametrize(
        "operation,options,expect_raises",
        [
            ("playbook", {"inventory": "hosts.ini", "playbook": "site.yml"}, False),
            (
                "playbook",
                {"inventory": "hosts.ini", "playbook": "site.yml", "check": True},
                False,
            ),
            ("check", {"inventory": "hosts.ini", "playbook": "site.yml"}, False),
            ("playbook", {"playbook": "site.yml"}, True),
            ("adhoc", {"pattern": "web", "module": "ping"}, False),
            ("adhoc", {"module": "ping"}, True),
            ("lint", {"playbook": "site.yml"}, False),
            ("lint", {}, True),
            ("galaxy-install", {"requirements": "requirements.yml"}, False),
            ("galaxy-install", {}, True),
        ],
    )
    def test_gate_agrees_with_executed_argv(
        self, tmp_path: Path, operation: str, options: dict, expect_raises: bool
    ) -> None:
        runner = AnsibleRunner(_definition(options), settings)
        gate_result = runner.is_destructive(_payload(tmp_path, operation=operation))

        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/ansible-playbook"),
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
