"""Tests for `hivepilot.services.update_service` -- the pure helper module
backing `hivepilot self-update` (spec construction + the two subprocess
invocations: pip install, best-effort service restart).

See `tests/test_cli_self_update.py` for the CLI-layer wiring tests (this
module's own security/behavior matrix lives here, mirroring the
`test_agent_install.py` / `test_cli_agents.py` split).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from hivepilot.services.update_service import (
    KNOWN_SERVICES,
    build_update_spec,
    mask_url_credentials,
    restart_services,
    run_self_update,
)

# ---------------------------------------------------------------------------
# build_update_spec -- pure function
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
# run_self_update -- venv targeting
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
    assert "-U" not in reinstall_argv
    assert "-U" not in deps_argv


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
    assert result.stderr == "boom: could not resolve ref"
    mock_run.assert_called_once()
    argv = mock_run.call_args.args[0]
    assert "--force-reinstall" in argv
    assert "--no-deps" in argv


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
# restart_services
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


def test_known_services_is_a_plain_nonempty_list_of_names() -> None:
    assert isinstance(KNOWN_SERVICES, list)
    assert all(isinstance(name, str) and name.startswith("hivepilot-") for name in KNOWN_SERVICES)
    assert len(KNOWN_SERVICES) >= 1


# ---------------------------------------------------------------------------
# mask_url_credentials -- credential redaction for echoed repo URLs
# ---------------------------------------------------------------------------


def test_mask_url_credentials_strips_userinfo_from_git_spec() -> None:
    spec = "hivepilot[api] @ git+https://x-access-token:SECRET@github.com/o/r.git@main"
    masked = mask_url_credentials(spec)
    assert "SECRET" not in masked
    assert "***@github.com" in masked


def test_mask_url_credentials_leaves_plain_urls_untouched() -> None:
    spec = "hivepilot[api,notifications] @ git+https://github.com/jsoyer/HivePilot.git@main"
    assert mask_url_credentials(spec) == spec


def test_mask_url_credentials_masks_multiple_occurrences() -> None:
    text = (
        "Cloning into 'HivePilot'...\n"
        "remote: https://x-access-token:TOK1@github.com/o/r.git\n"
        "fatal: could not read Username for 'https://x-access-token:TOK1@github.com'"
    )
    masked = mask_url_credentials(text)
    assert "TOK1" not in masked
    assert masked.count("***@") == 2
