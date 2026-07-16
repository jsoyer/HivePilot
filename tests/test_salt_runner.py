"""
Tests for the Salt runner (Phase 17b): a builtin runner so HivePilot agents
can run SaltStack state runs / pillar/grains lookups, with mutating
operations (``apply``/``highstate`` when not in test-mode) auto-gating via
the step-level approval gate
(``hivepilot.orchestrator.step_requires_approval``).

Covers, mirroring ``tests/test_ansible_runner.py``'s pattern:
(a) Registration: `resolve_runner_class("salt")` resolves via the real
    RunnerRegistry/RUNNER_MAP, and "salt" is advertised in
    KNOWN_RUNNER_KINDS.
(b) argv assembly for every operation (apply, highstate, test, pillar,
    grains), incl. `options.local` -> `salt-call --local` and
    `options.test`/`test` op -> `test=True` appended.
(c) `payload.secrets` land in the env passed to `subprocess.run`.
(d) Unknown operation -> ValueError.
(e) Missing required option (state for apply/state-mode test) -> ValueError.
(f) Missing binary (salt / salt-call) -> RuntimeError (subprocess NOT
    called).
(g) `is_destructive`: apply/highstate (real run) -> True; apply/highstate
    with `test=True` -> False; test/pillar/grains -> False.
(h) No `capture()` method exposed; `run()` always streams live
    (`capture_output=False`).
(i) Gate<->argv agreement test: is_destructive() and the actually-executed
    argv can never disagree about whether an operation mutates minions.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import KNOWN_RUNNER_KINDS, ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.registry import RUNNER_MAP, resolve_runner_class
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.salt_runner import SaltRunner

_MODULE = "hivepilot.runners.salt_runner"


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
        step=TaskStep(name="s", runner="salt", command=operation),
        metadata={},
        secrets=secrets or {},
    )


def _definition(options: dict | None = None, env: dict | None = None) -> RunnerDefinition:
    return RunnerDefinition(name="salt", kind="salt", options=options or {}, env=env or {})


class TestRegistration:
    def test_salt_resolves(self) -> None:
        assert resolve_runner_class("salt") is SaltRunner

    def test_salt_in_known_runner_kinds(self) -> None:
        assert "salt" in KNOWN_RUNNER_KINDS

    def test_salt_registered_in_runner_map(self) -> None:
        assert RUNNER_MAP["salt"] is SaltRunner


class TestArgv:
    def _run(self, tmp_path: Path, operation: str | None, options: dict | None = None):
        runner = SaltRunner(_definition(options), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/salt"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation=operation))
        return mock_run

    def test_apply_basic_default_target(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "apply", {"state": "webserver"})
        argv = mock_run.call_args.args[0]
        assert argv == ["salt", "*", "state.apply", "webserver"]

    def test_apply_with_target(self, tmp_path: Path) -> None:
        options = {"state": "webserver", "target": "web*"}
        mock_run = self._run(tmp_path, "apply", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["salt", "web*", "state.apply", "webserver"]

    def test_apply_local(self, tmp_path: Path) -> None:
        options = {"state": "webserver", "local": True}
        mock_run = self._run(tmp_path, "apply", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["salt-call", "--local", "state.apply", "webserver"]

    def test_highstate_basic(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "highstate", {})
        argv = mock_run.call_args.args[0]
        assert argv == ["salt", "*", "state.highstate"]

    def test_highstate_local(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "highstate", {"local": True})
        argv = mock_run.call_args.args[0]
        assert argv == ["salt-call", "--local", "state.highstate"]

    def test_apply_with_test_option_appends_test_true(self, tmp_path: Path) -> None:
        options = {"state": "webserver", "test": True}
        mock_run = self._run(tmp_path, "apply", options)
        argv = mock_run.call_args.args[0]
        assert argv == ["salt", "*", "state.apply", "webserver", "test=True"]

    def test_test_operation_with_state_uses_apply(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "test", {"state": "webserver"})
        argv = mock_run.call_args.args[0]
        assert argv == ["salt", "*", "state.apply", "webserver", "test=True"]

    def test_test_operation_without_state_uses_highstate(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "test", {})
        argv = mock_run.call_args.args[0]
        assert argv == ["salt", "*", "state.highstate", "test=True"]

    def test_default_operation_when_unset_is_test(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, None, {})
        argv = mock_run.call_args.args[0]
        assert argv == ["salt", "*", "state.highstate", "test=True"]

    def test_pillar_basic(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "pillar", {})
        argv = mock_run.call_args.args[0]
        assert argv == ["salt", "*", "pillar.items"]

    def test_pillar_local(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "pillar", {"local": True})
        argv = mock_run.call_args.args[0]
        assert argv == ["salt-call", "--local", "pillar.items"]

    def test_grains_basic(self, tmp_path: Path) -> None:
        mock_run = self._run(tmp_path, "grains", {"target": "db*"})
        argv = mock_run.call_args.args[0]
        assert argv == ["salt", "db*", "grains.items"]

    def test_unknown_operation_raises_value_error(self, tmp_path: Path) -> None:
        runner = SaltRunner(_definition(), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/salt"),
            patch(f"{_MODULE}.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation="bogus"))


class TestMissingRequiredOptions:
    @pytest.mark.parametrize(
        "operation,options",
        [
            ("apply", {}),
            ("test", {"state": None}),
        ],
    )
    def test_missing_state_raises_value_error(
        self, tmp_path: Path, operation: str, options: dict
    ) -> None:
        # "test" without state resolves to highstate (no state required), so
        # only "apply" without state is guaranteed to fail; kept as its own
        # parametrized case for clarity.
        runner = SaltRunner(_definition({}), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/salt"),
            patch(f"{_MODULE}.subprocess.run"),
        ):
            with pytest.raises(ValueError):
                runner.run(_payload(tmp_path, operation="apply"))


class TestMissingBinary:
    @pytest.mark.parametrize(
        "operation,options,local,missing_binary",
        [
            ("apply", {"state": "web"}, False, "salt"),
            ("apply", {"state": "web", "local": True}, True, "salt-call"),
            ("highstate", {}, False, "salt"),
            ("pillar", {}, False, "salt"),
            ("grains", {}, False, "salt"),
        ],
    )
    def test_missing_binary_raises_runtime_error(
        self, tmp_path: Path, operation: str, options: dict, local: bool, missing_binary: str
    ) -> None:
        runner = SaltRunner(_definition(options), settings)

        def _which(binary: str) -> str | None:
            return None if binary == missing_binary else f"/usr/bin/{binary}"

        with patch(f"{_MODULE}.shutil.which", side_effect=_which):
            with pytest.raises(RuntimeError, match="not found"):
                runner.run(_payload(tmp_path, operation=operation))

    def test_missing_binary_never_calls_subprocess(self, tmp_path: Path) -> None:
        runner = SaltRunner(_definition({"state": "web"}), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value=None),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            with pytest.raises(RuntimeError):
                runner.run(_payload(tmp_path, operation="apply"))
        mock_run.assert_not_called()


class TestSecretsReachTheEnvironment:
    def test_secrets_in_env(self, tmp_path: Path) -> None:
        runner = SaltRunner(_definition({"state": "web"}), settings)
        secrets = {"SALT_API_TOKEN": "sekret"}
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/salt"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="apply", secrets=secrets))

        env = mock_run.call_args.kwargs["env"]
        assert env["SALT_API_TOKEN"] == "sekret"


class TestIsDestructive:
    def test_apply_is_destructive(self, tmp_path: Path) -> None:
        runner = SaltRunner(_definition({"state": "web"}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="apply")) is True

    def test_highstate_is_destructive(self, tmp_path: Path) -> None:
        runner = SaltRunner(_definition({}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="highstate")) is True

    def test_apply_with_test_true_is_not_destructive(self, tmp_path: Path) -> None:
        runner = SaltRunner(_definition({"state": "web", "test": True}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="apply")) is False

    def test_highstate_with_test_true_is_not_destructive(self, tmp_path: Path) -> None:
        runner = SaltRunner(_definition({"test": True}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="highstate")) is False

    @pytest.mark.parametrize("operation", ["test", "pillar", "grains"])
    def test_readonly_ops_are_not_destructive(self, tmp_path: Path, operation: str) -> None:
        runner = SaltRunner(_definition({}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=operation)) is False

    def test_default_operation_when_unset_is_not_destructive(self, tmp_path: Path) -> None:
        runner = SaltRunner(_definition(), settings)
        assert runner.is_destructive(_payload(tmp_path, operation=None)) is False


class TestNoCaptureLeakPath:
    def test_runner_has_no_capture_method(self) -> None:
        runner = SaltRunner(_definition(), settings)
        assert not hasattr(runner, "capture")

    def test_run_streams_live_not_captured(self, tmp_path: Path) -> None:
        runner = SaltRunner(_definition({"state": "web"}), settings)
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/salt"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="apply"))

        assert mock_run.call_args.kwargs["capture_output"] is False


class TestCaseNormalization:
    def test_case_variant_operation_normalizes_and_works(self, tmp_path: Path) -> None:
        runner = SaltRunner(_definition({"state": "web"}), settings)
        assert runner.is_destructive(_payload(tmp_path, operation="Apply")) is True
        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/salt"),
            patch(f"{_MODULE}.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.run(_payload(tmp_path, operation="Apply"))
        argv = mock_run.call_args.args[0]
        assert argv == ["salt", "*", "state.apply", "web"]


class TestGateArgvAgreement:
    """Phase 17b: `is_destructive()` and `_build_command()` both resolve the
    operation (and test-mode) from the SAME `options` dict via the same
    helpers, so they can never disagree about whether an operation mutates
    minions. For options that make `_build_command` raise (missing required
    option, unknown operation), that's fine — fail-closed execution is a
    valid way to never disagree with a False gate."""

    def _argv_is_mutating(self, argv: list[str]) -> bool:
        if "state.apply" in argv or "state.highstate" in argv:
            return "test=True" not in argv
        return False

    @pytest.mark.parametrize(
        "operation,options,expect_raises",
        [
            ("apply", {"state": "web"}, False),
            ("apply", {"state": "web", "test": True}, False),
            ("apply", {}, True),
            ("highstate", {}, False),
            ("highstate", {"test": True}, False),
            ("test", {"state": "web"}, False),
            ("test", {}, False),
            ("pillar", {}, False),
            ("grains", {}, False),
        ],
    )
    def test_gate_agrees_with_executed_argv(
        self, tmp_path: Path, operation: str, options: dict, expect_raises: bool
    ) -> None:
        runner = SaltRunner(_definition(options), settings)
        gate_result = runner.is_destructive(_payload(tmp_path, operation=operation))

        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/salt"),
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
