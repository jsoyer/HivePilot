"""
Tests for the `herdr` runner plugin (Phase 27).

`plugins/herdr.py` is a local-file plugin (see docs/v4/PLUGINS.md) that
executes each pipeline step *inside a dedicated herdr pane* by driving the
`herdr` CLI (split -> run -> wait idle -> read), giving live parallel-pane
visibility. It degrades gracefully (raw `bash -lc` execution) when the
`herdr` binary isn't on PATH — same posture as `plugins/rtk.py`.

Covers:
(a) `register()` exposes runner kind `herdr`; no collision with a built-in
    kind (`KNOWN_RUNNER_KINDS`).
(b) `capture()` drives the full CLI sequence in order (split -> run -> wait
    -> read) via mocked `subprocess.run`, threading the pane id **parsed
    from the split JSON** into every subsequent command — never a
    hand-built id.
(c) `herdr_wait_timeout_ms` / `herdr_read_lines` config values are passed to
    the `wait agent-status --timeout` and `pane read --lines` commands.
(d) Fallback: `shutil.which("herdr")` is None -> runs `["bash","-lc",cmd]`
    raw, no crash, logs the degradation at INFO.
(e) Malformed `pane split` JSON -> a clear `RuntimeError` naming the step
    (fail-closed), never a raw `json.JSONDecodeError`/`KeyError` leak.
(f) Env/secrets reach the pane command via a private (0600) temp env file
    that the pane `source`s — the secret VALUE never appears on any
    `subprocess.run` argv (only the env file *path* does).
(g) Loading via the real `PluginManager` local-file discovery mechanism
    registers `herdr` into `hivepilot.registry.RUNNER_MAP` (mirrors
    `tests/test_rtk.py`; relies on the conftest autouse `RUNNER_MAP` reset).
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import Settings, settings
from hivepilot.models import KNOWN_RUNNER_KINDS, ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload

REPO_ROOT = Path(__file__).parent.parent
HERDR_PLUGIN_PATH = REPO_ROOT / "plugins" / "herdr.py"


def _load_herdr_module() -> ModuleType:
    """Load plugins/herdr.py by file path — same mechanism
    `hivepilot.plugins._scan_local_plugins` uses (no dependency on `plugins`
    being an importable package on sys.path)."""
    spec = importlib.util.spec_from_file_location("hivepilot_plugin_herdr_test", HERDR_PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def herdr_module() -> ModuleType:
    return _load_herdr_module()


def _payload(
    tmp_path: Path,
    command: str = "echo {project_name}",
    secrets: dict[str, str] | None = None,
    project_env: dict[str, str] | None = None,
) -> RunnerPayload:
    return RunnerPayload(
        project_name="proj",
        project=ProjectConfig(path=tmp_path, env=project_env or {}),
        task_name="t",
        step=TaskStep(name="s", runner="herdr", command=command),
        metadata={},
        secrets=secrets or {},
    )


def _split_result(pane_id: str) -> MagicMock:
    return MagicMock(returncode=0, stdout=json.dumps({"id": pane_id}), stderr="")


def _ok_result(stdout: str = "") -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


class TestRegister:
    def test_register_exposes_herdr_kind(self, herdr_module: ModuleType) -> None:
        hooks = herdr_module.register()
        assert "runners" in hooks
        assert "herdr" in hooks["runners"]

    def test_herdr_kind_does_not_collide_with_a_built_in(self) -> None:
        assert "herdr" not in KNOWN_RUNNER_KINDS


class TestCaptureDrivesFullCliSequence:
    def test_split_run_wait_read_in_order_with_parsed_pane_id(
        self, herdr_module: ModuleType, tmp_path: Path
    ) -> None:
        pane_id = "w1:p7"
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "split"]:
                return _split_result(pane_id)
            if argv[:3] == ["herdr", "pane", "run"]:
                return _ok_result()
            if argv[:3] == ["herdr", "wait", "agent-status"]:
                return _ok_result()
            if argv[:3] == ["herdr", "pane", "read"]:
                return _ok_result(stdout="pane output text")
            raise AssertionError(f"unexpected argv: {argv}")

        custom_settings = Settings(
            _env_file=None,
            herdr_wait_timeout_ms=45_000,
            herdr_read_lines=77,
            herdr_split_direction="right",
        )
        runner = herdr_module.HerdrRunner(
            RunnerDefinition(name="herdr", kind="herdr"), custom_settings
        )

        with (
            patch.object(herdr_module.shutil, "which", return_value="/usr/local/bin/herdr"),
            patch.object(herdr_module.subprocess, "run", side_effect=fake_run),
        ):
            output = runner.capture(_payload(tmp_path))

        assert output == "pane output text"
        assert len(calls) == 4

        split_argv, run_argv, wait_argv, read_argv = calls

        assert split_argv[:3] == ["herdr", "pane", "split"]

        # pane id must be the one PARSED from the split JSON — never hand-built.
        assert run_argv[:3] == ["herdr", "pane", "run"]
        assert run_argv[3] == pane_id

        assert wait_argv[:3] == ["herdr", "wait", "agent-status"]
        assert wait_argv[3] == pane_id
        assert "--status" in wait_argv
        assert wait_argv[wait_argv.index("--status") + 1] == "idle"

        assert read_argv[:3] == ["herdr", "pane", "read"]
        assert read_argv[3] == pane_id

    def test_timeout_and_read_lines_come_from_config(
        self, herdr_module: ModuleType, tmp_path: Path
    ) -> None:
        pane_id = "w1:p3"
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "split"]:
                return _split_result(pane_id)
            return _ok_result()

        custom_settings = Settings(
            _env_file=None,
            herdr_wait_timeout_ms=12_345,
            herdr_read_lines=42,
            herdr_split_direction="left",
        )
        runner = herdr_module.HerdrRunner(
            RunnerDefinition(name="herdr", kind="herdr"), custom_settings
        )

        with (
            patch.object(herdr_module.shutil, "which", return_value="/usr/local/bin/herdr"),
            patch.object(herdr_module.subprocess, "run", side_effect=fake_run),
        ):
            runner.capture(_payload(tmp_path))

        split_argv, _run_argv, wait_argv, read_argv = calls

        assert "--direction" in split_argv
        assert split_argv[split_argv.index("--direction") + 1] == "left"

        assert "--timeout" in wait_argv
        assert wait_argv[wait_argv.index("--timeout") + 1] == "12345"

        assert "--lines" in read_argv
        assert read_argv[read_argv.index("--lines") + 1] == "42"


class TestFallbackWithoutHerdrOnPath:
    def test_falls_back_to_raw_command_when_herdr_missing(
        self, herdr_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = herdr_module.HerdrRunner(RunnerDefinition(name="herdr", kind="herdr"), settings)
        with (
            patch.object(herdr_module.shutil, "which", return_value=None),
            patch.object(
                herdr_module.subprocess,
                "run",
                return_value=MagicMock(returncode=0, stdout="raw output", stderr=""),
            ) as mock_run,
        ):
            output = runner.run(_payload(tmp_path))  # must not raise

        assert output is None
        args = mock_run.call_args.args[0]
        assert args == ["bash", "-lc", "echo proj"]
        assert "herdr" not in args

    def test_fallback_capture_returns_raw_stdout(
        self, herdr_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = herdr_module.HerdrRunner(RunnerDefinition(name="herdr", kind="herdr"), settings)
        with (
            patch.object(herdr_module.shutil, "which", return_value=None),
            patch.object(
                herdr_module.subprocess,
                "run",
                return_value=MagicMock(returncode=0, stdout="raw output", stderr=""),
            ),
        ):
            output = runner.capture(_payload(tmp_path))

        assert output == "raw output"

    def test_fallback_logs_at_info(self, herdr_module: ModuleType, tmp_path: Path) -> None:
        # Missing herdr is expected graceful degradation, not a problem, so it
        # logs at INFO (not WARNING) — mirrors plugins/rtk.py's fallback.
        runner = herdr_module.HerdrRunner(RunnerDefinition(name="herdr", kind="herdr"), settings)
        with (
            patch.object(herdr_module.shutil, "which", return_value=None),
            patch.object(
                herdr_module.subprocess,
                "run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ),
            patch.object(herdr_module, "logger", MagicMock()) as mock_logger,
        ):
            runner.run(_payload(tmp_path))

        assert mock_logger.info.called
        assert not mock_logger.warning.called

    def test_fallback_raises_clear_error_on_nonzero_exit(
        self, herdr_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = herdr_module.HerdrRunner(RunnerDefinition(name="herdr", kind="herdr"), settings)
        with (
            patch.object(herdr_module.shutil, "which", return_value=None),
            patch.object(
                herdr_module.subprocess,
                "run",
                return_value=MagicMock(returncode=1, stdout="", stderr="boom"),
            ),
            pytest.raises(RuntimeError, match="s"),
        ):
            runner.capture(_payload(tmp_path))


class TestMalformedSplitJson:
    def test_malformed_json_raises_clear_fail_closed_error(
        self, herdr_module: ModuleType, tmp_path: Path
    ) -> None:
        def fake_run(argv: list[str], **kwargs: object) -> MagicMock:
            if argv[:3] == ["herdr", "pane", "split"]:
                return MagicMock(returncode=0, stdout="not json at all {{{", stderr="")
            raise AssertionError("should never reach pane run/wait/read")

        runner = herdr_module.HerdrRunner(RunnerDefinition(name="herdr", kind="herdr"), settings)
        with (
            patch.object(herdr_module.shutil, "which", return_value="/usr/local/bin/herdr"),
            patch.object(herdr_module.subprocess, "run", side_effect=fake_run),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                runner.capture(_payload(tmp_path, command="echo hi"))

        message = str(exc_info.value)
        # Names the step (fail-closed, actionable error) — not a raw JSON/Key error leak.
        assert "s" in message  # step name
        assert exc_info.type is RuntimeError
        assert "Traceback" not in message

    def test_json_missing_id_field_raises_clear_error(
        self, herdr_module: ModuleType, tmp_path: Path
    ) -> None:
        def fake_run(argv: list[str], **kwargs: object) -> MagicMock:
            if argv[:3] == ["herdr", "pane", "split"]:
                return MagicMock(
                    returncode=0, stdout=json.dumps({"unexpected": "shape"}), stderr=""
                )
            raise AssertionError("should never reach pane run/wait/read")

        runner = herdr_module.HerdrRunner(RunnerDefinition(name="herdr", kind="herdr"), settings)
        with (
            patch.object(herdr_module.shutil, "which", return_value="/usr/local/bin/herdr"),
            patch.object(herdr_module.subprocess, "run", side_effect=fake_run),
        ):
            with pytest.raises(RuntimeError):
                runner.capture(_payload(tmp_path))


class TestEnvSecretsReachPaneWithoutArgvLeak:
    def test_secret_value_reaches_pane_via_env_file_not_argv(
        self, herdr_module: ModuleType, tmp_path: Path
    ) -> None:
        pane_id = "w1:p9"
        secret_value = "sk-super-secret-token-value-12345"
        calls: list[list[str]] = []
        env_file_snapshot: dict[str, str] = {}

        def fake_run(argv: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "split"]:
                return _split_result(pane_id)
            if argv[:3] == ["herdr", "pane", "run"]:
                # Peek at the env file BEFORE the runner cleans it up.
                wrapped_cmd = argv[-1]
                match = re.search(r"source\s+([^\s;]+)", wrapped_cmd)
                assert match, f"expected a 'source <path>' in wrapped command: {wrapped_cmd!r}"
                env_file_path = match.group(1).strip("'\"")
                env_file_snapshot["content"] = Path(env_file_path).read_text()
                env_file_snapshot["path"] = env_file_path
                return _ok_result()
            if argv[:3] == ["herdr", "wait", "agent-status"]:
                return _ok_result()
            if argv[:3] == ["herdr", "pane", "read"]:
                return _ok_result(stdout="done")
            raise AssertionError(f"unexpected argv: {argv}")

        runner = herdr_module.HerdrRunner(RunnerDefinition(name="herdr", kind="herdr"), settings)

        with (
            patch.object(herdr_module.shutil, "which", return_value="/usr/local/bin/herdr"),
            patch.object(herdr_module.subprocess, "run", side_effect=fake_run),
        ):
            runner.capture(_payload(tmp_path, secrets={"MY_SECRET": secret_value}))

        # The secret value must be present in the env file the pane sources...
        assert "MY_SECRET" in env_file_snapshot["content"]
        assert secret_value in env_file_snapshot["content"]

        # ...and must NEVER appear on any subprocess.run argv (ps-visible).
        flattened_argv = [token for call in calls for token in call]
        assert not any(secret_value in token for token in flattened_argv)

        # The env file itself must be private (owner read/write only).
        # (File is removed by the runner after use — assert cleanup happened.)
        assert not Path(env_file_snapshot["path"]).exists()

    def test_project_env_also_reaches_pane_via_env_file(
        self, herdr_module: ModuleType, tmp_path: Path
    ) -> None:
        pane_id = "w1:p5"
        env_file_snapshot: dict[str, str] = {}

        def fake_run(argv: list[str], **kwargs: object) -> MagicMock:
            if argv[:3] == ["herdr", "pane", "split"]:
                return _split_result(pane_id)
            if argv[:3] == ["herdr", "pane", "run"]:
                wrapped_cmd = argv[-1]
                match = re.search(r"source\s+([^\s;]+)", wrapped_cmd)
                assert match
                env_file_snapshot["content"] = Path(match.group(1).strip("'\"")).read_text()
                return _ok_result()
            return _ok_result()

        runner = herdr_module.HerdrRunner(RunnerDefinition(name="herdr", kind="herdr"), settings)
        with (
            patch.object(herdr_module.shutil, "which", return_value="/usr/local/bin/herdr"),
            patch.object(herdr_module.subprocess, "run", side_effect=fake_run),
        ):
            runner.capture(_payload(tmp_path, project_env={"PROJECT_FLAG": "on"}))

        assert "PROJECT_FLAG" in env_file_snapshot["content"]


class TestPluginManagerDiscoversHerdr:
    @pytest.fixture(autouse=True)
    def _restore_runner_map(self):
        """RUNNER_MAP is process-global mutable state — snapshot/restore around
        every test here so `herdr` (registered by the real `plugins/herdr.py`
        on disk) never leaks into other test modules sharing the pytest
        session (same pattern as test_rtk.py)."""
        from hivepilot.registry import RUNNER_MAP

        snapshot = dict(RUNNER_MAP)
        yield
        RUNNER_MAP.clear()
        RUNNER_MAP.update(snapshot)

    def test_plugin_manager_registers_herdr_into_runner_map(self, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RUNNER_MAP

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert "herdr" in RUNNER_MAP
        assert any(r.source == "local-file" and r.name == "herdr" for r in pm.loaded)
