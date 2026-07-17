"""
Tests for the `tmux` runner plugin (plugin-arch-overhaul PRD, Sprint 03).

`plugins/tmux.py` is a local-file plugin (see docs/v4/PLUGINS.md) that
executes each pipeline step *inside a dedicated, deterministically-named tmux
session* (`new-session -d` -> `wait-for` completion signal -> `capture-pane`
full scrollback -> `kill-session`), enabling live attach/observe. It degrades
gracefully (raw `bash -lc` execution) when the `tmux` binary isn't on PATH —
same posture as `plugins/rtk.py` / `plugins/herdr.py`.

Covers:
(a) `register()` exposes runner kind `tmux`; no collision with a built-in
    kind (`KNOWN_RUNNER_KINDS`); gated by `settings.tmux_enabled`.
(b) `health()` reflects `shutil.which("tmux")`: `ok` / `degraded` (exact
    "tmux not found; using shell fallback" detail) / `error` (never raises).
(c) Session-name determinism: `_session_name()` derives ONLY from stable
    payload identifiers (project/task/step names) — no timestamp/random —
    and sanitizes characters tmux disallows.
(d) `capture()` drives the full session lifecycle in order (new-session ->
    wait-for -> capture-pane -> kill-session) via mocked `subprocess.run`.
(e) A non-zero command exit code (read from the exit-marker file the wrapped
    command writes) raises a clear `RuntimeError`, and `kill-session` still
    runs (fail-closed cleanup).
(f) `wait-for` timing out raises a clear `RuntimeError` and still cleans up
    the session.
(g) Fallback: `shutil.which("tmux")` is None -> runs `["bash","-lc",cmd]`
    raw, no crash, logs the degradation at INFO, `health()` = degraded.
(h) Env/secrets reach the session via a private (0600) temp env file the
    wrapped command `source`s — mirrors `plugins/herdr.py`'s security
    posture (no secret value ever appears on a `subprocess.run` argv).
(i) Loading via the real `PluginManager` local-file discovery mechanism
    registers `tmux` into `hivepilot.registry.RUNNER_MAP`.
(j) A real (unmocked) tmux integration smoke test — skipped if `tmux` truly
    isn't installed on the host running this suite.
"""

from __future__ import annotations

import importlib.util
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import KNOWN_RUNNER_KINDS, ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload

REPO_ROOT = Path(__file__).parent.parent
TMUX_PLUGIN_PATH = REPO_ROOT / "plugins" / "tmux.py"


def _load_tmux_module() -> ModuleType:
    """Load plugins/tmux.py by file path — same mechanism
    `hivepilot.plugins._scan_local_plugins` uses (no dependency on `plugins`
    being an importable package on sys.path)."""
    spec = importlib.util.spec_from_file_location("hivepilot_plugin_tmux_test", TMUX_PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tmux_module() -> ModuleType:
    return _load_tmux_module()


def _payload(
    tmp_path: Path,
    command: str = "echo {project_name}",
    project_name: str = "proj",
    task_name: str = "t",
    step_name: str = "s",
    secrets: dict[str, str] | None = None,
    project_env: dict[str, str] | None = None,
) -> RunnerPayload:
    return RunnerPayload(
        project_name=project_name,
        project=ProjectConfig(path=tmp_path, env=project_env or {}),
        task_name=task_name,
        step=TaskStep(name=step_name, runner="tmux", command=command),
        metadata={},
        secrets=secrets or {},
    )


def _ok(stdout: str = "") -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _extract_exit_file(wrapped_cmd: str) -> str:
    match = re.search(r"echo \$\? > (\S+);", wrapped_cmd)
    assert match, f"expected an exit-marker redirect in wrapped command: {wrapped_cmd!r}"
    return match.group(1).strip("'\"")


def _extract_env_file(wrapped_cmd: str) -> str:
    match = re.search(r"source\s+(\S+);", wrapped_cmd)
    assert match, f"expected a 'source <path>' in wrapped command: {wrapped_cmd!r}"
    return match.group(1).strip("'\"")


class TestRegister:
    def test_register_exposes_tmux_kind(self, tmux_module: ModuleType) -> None:
        hooks = tmux_module.register()
        assert "runners" in hooks
        assert "tmux" in hooks["runners"]

    def test_tmux_kind_does_not_collide_with_a_built_in(self) -> None:
        assert "tmux" not in KNOWN_RUNNER_KINDS

    def test_register_exposes_health_check(self, tmux_module: ModuleType) -> None:
        hooks = tmux_module.register()
        assert "health" in hooks
        assert "tmux" in hooks["health"]
        assert hooks["health"]["tmux"] is tmux_module.health

    def test_register_returns_contributions_when_enabled_by_default(
        self, tmux_module: ModuleType
    ) -> None:
        assert settings.tmux_enabled is True
        hooks = tmux_module.register()
        assert hooks["runners"] == {"tmux": tmux_module.TmuxRunner}
        assert "tmux" in hooks["health"]

    def test_register_returns_empty_when_disabled(
        self, tmux_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "tmux_enabled", False, raising=False)
        assert tmux_module.register() == {}


class TestHealth:
    def test_ok_when_tmux_on_path(self, tmux_module: ModuleType) -> None:
        with patch.object(tmux_module.shutil, "which", return_value="/usr/bin/tmux"):
            result = tmux_module.health()
        assert result.status == "ok"
        assert "tmux" in result.detail

    def test_degraded_when_tmux_not_on_path(self, tmux_module: ModuleType) -> None:
        with patch.object(tmux_module.shutil, "which", return_value=None):
            result = tmux_module.health()
        assert result.status == "degraded"
        assert result.detail == "tmux not found; using shell fallback"

    def test_health_is_keyword_tolerant(self, tmux_module: ModuleType) -> None:
        with patch.object(tmux_module.shutil, "which", return_value=None):
            result = tmux_module.health(project="anything", step="x")
        assert result.status == "degraded"

    def test_health_never_raises_returns_error_type_name(self, tmux_module: ModuleType) -> None:
        with patch.object(tmux_module.shutil, "which", side_effect=RuntimeError("boom")):
            result = tmux_module.health()
        assert result.status == "error"
        assert result.detail == "RuntimeError"
        assert "boom" not in result.detail


class TestSessionNameDeterminism:
    def test_deterministic_across_calls(self, tmux_module: ModuleType, tmp_path: Path) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        payload = _payload(tmp_path)
        name1 = runner._session_name(payload)
        name2 = runner._session_name(payload)
        assert name1 == name2

    def test_no_randomness_across_separate_payload_instances(
        self, tmux_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        name1 = runner._session_name(_payload(tmp_path))
        name2 = runner._session_name(_payload(tmp_path))
        assert name1 == name2

    def test_prefix_and_stable_identifiers(self, tmux_module: ModuleType, tmp_path: Path) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        name = runner._session_name(
            _payload(tmp_path, project_name="myproj", task_name="mytask", step_name="mystep")
        )
        assert name.startswith("hivepilot-")
        assert "myproj" in name
        assert "mytask" in name
        assert "mystep" in name

    def test_sanitizes_disallowed_characters(self, tmux_module: ModuleType, tmp_path: Path) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        name = runner._session_name(
            _payload(
                tmp_path, project_name="proj:with spaces!", task_name="t/ask", step_name="s.tep"
            )
        )
        assert re.fullmatch(r"[A-Za-z0-9_-]+", name)
        assert ":" not in name
        assert " " not in name

    def test_different_steps_produce_different_names(
        self, tmux_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        name_a = runner._session_name(_payload(tmp_path, step_name="step-a"))
        name_b = runner._session_name(_payload(tmp_path, step_name="step-b"))
        assert name_a != name_b


class TestCaptureDrivesFullSessionSequence:
    def test_new_session_send_wait_capture_kill_in_order(
        self, tmux_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        payload = _payload(tmp_path, command="echo hi")
        expected_name = runner._session_name(payload)
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(argv))
            if argv[:3] == ["tmux", "new-session", "-d"]:
                return _ok()
            if argv[:3] == ["tmux", "send-keys", "-t"] and "-l" in argv:
                wrapped_cmd = argv[-1]
                exit_file = _extract_exit_file(wrapped_cmd)
                Path(exit_file).write_text("0\n")
                return _ok()
            if argv[:3] == ["tmux", "send-keys", "-t"] and argv[-1] == "Enter":
                return _ok()
            if argv[:2] == ["tmux", "wait-for"]:
                return _ok()
            if argv[:2] == ["tmux", "capture-pane"]:
                return _ok(stdout="captured session output")
            if argv[:2] == ["tmux", "kill-session"]:
                return _ok()
            raise AssertionError(f"unexpected argv: {argv}")

        with (
            patch.object(tmux_module.shutil, "which", return_value="/usr/bin/tmux"),
            patch.object(tmux_module.subprocess, "run", side_effect=fake_run),
        ):
            output = runner.capture(payload)

        assert output == "captured session output"
        assert len(calls) == 6

        new_session_argv, literal_argv, enter_argv, wait_argv, capture_argv = calls[:5]
        kill_argv = calls[-1]

        assert new_session_argv[:3] == ["tmux", "new-session", "-d"]
        assert "-s" in new_session_argv
        assert new_session_argv[new_session_argv.index("-s") + 1] == expected_name
        assert "-c" in new_session_argv
        assert new_session_argv[new_session_argv.index("-c") + 1] == str(tmp_path)

        assert literal_argv[:2] == ["tmux", "send-keys"]
        assert "-l" in literal_argv
        assert literal_argv[literal_argv.index("-t") + 1] == expected_name

        assert enter_argv[:2] == ["tmux", "send-keys"]
        assert enter_argv[-1] == "Enter"

        assert wait_argv[0:2] == ["tmux", "wait-for"]

        assert capture_argv[:2] == ["tmux", "capture-pane"]
        assert "-p" in capture_argv
        assert "-S" in capture_argv
        assert capture_argv[capture_argv.index("-S") + 1] == "-"
        assert "-t" in capture_argv
        assert capture_argv[capture_argv.index("-t") + 1] == expected_name

        assert kill_argv[:2] == ["tmux", "kill-session"]
        assert kill_argv[kill_argv.index("-t") + 1] == expected_name

    def test_wrapped_command_cds_into_project_path(
        self, tmux_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        captured: dict[str, str] = {}

        def fake_run(argv: list[str], **kwargs: object) -> MagicMock:
            if argv[:3] == ["tmux", "send-keys", "-t"] and "-l" in argv:
                wrapped_cmd = argv[-1]
                Path(_extract_exit_file(wrapped_cmd)).write_text("0\n")
                captured["wrapped_cmd"] = wrapped_cmd
                return _ok()
            return _ok()

        with (
            patch.object(tmux_module.shutil, "which", return_value="/usr/bin/tmux"),
            patch.object(tmux_module.subprocess, "run", side_effect=fake_run),
        ):
            runner.capture(_payload(tmp_path))

        expected_prefix = f"cd {shlex.quote(str(tmp_path))}; "
        assert captured["wrapped_cmd"].startswith(expected_prefix)

    def test_nonzero_exit_code_raises_runtime_error_and_still_kills_session(
        self, tmux_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        kill_called = {"value": False}

        def fake_run(argv: list[str], **kwargs: object) -> MagicMock:
            if argv[:3] == ["tmux", "new-session", "-d"]:
                return _ok()
            if argv[:3] == ["tmux", "send-keys", "-t"] and "-l" in argv:
                Path(_extract_exit_file(argv[-1])).write_text("1\n")
                return _ok()
            if argv[:3] == ["tmux", "send-keys", "-t"] and argv[-1] == "Enter":
                return _ok()
            if argv[:2] == ["tmux", "wait-for"]:
                return _ok()
            if argv[:2] == ["tmux", "capture-pane"]:
                return _ok(stdout="boom output")
            if argv[:2] == ["tmux", "kill-session"]:
                kill_called["value"] = True
                return _ok()
            raise AssertionError(f"unexpected argv: {argv}")

        with (
            patch.object(tmux_module.shutil, "which", return_value="/usr/bin/tmux"),
            patch.object(tmux_module.subprocess, "run", side_effect=fake_run),
        ):
            with pytest.raises(RuntimeError, match="s"):
                runner.capture(_payload(tmp_path))

        assert kill_called["value"] is True

    def test_new_session_failure_raises_without_wait_or_capture(
        self, tmux_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)

        def fake_run(argv: list[str], **kwargs: object) -> MagicMock:
            if argv[:3] == ["tmux", "new-session", "-d"]:
                return MagicMock(returncode=1, stdout="", stderr="duplicate session")
            if argv[:2] == ["tmux", "send-keys"]:
                raise AssertionError("send-keys must not be called after a failed new-session")
            if argv[:2] == ["tmux", "wait-for"]:
                raise AssertionError("wait-for must not be called after a failed new-session")
            if argv[:2] == ["tmux", "capture-pane"]:
                raise AssertionError("capture-pane must not be called after a failed new-session")
            return _ok()

        with (
            patch.object(tmux_module.shutil, "which", return_value="/usr/bin/tmux"),
            patch.object(tmux_module.subprocess, "run", side_effect=fake_run),
        ):
            with pytest.raises(RuntimeError):
                runner.capture(_payload(tmp_path))

    def test_wait_for_timeout_raises_and_still_kills_session(
        self, tmux_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        kill_called = {"value": False}

        def fake_run(argv: list[str], **kwargs: object) -> MagicMock:
            if argv[:3] == ["tmux", "new-session", "-d"]:
                return _ok()
            if argv[:3] == ["tmux", "send-keys", "-t"] and "-l" in argv:
                Path(_extract_exit_file(argv[-1])).write_text("0\n")
                return _ok()
            if argv[:3] == ["tmux", "send-keys", "-t"] and argv[-1] == "Enter":
                return _ok()
            if argv[:2] == ["tmux", "wait-for"]:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=1)
            if argv[:2] == ["tmux", "kill-session"]:
                kill_called["value"] = True
                return _ok()
            raise AssertionError(f"unexpected argv: {argv}")

        with (
            patch.object(tmux_module.shutil, "which", return_value="/usr/bin/tmux"),
            patch.object(tmux_module.subprocess, "run", side_effect=fake_run),
        ):
            with pytest.raises(RuntimeError, match="timed out"):
                runner.capture(_payload(tmp_path))

        assert kill_called["value"] is True


class TestFallbackWithoutTmuxOnPath:
    def test_falls_back_to_raw_command_when_tmux_missing(
        self, tmux_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        with (
            patch.object(tmux_module.shutil, "which", return_value=None),
            patch.object(
                tmux_module.subprocess,
                "run",
                return_value=MagicMock(returncode=0, stdout="raw output", stderr=""),
            ) as mock_run,
        ):
            output = runner.run(_payload(tmp_path))  # must not raise

        assert output is None
        args = mock_run.call_args.args[0]
        assert args == ["bash", "-lc", "echo proj"]
        assert "tmux" not in args

    def test_fallback_capture_returns_raw_stdout(
        self, tmux_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        with (
            patch.object(tmux_module.shutil, "which", return_value=None),
            patch.object(
                tmux_module.subprocess,
                "run",
                return_value=MagicMock(returncode=0, stdout="raw output", stderr=""),
            ),
        ):
            output = runner.capture(_payload(tmp_path))
        assert output == "raw output"

    def test_fallback_logs_at_info_not_warning(
        self, tmux_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        with (
            patch.object(tmux_module.shutil, "which", return_value=None),
            patch.object(
                tmux_module.subprocess,
                "run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ),
            patch.object(tmux_module, "logger", MagicMock()) as mock_logger,
        ):
            runner.run(_payload(tmp_path))

        assert mock_logger.info.called
        assert not mock_logger.warning.called

    def test_fallback_raises_clear_error_on_nonzero_exit(
        self, tmux_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        with (
            patch.object(tmux_module.shutil, "which", return_value=None),
            patch.object(
                tmux_module.subprocess,
                "run",
                return_value=MagicMock(returncode=1, stdout="", stderr="boom"),
            ),
            pytest.raises(RuntimeError, match="s"),
        ):
            runner.capture(_payload(tmp_path))

    def test_health_degraded_matches_fallback_trigger(self, tmux_module: ModuleType) -> None:
        with patch.object(tmux_module.shutil, "which", return_value=None):
            result = tmux_module.health()
        assert result.status == "degraded"


class TestEnvSecretsReachSessionWithoutArgvLeak:
    def test_secret_value_reaches_session_via_env_file_not_argv(
        self, tmux_module: ModuleType, tmp_path: Path
    ) -> None:
        import os

        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        secret_value = "sk-super-secret-token-value-12345"
        calls: list[list[str]] = []
        env_file_snapshot: dict[str, str] = {}

        def fake_run(argv: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(argv))
            if argv[:3] == ["tmux", "new-session", "-d"]:
                return _ok()
            if argv[:3] == ["tmux", "send-keys", "-t"] and "-l" in argv:
                wrapped_cmd = argv[-1]
                env_file_path = _extract_env_file(wrapped_cmd)
                env_file_snapshot["content"] = Path(env_file_path).read_text()
                env_file_snapshot["path"] = env_file_path
                env_file_snapshot["mode"] = oct(os.stat(env_file_path).st_mode & 0o777)
                Path(_extract_exit_file(wrapped_cmd)).write_text("0\n")
                return _ok()
            if argv[:3] == ["tmux", "send-keys", "-t"] and argv[-1] == "Enter":
                return _ok()
            if argv[:2] == ["tmux", "wait-for"]:
                return _ok()
            if argv[:2] == ["tmux", "capture-pane"]:
                return _ok(stdout="done")
            if argv[:2] == ["tmux", "kill-session"]:
                return _ok()
            raise AssertionError(f"unexpected argv: {argv}")

        with (
            patch.object(tmux_module.shutil, "which", return_value="/usr/bin/tmux"),
            patch.object(tmux_module.subprocess, "run", side_effect=fake_run),
        ):
            runner.capture(_payload(tmp_path, secrets={"MY_SECRET": secret_value}))

        assert "MY_SECRET" in env_file_snapshot["content"]
        assert secret_value in env_file_snapshot["content"]
        assert env_file_snapshot["mode"] == "0o600"

        flattened_argv = [token for call in calls for token in call]
        assert not any(secret_value in token for token in flattened_argv)
        assert not Path(env_file_snapshot["path"]).exists()

    def test_invalid_env_key_raises_clear_fail_closed_error(
        self, tmux_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        payload = _payload(tmp_path, secrets={"X;evil": "value"})
        with pytest.raises(RuntimeError, match="X;evil"):
            runner._write_env_file(payload)

    def test_nasty_value_round_trips_through_real_source_without_injection(
        self, tmux_module: ModuleType, tmp_path: Path
    ) -> None:
        nasty_value = "a'b c\n$(touch PWNED)`touch PWNED2`"
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        payload = _payload(tmp_path, secrets={"NASTY_VAR": nasty_value})

        env_file_path = runner._write_env_file(payload)
        try:
            result = subprocess.run(
                ["bash", "-c", f'source {shlex.quote(env_file_path)}; printf %s "$NASTY_VAR"'],
                cwd=str(tmp_path),
                capture_output=True,
                text=True,
                check=True,
            )
            assert result.stdout == nasty_value
            assert not (tmp_path / "PWNED").exists()
            assert not (tmp_path / "PWNED2").exists()
            assert not (Path.cwd() / "PWNED").exists()
            assert not (Path.cwd() / "PWNED2").exists()
        finally:
            runner._cleanup_env_file(env_file_path)


class TestPluginManagerDiscoversTmux:
    @pytest.fixture(autouse=True)
    def _restore_runner_map(self):
        """RUNNER_MAP is process-global mutable state — snapshot/restore
        around every test here so `tmux` (registered by the real
        `plugins/tmux.py` on disk) never leaks into other test modules
        sharing the pytest session (same pattern as test_rtk.py)."""
        from hivepilot.registry import RUNNER_MAP

        snapshot = dict(RUNNER_MAP)
        yield
        RUNNER_MAP.clear()
        RUNNER_MAP.update(snapshot)

    def test_plugin_manager_registers_tmux_into_runner_map(self, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RUNNER_MAP

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)

        pm = plugins_mod.PluginManager()

        assert "tmux" in RUNNER_MAP
        assert any(r.source == "local-file" and r.name == "tmux" for r in pm.loaded)

    def test_plugin_manager_skips_tmux_when_disabled(self, monkeypatch) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RUNNER_MAP

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "tmux_enabled", False, raising=False)
        RUNNER_MAP.pop("tmux", None)  # clean baseline (fixture restores after)

        plugins_mod.PluginManager()

        assert "tmux" not in RUNNER_MAP


class TestRealTmuxIntegration:
    """Unmocked smoke test against a real `tmux` binary — skipped outright
    when `tmux` truly isn't installed on the host running this suite (the
    monkeypatched fallback path above is what actually asserts the
    degradation behavior when `tmux` is absent)."""

    @pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed on this host")
    def test_real_tmux_session_executes_and_captures_output(
        self, tmux_module: ModuleType, tmp_path: Path
    ) -> None:
        runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
        marker = "hivepilot-tmux-integration-marker-42"
        payload = _payload(tmp_path, command=f"echo {marker}")

        output = runner.capture(payload)

        assert marker in output
