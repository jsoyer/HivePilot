"""Smoke tests for `scripts/setup-openrc.sh`.

This is a POSIX `sh` installer, not Python — so these tests do NOT import it.
Instead they exec the real script (via `sh`) against a fully overridden,
non-root-writable environment (`HIVEPILOT_INITD_DIR`/`HIVEPILOT_CONFD_DIR`/
`HIVEPILOT_LOG_DIR`/`HIVEPILOT_VENV_DIR` all pointed at a pytest `tmp_path`,
plus stubbed `rc-update`/`rc-service`/`hivepilot` binaries on `PATH`) and
assert on the files it actually generates. This validates real script
BEHAVIOR (what gets written, with what content/permissions) rather than just
"the script exits 0".

Skips gracefully if `sh` is not available (should never happen on a POSIX
host, but keeps this test harness portable).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "setup-openrc.sh"

SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None, reason="sh not available on this host")


def _make_stub_bin(bin_dir: Path, name: str, body: str = "exit 0\n") -> None:
    """Write an executable stub script `name` into `bin_dir`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text(f"#!/bin/sh\n{body}")
    stub.chmod(0o755)


def _make_fake_venv(venv_dir: Path) -> None:
    """Create a fake `$VENV/bin/hivepilot` executable stub."""
    _make_stub_bin(venv_dir / "bin", "hivepilot")


def _run_script(
    tmp_path: Path,
    extra_env: dict[str, str],
    *,
    telegram_token: bool = True,
) -> tuple[subprocess.CompletedProcess, Path, Path, Path]:
    """Run setup-openrc.sh with every output dir + binary overridden into tmp_path.

    Returns (completed_process, initd_dir, confd_dir, venv_dir).
    """
    initd_dir = tmp_path / "etc" / "init.d"
    confd_dir = tmp_path / "etc" / "conf.d"
    log_dir = tmp_path / "var" / "log" / "hivepilot"
    venv_dir = tmp_path / "opt" / "hivepilot" / "venv"
    stub_bin_dir = tmp_path / "stub-bin"

    _make_fake_venv(venv_dir)
    _make_stub_bin(stub_bin_dir, "rc-update")
    _make_stub_bin(stub_bin_dir, "rc-service")

    env = dict(os.environ)
    env["PATH"] = f"{stub_bin_dir}:{env.get('PATH', '')}"
    env["HIVEPILOT_INITD_DIR"] = str(initd_dir)
    env["HIVEPILOT_CONFD_DIR"] = str(confd_dir)
    env["HIVEPILOT_LOG_DIR"] = str(log_dir)
    env["HIVEPILOT_VENV_DIR"] = str(venv_dir)
    env["HIVEPILOT_CONFIG_REPO"] = "https://example.invalid/config-repo.git"
    env["RUN_TOKEN"] = "test-run-token-abc123"
    env["ANTHROPIC_API_KEY"] = "sk-ant-test-key"
    if telegram_token:
        env["TELEGRAM_BOT_TOKEN"] = "123456:telegram-test-token"
        env["TELEGRAM_CHAT_IDS"] = "111, 222"
    else:
        env.pop("TELEGRAM_BOT_TOKEN", None)
        env.pop("TELEGRAM_CHAT_IDS", None)
    env.update(extra_env)

    proc = subprocess.run(
        [SH, str(SCRIPT_PATH)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc, initd_dir, confd_dir, venv_dir


# ---------------------------------------------------------------------------
# Syntax
# ---------------------------------------------------------------------------


def test_script_exists_and_is_executable_shell():
    assert SCRIPT_PATH.is_file(), f"missing {SCRIPT_PATH}"
    content = SCRIPT_PATH.read_text()
    assert content.startswith("#!/bin/sh"), "must be POSIX sh, not bash"
    assert "set -eu" in content


def test_sh_syntax_check_passes():
    """`sh -n` — pure syntax check, catches bashisms/typos without executing."""
    proc = subprocess.run(
        [SH, "-n", str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# Behavior: full run (telegram enabled)
# ---------------------------------------------------------------------------


def test_full_run_succeeds_and_writes_all_three_services(tmp_path):
    proc, initd_dir, confd_dir, _venv = _run_script(tmp_path, {}, telegram_token=True)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

    for svc in ("hivepilot-api", "hivepilot-scheduler", "hivepilot-telegram"):
        assert (initd_dir / svc).is_file(), f"missing init.d/{svc}\n{proc.stdout}\n{proc.stderr}"
        assert (confd_dir / svc).is_file(), f"missing conf.d/{svc}\n{proc.stdout}\n{proc.stderr}"


def test_initd_scripts_are_executable(tmp_path):
    proc, initd_dir, _confd, _venv = _run_script(tmp_path, {}, telegram_token=True)
    assert proc.returncode == 0, proc.stderr
    for svc in ("hivepilot-api", "hivepilot-scheduler", "hivepilot-telegram"):
        mode = stat.S_IMODE((initd_dir / svc).stat().st_mode)
        assert mode & stat.S_IXUSR, f"{svc} init.d script is not executable"


def test_conf_d_files_are_chmod_600(tmp_path):
    proc, _initd, confd_dir, _venv = _run_script(tmp_path, {}, telegram_token=True)
    assert proc.returncode == 0, proc.stderr
    for svc in ("hivepilot-api", "hivepilot-scheduler", "hivepilot-telegram"):
        mode = stat.S_IMODE((confd_dir / svc).stat().st_mode)
        assert mode == 0o600, f"{svc} conf.d perms are {oct(mode)}, expected 0o600"


def test_scheduler_conf_has_api_token_others_do_not(tmp_path):
    proc, _initd, confd_dir, _venv = _run_script(tmp_path, {}, telegram_token=True)
    assert proc.returncode == 0, proc.stderr

    scheduler_conf = (confd_dir / "hivepilot-scheduler").read_text()
    api_conf = (confd_dir / "hivepilot-api").read_text()
    telegram_conf = (confd_dir / "hivepilot-telegram").read_text()

    assert "HIVEPILOT_API_TOKEN" in scheduler_conf
    assert "test-run-token-abc123" in scheduler_conf
    assert "HIVEPILOT_API_TOKEN" not in api_conf
    assert "HIVEPILOT_API_TOKEN" not in telegram_conf


def test_api_conf_enables_webui_scheduler_and_telegram_do_not(tmp_path):
    proc, _initd, confd_dir, _venv = _run_script(tmp_path, {}, telegram_token=True)
    assert proc.returncode == 0, proc.stderr

    api_conf = (confd_dir / "hivepilot-api").read_text()
    scheduler_conf = (confd_dir / "hivepilot-scheduler").read_text()
    telegram_conf = (confd_dir / "hivepilot-telegram").read_text()

    assert "HIVEPILOT_ENABLE_WEBUI" in api_conf
    assert "true" in api_conf.split("HIVEPILOT_ENABLE_WEBUI", 1)[1].splitlines()[0]
    assert "HIVEPILOT_ENABLE_WEBUI" not in scheduler_conf
    assert "HIVEPILOT_ENABLE_WEBUI" not in telegram_conf


def test_telegram_conf_has_bot_token_and_chat_ids(tmp_path):
    proc, _initd, confd_dir, _venv = _run_script(tmp_path, {}, telegram_token=True)
    assert proc.returncode == 0, proc.stderr

    telegram_conf = (confd_dir / "hivepilot-telegram").read_text()
    assert "HIVEPILOT_TELEGRAM_BOT_TOKEN" in telegram_conf
    assert "123456:telegram-test-token" in telegram_conf
    assert "HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS" in telegram_conf
    # HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS maps a pydantic-settings list[int]
    # field, which parses its env value as STRICT JSON -- a bare/CSV value
    # (e.g. "123456" or "123,456") makes `Settings()` raise at import time.
    # Spaces must be stripped and the value wrapped in a real JSON array.
    assert "[111,222]" in telegram_conf
    allowed_line = next(
        line for line in telegram_conf.splitlines() if "HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS" in line
    )
    assert "[111,222]" in allowed_line
    assert "HIVEPILOT_TELEGRAM_NOTIFICATION_CHAT_ID" in telegram_conf


def test_telegram_conf_omits_chat_id_lines_when_no_ids_given(tmp_path):
    """No chat ids provided -> both ALLOWED_CHAT_IDS and NOTIFICATION_CHAT_ID
    lines must be omitted entirely (not an explicit "[]"/empty value) so
    HivePilot falls back to its own open-whitelist default rather than the
    script emitting a value at all."""
    proc, _initd, confd_dir, _venv = _run_script(
        tmp_path, {"TELEGRAM_CHAT_IDS": ""}, telegram_token=True
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

    telegram_conf = (confd_dir / "hivepilot-telegram").read_text()
    assert "HIVEPILOT_TELEGRAM_BOT_TOKEN" in telegram_conf
    assert "HIVEPILOT_TELEGRAM_ALLOWED_CHAT_IDS" not in telegram_conf
    assert "HIVEPILOT_TELEGRAM_NOTIFICATION_CHAT_ID" not in telegram_conf


def test_all_conf_d_files_export_path_with_local_bin(tmp_path):
    proc, _initd, confd_dir, venv_dir = _run_script(tmp_path, {}, telegram_token=True)
    assert proc.returncode == 0, proc.stderr

    for svc in ("hivepilot-api", "hivepilot-scheduler", "hivepilot-telegram"):
        conf = (confd_dir / svc).read_text()
        assert "/root/.local/bin" in conf
        assert str(venv_dir) in conf
        assert "HOME" in conf


def test_all_conf_d_files_export_anthropic_key_when_provided(tmp_path):
    proc, _initd, confd_dir, _venv = _run_script(tmp_path, {}, telegram_token=True)
    assert proc.returncode == 0, proc.stderr

    for svc in ("hivepilot-api", "hivepilot-scheduler", "hivepilot-telegram"):
        conf = (confd_dir / svc).read_text()
        assert "ANTHROPIC_API_KEY" in conf
        assert "sk-ant-test-key" in conf


# ---------------------------------------------------------------------------
# Behavior: telegram optional
# ---------------------------------------------------------------------------


def test_telegram_service_skipped_entirely_when_no_bot_token(tmp_path):
    proc, initd_dir, confd_dir, _venv = _run_script(tmp_path, {}, telegram_token=False)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

    assert not (initd_dir / "hivepilot-telegram").exists()
    assert not (confd_dir / "hivepilot-telegram").exists()
    # the two mandatory services must still be written
    assert (initd_dir / "hivepilot-api").is_file()
    assert (initd_dir / "hivepilot-scheduler").is_file()


def test_anthropic_key_omitted_when_not_provided(tmp_path):
    proc, _initd, confd_dir, _venv = _run_script(
        tmp_path, {"ANTHROPIC_API_KEY": ""}, telegram_token=False
    )
    assert proc.returncode == 0, proc.stderr
    api_conf = (confd_dir / "hivepilot-api").read_text()
    assert "ANTHROPIC_API_KEY" not in api_conf


# ---------------------------------------------------------------------------
# Fail-closed: RUN_TOKEN is required
# ---------------------------------------------------------------------------


def test_missing_run_token_fails_closed(tmp_path):
    proc, _initd, confd_dir, _venv = _run_script(tmp_path, {"RUN_TOKEN": ""}, telegram_token=False)
    assert proc.returncode != 0, "must refuse to proceed without a RUN_TOKEN"
    assert not (confd_dir / "hivepilot-scheduler").exists()


def test_missing_hivepilot_binary_fails_closed(tmp_path):
    initd_dir = tmp_path / "etc" / "init.d"
    confd_dir = tmp_path / "etc" / "conf.d"
    log_dir = tmp_path / "var" / "log" / "hivepilot"
    empty_venv_dir = tmp_path / "no-hivepilot-here"
    stub_bin_dir = tmp_path / "stub-bin"
    _make_stub_bin(stub_bin_dir, "rc-update")
    _make_stub_bin(stub_bin_dir, "rc-service")

    env = dict(os.environ)
    env["PATH"] = f"{stub_bin_dir}:{env.get('PATH', '')}"
    env["HIVEPILOT_INITD_DIR"] = str(initd_dir)
    env["HIVEPILOT_CONFD_DIR"] = str(confd_dir)
    env["HIVEPILOT_LOG_DIR"] = str(log_dir)
    env["HIVEPILOT_VENV_DIR"] = str(empty_venv_dir)
    env["RUN_TOKEN"] = "x"
    env.pop("TELEGRAM_BOT_TOKEN", None)

    proc = subprocess.run(
        [SH, str(SCRIPT_PATH)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode != 0
    assert not confd_dir.exists() or not any(confd_dir.iterdir())


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_rerun_is_idempotent_and_rotates_token(tmp_path):
    proc1, initd_dir, confd_dir, venv_dir = _run_script(tmp_path, {}, telegram_token=True)
    assert proc1.returncode == 0, proc1.stderr

    # second run with a rotated RUN_TOKEN should overwrite cleanly
    env2 = {"RUN_TOKEN": "rotated-token-xyz"}
    initd_dir2 = tmp_path / "etc" / "init.d"
    confd_dir2 = tmp_path / "etc" / "conf.d"
    stub_bin_dir = tmp_path / "stub-bin"

    env = dict(os.environ)
    env["PATH"] = f"{stub_bin_dir}:{env.get('PATH', '')}"
    env["HIVEPILOT_INITD_DIR"] = str(initd_dir2)
    env["HIVEPILOT_CONFD_DIR"] = str(confd_dir2)
    env["HIVEPILOT_LOG_DIR"] = str(tmp_path / "var" / "log" / "hivepilot")
    env["HIVEPILOT_VENV_DIR"] = str(venv_dir)
    env["HIVEPILOT_CONFIG_REPO"] = "https://example.invalid/config-repo.git"
    env["ANTHROPIC_API_KEY"] = "sk-ant-test-key"
    env["TELEGRAM_BOT_TOKEN"] = "123456:telegram-test-token"
    env["TELEGRAM_CHAT_IDS"] = "111, 222"
    env.update(env2)

    proc2 = subprocess.run(
        [SH, str(SCRIPT_PATH)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc2.returncode == 0, proc2.stderr
    scheduler_conf = (confd_dir / "hivepilot-scheduler").read_text()
    assert "rotated-token-xyz" in scheduler_conf
    assert "test-run-token-abc123" not in scheduler_conf
    mode = stat.S_IMODE((confd_dir / "hivepilot-scheduler").stat().st_mode)
    assert mode == 0o600
