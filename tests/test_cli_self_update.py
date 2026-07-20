"""Tests for `hivepilot self-update`.

Covers both the pure helper module (`hivepilot.services.update_service`) and
the thin CLI wrapper (`hivepilot.cli.self_update`). HivePilot is not on PyPI
-- it installs from git -- so this command must ALWAYS target THIS process's
venv interpreter (`sys.executable`) rather than the system Python, avoiding
PEP 668's "externally-managed-environment" guard without ever needing
`--break-system-packages`. It is also install-mutating, so it must
confirm-then-run (mirroring `hivepilot agents install`'s UX) unless `--yes`
is passed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub out optional heavy dependencies before importing hivepilot.cli -- same
# approach as tests/test_cli.py / tests/test_cli_agents.py.
# ---------------------------------------------------------------------------

_STUBS = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
    "langchain_openai",
    "openai",
    "boto3",
    "docker",
    "telegram",
    "telegram.ext",
    "fastapi",
    "fastapi.responses",
    "fastapi.security",
    "uvicorn",
    "textual",
    "slack_bolt",
    "slack_bolt.adapter",
    "slack_bolt.adapter.fastapi",
    "slack_bolt.adapter.socket_mode",
    "discord",
    "PyNaCl",
    "nacl",
    "nacl.exceptions",
    "nacl.signing",
]

import importlib  # noqa: E402

for _mod in _STUBS:
    if _mod in sys.modules:
        continue
    try:
        importlib.import_module(_mod)
    except Exception:
        sys.modules[_mod] = MagicMock()

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from hivepilot.cli import app  # noqa: E402
from hivepilot.services.update_service import (  # noqa: E402
    KNOWN_SERVICES,
    build_update_spec,
    restart_services,
    run_self_update,
)

# ---------------------------------------------------------------------------
# hivepilot.services.update_service.build_update_spec -- pure function
# ---------------------------------------------------------------------------


def test_build_update_spec_formats_git_url_with_extras_and_ref() -> None:
    spec = build_update_spec(
        "https://github.com/jsoyer/HivePilot.git", "v1.2.3", "api,notifications,webui"
    )
    assert spec == (
        "hivepilot[api,notifications,webui] @ git+https://github.com/jsoyer/HivePilot.git@v1.2.3"
    )


def test_build_update_spec_uses_defaults_shape() -> None:
    spec = build_update_spec("https://github.com/jsoyer/HivePilot.git", "main", "api,notifications")
    assert spec == "hivepilot[api,notifications] @ git+https://github.com/jsoyer/HivePilot.git@main"


# ---------------------------------------------------------------------------
# hivepilot.services.update_service.run_self_update -- venv targeting
# ---------------------------------------------------------------------------


def test_run_self_update_force_reinstalls_code_then_resolves_new_deps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: a moving `main` whose HEAD commit changed but whose
    `pyproject.toml` version string did NOT (e.g. still `0.2.0`) makes plain
    `pip install -U <spec>` a silent no-op -- pip resolves the new commit,
    sees the same version already satisfied, and reports "Requirement
    already satisfied" without reinstalling. `run_self_update` must instead
    force-reinstall the package itself (`--force-reinstall --no-deps`, so
    the new code is ALWAYS pulled down regardless of the version string),
    followed by a plain resolve to pick up any newly added dependency."""
    mock_run = MagicMock(
        side_effect=[
            subprocess.CompletedProcess(args=[], returncode=0, stdout="reinstalled\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="deps ok\n", stderr=""),
        ]
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    spec = "hivepilot[api,notifications] @ git+https://github.com/jsoyer/HivePilot.git@main"
    result = run_self_update(spec)

    assert result.returncode == 0
    assert result.stdout == "reinstalled\ndeps ok\n"
    assert mock_run.call_count == 2

    reinstall_argv = mock_run.call_args_list[0].args[0]
    deps_argv = mock_run.call_args_list[1].args[0]

    assert reinstall_argv == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        "--no-deps",
        "--no-cache-dir",
        spec,
    ]
    assert deps_argv == [sys.executable, "-m", "pip", "install", "--no-cache-dir", spec]
    assert "--break-system-packages" not in reinstall_argv
    assert "--break-system-packages" not in deps_argv


def test_run_self_update_skips_dep_resolve_when_force_reinstall_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="boom: could not resolve ref"
        )
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    result = run_self_update("hivepilot[api] @ git+https://example.com/repo.git@main")

    assert result.returncode == 1
    mock_run.assert_called_once()


def test_run_self_update_accepts_explicit_python_override(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_run = MagicMock(
        side_effect=[
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    run_self_update(
        "hivepilot[api] @ git+https://example.com/repo.git@main", python="/other/python"
    )
    assert mock_run.call_count == 2
    for call in mock_run.call_args_list:
        assert call.args[0][0] == "/other/python"


# ---------------------------------------------------------------------------
# hivepilot.services.update_service.restart_services
# ---------------------------------------------------------------------------


def test_restart_services_prefers_rc_service_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    def _which(name: str):
        return f"/sbin/{name}" if name == "rc-service" else None

    monkeypatch.setattr(shutil, "which", _which)
    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    restarted = restart_services(["hivepilot-api", "hivepilot-scheduler"])

    assert restarted == ["hivepilot-api", "hivepilot-scheduler"]
    called_argvs = [c.args[0] for c in mock_run.call_args_list]
    assert ["rc-service", "hivepilot-api", "restart"] in called_argvs
    assert ["rc-service", "hivepilot-scheduler", "restart"] in called_argvs


def test_restart_services_falls_back_to_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    def _which(name: str):
        return f"/bin/{name}" if name == "systemctl" else None

    monkeypatch.setattr(shutil, "which", _which)
    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    restarted = restart_services(["hivepilot-api"])

    assert restarted == ["hivepilot-api"]
    mock_run.assert_called_once_with(
        ["systemctl", "restart", "hivepilot-api"], check=False, capture_output=True, text=True
    )


def test_restart_services_skips_missing_service_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _which(name: str):
        return f"/sbin/{name}" if name == "rc-service" else None

    monkeypatch.setattr(shutil, "which", _which)

    def _run(argv, **kwargs):
        # "hivepilot-telegram" doesn't exist under this init system.
        rc = 1 if "hivepilot-telegram" in argv else 0
        return subprocess.CompletedProcess(args=argv, returncode=rc, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)

    restarted = restart_services(["hivepilot-api", "hivepilot-telegram"])

    assert restarted == ["hivepilot-api"]


def test_restart_services_returns_empty_when_no_init_system_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)

    restarted = restart_services(KNOWN_SERVICES)

    assert restarted == []
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# `hivepilot self-update` CLI -- version print
# ---------------------------------------------------------------------------


def test_self_update_prints_current_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.metadata.version", lambda name: "9.9.9")
    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    runner = CliRunner()
    result = runner.invoke(app, ["self-update", "--yes"])

    assert result.exit_code == 0, result.output
    assert "9.9.9" in result.output


# ---------------------------------------------------------------------------
# `hivepilot self-update` CLI -- pip invocation + defaults
# ---------------------------------------------------------------------------


def test_self_update_yes_invokes_pip_with_default_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1.0.0")
    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    runner = CliRunner()
    result = runner.invoke(app, ["self-update", "--yes"])

    assert result.exit_code == 0, result.output
    # Two pip invocations: force-reinstall the code (no-deps), then a plain
    # resolve for any newly added dependency -- see run_self_update.
    assert mock_run.call_count == 2
    reinstall_argv = mock_run.call_args_list[0].args[0]
    deps_argv = mock_run.call_args_list[1].args[0]

    assert reinstall_argv[0] == sys.executable
    assert reinstall_argv[1:5] == ["-m", "pip", "install", "--force-reinstall"]
    assert "--no-deps" in reinstall_argv
    assert "--no-cache-dir" in reinstall_argv
    assert "--break-system-packages" not in reinstall_argv

    assert deps_argv[0] == sys.executable
    assert deps_argv[1:4] == ["-m", "pip", "install"]
    assert "--force-reinstall" not in deps_argv
    assert "--no-deps" not in deps_argv
    assert "--no-cache-dir" in deps_argv
    assert "--break-system-packages" not in deps_argv

    for argv in (reinstall_argv, deps_argv):
        spec_arg = argv[-1]
        assert spec_arg.startswith("hivepilot[api,notifications] @ git+")
        assert spec_arg.endswith("@main")


def test_self_update_nonzero_pip_exit_surfaces_stderr_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1.0.0")
    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="boom: could not resolve ref"
        )
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    runner = CliRunner()
    result = runner.invoke(app, ["self-update", "--yes"])

    assert result.exit_code == 1
    assert "boom: could not resolve ref" in result.output


def test_self_update_custom_ref_extras_repo_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1.0.0")
    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "self-update",
            "--yes",
            "--ref",
            "v2.0.0",
            "--extras",
            "api,notifications,webui",
            "--repo",
            "https://example.com/fork.git",
        ],
    )

    assert result.exit_code == 0, result.output
    argv = mock_run.call_args.args[0]
    spec_arg = argv[-1]
    assert (
        spec_arg == "hivepilot[api,notifications,webui] @ git+https://example.com/fork.git@v2.0.0"
    )


# ---------------------------------------------------------------------------
# `hivepilot self-update` CLI -- never echoes credentials embedded in --repo
# ---------------------------------------------------------------------------


def test_self_update_never_echoes_token_embedded_in_repo_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `--repo` (or HIVEPILOT_UPDATE_REPO) pointed at a private fork may embed
    a credential, e.g. `https://x-access-token:SECRETTOKEN@github.com/o/r.git`.
    Neither the "Will install: <spec>" echo NOR pip's own captured
    stdout/stderr (pip logs the exact clone URL, credentials included, when
    resolving a `git+` requirement) may ever surface that token."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1.0.0")
    token_repo = "https://x-access-token:SECRETTOKEN@github.com/o/private-fork.git"
    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"Cloning into 'HivePilot'...\nRunning command git clone {token_repo}\n",
            stderr="",
        )
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    runner = CliRunner()
    result = runner.invoke(app, ["self-update", "--yes", "--repo", token_repo])

    assert result.exit_code == 0, result.output
    assert "SECRETTOKEN" not in result.output
    assert "***@github.com" in result.output
    # The subprocess call itself must still receive the real, unmasked repo
    # URL -- masking is output-only, pip must still be able to authenticate.
    argv = mock_run.call_args.args[0]
    assert "SECRETTOKEN" in argv[-1]


def test_self_update_never_echoes_token_on_pip_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same guarantee on the failure path: stderr surfaced on a nonzero pip
    exit must also be masked before it's echoed."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1.0.0")
    token_repo = "https://x-access-token:SECRETTOKEN@github.com/o/private-fork.git"
    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"fatal: could not read Username for '{token_repo}': terminal prompts disabled",
        )
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    runner = CliRunner()
    result = runner.invoke(app, ["self-update", "--yes", "--repo", token_repo])

    assert result.exit_code == 1
    assert "SECRETTOKEN" not in result.output
    assert "***@github.com" in result.output


# ---------------------------------------------------------------------------
# `hivepilot self-update` CLI -- confirm-then-run
# ---------------------------------------------------------------------------


def test_self_update_without_yes_aborts_on_no_without_calling_pip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1.0.0")
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)

    with patch("typer.confirm", return_value=False) as mock_confirm:
        runner = CliRunner()
        result = runner.invoke(app, ["self-update"])

    assert result.exit_code == 0, result.output
    mock_confirm.assert_called_once()
    mock_run.assert_not_called()


def test_self_update_without_yes_proceeds_on_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1.0.0")
    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    with patch("typer.confirm", return_value=True):
        runner = CliRunner()
        result = runner.invoke(app, ["self-update"])

    assert result.exit_code == 0, result.output
    # Both the force-reinstall and the follow-up dep-resolve pip calls run.
    assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# `hivepilot self-update` CLI -- step 1 failure skips step 2 (deps resolve)
# ---------------------------------------------------------------------------


def test_self_update_force_reinstall_failure_skips_dep_resolve_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the force-reinstall (step 1) fails, the CLI must exit 1 without
    ever issuing the follow-up plain-resolve (step 2) pip call."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1.0.0")
    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="boom: could not resolve ref"
        )
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    runner = CliRunner()
    result = runner.invoke(app, ["self-update", "--yes"])

    assert result.exit_code == 1
    assert "boom: could not resolve ref" in result.output
    mock_run.assert_called_once()
    argv = mock_run.call_args.args[0]
    assert "--force-reinstall" in argv
    assert "--no-deps" in argv


def test_self_update_echo_explains_force_reinstall_for_moving_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: an operator must understand WHY hivepilot is re-downloaded
    even when `Current version` looks unchanged -- the version string alone
    does not reflect a moving git ref's HEAD commit."""
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1.0.0")
    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    runner = CliRunner()
    result = runner.invoke(app, ["self-update", "--yes"])

    assert result.exit_code == 0, result.output
    assert "force-reinstalling" in result.output.lower()


# ---------------------------------------------------------------------------
# `hivepilot self-update --restart`
# ---------------------------------------------------------------------------


def test_self_update_restart_attempts_known_services_with_rc_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1.0.0")

    def _which(name: str):
        return f"/sbin/{name}" if name == "rc-service" else None

    monkeypatch.setattr(shutil, "which", _which)

    def _run(argv, **kwargs):
        if argv[:2] == ["rc-service", "hivepilot-telegram"]:
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)

    runner = CliRunner()
    result = runner.invoke(app, ["self-update", "--yes", "--restart"])

    assert result.exit_code == 0, result.output
    assert "hivepilot-api" in result.output
    assert "hivepilot-telegram" not in result.output.split("Restarted services:")[-1].split("\n")[0]


def test_self_update_restart_no_init_system_prints_manual_notice_and_exits_0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1.0.0")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    runner = CliRunner()
    result = runner.invoke(app, ["self-update", "--yes", "--restart"])

    assert result.exit_code == 0, result.output
    assert "no init system detected" in result.output.lower()
    # The self-update pip calls (force-reinstall + dep resolve) are the only
    # subprocess.run invocations -- restart never shells out when no init
    # system was found.
    assert mock_run.call_count == 2
