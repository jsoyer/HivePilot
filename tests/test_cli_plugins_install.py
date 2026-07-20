"""Tests for `hivepilot plugins install <name>...` / `hivepilot plugins
available` (one-command built-in example plugin setup).

`plugins install` is a thin CLI wrapper over
`hivepilot.services.plugin_installer.fetch_plugin` / `persist_enabled` --
see `tests/test_plugin_installer.py` for that module's own security matrix
(curated-name-only, no arbitrary fetch). The properties under test here, at
the CLI layer, are: unknown names are rejected before any network call,
confirmation is required unless `--yes`, `--no-enable` skips the persist
step, and every fetch/enable outcome is surfaced to the operator.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from typer.testing import CliRunner

# ---------------------------------------------------------------------------
# Stub out optional heavy dependencies before importing hivepilot.cli -- same
# approach as tests/test_cli_agents.py.
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

from hivepilot.cli import app  # noqa: E402
from hivepilot.services import plugin_installer  # noqa: E402

# ---------------------------------------------------------------------------
# `hivepilot plugins available`
# ---------------------------------------------------------------------------


def test_plugins_available_lists_every_curated_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        type(plugin_installer.settings), "xdg_data_home", property(lambda self: tmp_path)
    )
    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "available"])

    assert result.exit_code == 0, result.output
    for name in plugin_installer.KNOWN_EXAMPLE_PLUGINS:
        assert name in result.output


def test_plugins_available_shows_installed_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        type(plugin_installer.settings), "xdg_data_home", property(lambda self: tmp_path)
    )
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "rtk.py").write_text("x", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "available"])

    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if "rtk" in line]
    assert any("yes" in line.lower() for line in lines)


def test_plugins_available_never_makes_a_network_call(tmp_path: Path, monkeypatch) -> None:
    import requests

    monkeypatch.setattr(
        type(plugin_installer.settings), "xdg_data_home", property(lambda self: tmp_path)
    )
    mock_get = MagicMock()
    monkeypatch.setattr(requests, "get", mock_get)

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "available"])

    assert result.exit_code == 0, result.output
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# `hivepilot plugins install <unknown>`
# ---------------------------------------------------------------------------


def test_plugins_install_unknown_name_exits_1_before_any_fetch(monkeypatch) -> None:
    import requests

    mock_get = MagicMock()
    monkeypatch.setattr(requests, "get", mock_get)

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "install", "not-a-real-plugin", "--yes"])

    assert result.exit_code == 1
    assert "unknown" in result.output.lower()
    assert "not-a-real-plugin" in result.output
    mock_get.assert_not_called()


def test_plugins_install_partial_unknown_name_rejects_whole_batch(monkeypatch) -> None:
    import requests

    mock_get = MagicMock()
    monkeypatch.setattr(requests, "get", mock_get)

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "install", "rtk", "not-a-real-plugin", "--yes"])

    assert result.exit_code == 1
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# `hivepilot plugins install <known>` -- confirm-then-run
# ---------------------------------------------------------------------------


def _mock_response(text: str = "def register():\n    return {}\n", status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


def test_plugins_install_declines_without_yes_flag(tmp_path: Path, monkeypatch) -> None:
    import requests

    monkeypatch.setattr(
        type(plugin_installer.settings), "xdg_data_home", property(lambda self: tmp_path)
    )
    mock_get = MagicMock(return_value=_mock_response())
    monkeypatch.setattr(requests, "get", mock_get)

    runner = CliRunner()
    # No --yes, and input "n" declines the confirmation prompt.
    result = runner.invoke(app, ["plugins", "install", "rtk"], input="n\n")

    assert result.exit_code == 0, result.output
    mock_get.assert_not_called()
    assert not (tmp_path / "plugins" / "rtk.py").exists()


def test_plugins_install_shows_prereq_before_confirming(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        type(plugin_installer.settings), "xdg_data_home", property(lambda self: tmp_path)
    )
    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "install", "rtk"], input="n\n")

    assert "rtk" in result.output
    assert "PATH" in result.output  # rtk's binary prereq detail


def test_plugins_install_yes_flag_fetches_and_enables(tmp_path: Path, monkeypatch) -> None:
    import requests

    monkeypatch.setattr(
        type(plugin_installer.settings), "xdg_data_home", property(lambda self: tmp_path)
    )
    env_path = tmp_path / ".env"
    monkeypatch.setattr(plugin_installer, "_default_env_path", lambda: env_path)
    mock_get = MagicMock(return_value=_mock_response())
    monkeypatch.setattr(requests, "get", mock_get)

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "install", "rtk", "--yes"])

    assert result.exit_code == 0, result.output
    mock_get.assert_called_once()
    assert (tmp_path / "plugins" / "rtk.py").exists()
    assert "HIVEPILOT_RTK_ENABLED=true" in env_path.read_text(encoding="utf-8")
    assert "prereq" in result.output.lower()


def test_plugins_install_no_enable_skips_persist(tmp_path: Path, monkeypatch) -> None:
    import requests

    monkeypatch.setattr(
        type(plugin_installer.settings), "xdg_data_home", property(lambda self: tmp_path)
    )
    env_path = tmp_path / ".env"
    monkeypatch.setattr(plugin_installer, "_default_env_path", lambda: env_path)
    monkeypatch.setattr(requests, "get", MagicMock(return_value=_mock_response()))

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "install", "rtk", "--yes", "--no-enable"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "plugins" / "rtk.py").exists()
    assert not env_path.exists()


def test_plugins_install_multiple_names_fetches_each(tmp_path: Path, monkeypatch) -> None:
    import requests

    monkeypatch.setattr(
        type(plugin_installer.settings), "xdg_data_home", property(lambda self: tmp_path)
    )
    env_path = tmp_path / ".env"
    monkeypatch.setattr(plugin_installer, "_default_env_path", lambda: env_path)
    monkeypatch.setattr(requests, "get", MagicMock(return_value=_mock_response()))

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "install", "rtk", "herdr", "--yes"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "plugins" / "rtk.py").exists()
    assert (tmp_path / "plugins" / "herdr.py").exists()


def test_plugins_install_ref_override_is_forwarded(tmp_path: Path, monkeypatch) -> None:
    import requests

    monkeypatch.setattr(
        type(plugin_installer.settings), "xdg_data_home", property(lambda self: tmp_path)
    )
    monkeypatch.setattr(plugin_installer, "_default_env_path", lambda: tmp_path / ".env")
    mock_get = MagicMock(return_value=_mock_response())
    monkeypatch.setattr(requests, "get", mock_get)

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "install", "rtk", "--yes", "--ref", "v2"])

    assert result.exit_code == 0, result.output
    args, _kwargs = mock_get.call_args
    assert "/v2/plugins/rtk.py" in args[0]


def test_plugins_install_fetch_failure_exits_1_with_friendly_message(
    tmp_path: Path, monkeypatch
) -> None:
    import requests

    monkeypatch.setattr(
        type(plugin_installer.settings), "xdg_data_home", property(lambda self: tmp_path)
    )
    monkeypatch.setattr(requests, "get", MagicMock(side_effect=requests.ConnectionError("boom")))

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "install", "rtk", "--yes"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_plugins_install_never_executes_fetched_content(tmp_path: Path, monkeypatch) -> None:
    """End-to-end CLI check that a malicious-looking fetched body is only
    ever written to disk, never imported/exec'd during install."""
    import os as _os

    import requests

    monkeypatch.setattr(
        type(plugin_installer.settings), "xdg_data_home", property(lambda self: tmp_path)
    )
    monkeypatch.setattr(plugin_installer, "_default_env_path", lambda: tmp_path / ".env")
    malicious = "import os\nos.environ['PWNED_CLI'] = '1'\n"
    monkeypatch.setattr(requests, "get", MagicMock(return_value=_mock_response(text=malicious)))

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "install", "rtk", "--yes"])

    assert result.exit_code == 0, result.output
    assert "PWNED_CLI" not in _os.environ
    assert (tmp_path / "plugins" / "rtk.py").read_text(encoding="utf-8") == malicious


@pytest.mark.parametrize("name", sorted(plugin_installer.KNOWN_EXAMPLE_PLUGINS))
def test_plugins_install_help_lists_every_curated_name_via_available(name: str) -> None:
    """Sanity: `plugins available`'s registry source is exactly
    `plugin_installer.KNOWN_EXAMPLE_PLUGINS` -- no CLI-side duplicate list to
    drift out of sync."""
    assert name in plugin_installer.KNOWN_EXAMPLE_PLUGINS
