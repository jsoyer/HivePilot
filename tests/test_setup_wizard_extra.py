"""Unit tests for `hivepilot.services.setup_wizard_extra` -- the read-only /
low-risk `hivepilot setup` steps (welcome probe, admin token bootstrap,
runner probe, plugins install, services guidance, final summary) that were
split out of `hivepilot.services.setup_wizard` to keep that module under
its ~500-line budget.

These functions are re-exported by `hivepilot.services.setup_wizard` (see
that module's imports), so the primary behavioral tests for
`step_admin_token`/`step_runners`/`step_plugins`/`step_services` live in
`tests/test_setup_wizard.py` (which exercises them via the re-exported
`setup_wizard.step_*` names, matching how `run_setup` calls them). This
file adds direct, module-local coverage so `setup_wizard_extra.py` is also
independently testable/importable without going through the parent module.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from rich.console import Console

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

from hivepilot.services import setup_wizard_extra  # noqa: E402
from hivepilot.services.setup_wizard_common import SetupOptions  # noqa: E402


def _console() -> Console:
    return Console(record=True, width=120)


def test_step_welcome_prints_environment_probe(tmp_path: Path) -> None:
    console = _console()
    setup_wizard_extra.step_welcome(console, tmp_path / ".env")
    text = console.export_text()
    assert "Python" in text
    assert "Config dir" in text


def test_step_runners_lists_known_kinds() -> None:
    console = _console()
    status = setup_wizard_extra.step_runners(console)
    assert "on PATH" in status


def test_step_plugins_skips_when_none_selected(tmp_path: Path) -> None:
    console = _console()
    options = SetupOptions(assume_yes=True)
    status = setup_wizard_extra.step_plugins(
        console, options, interactive=False, env_path=tmp_path / ".env", only_requested=False
    )
    assert status == "skipped"


def test_step_plugins_installs_named_plugin(monkeypatch, tmp_path: Path) -> None:
    fetch_mock = MagicMock()
    persist_mock = MagicMock()
    monkeypatch.setattr(setup_wizard_extra.plugin_installer, "fetch_plugin", fetch_mock)
    monkeypatch.setattr(setup_wizard_extra.plugin_installer, "persist_enabled", persist_mock)
    console = _console()
    options = SetupOptions(assume_yes=True, plugins="rtk")
    status = setup_wizard_extra.step_plugins(
        console, options, interactive=False, env_path=tmp_path / ".env", only_requested=False
    )
    assert status == "installed 1"
    fetch_mock.assert_called_once_with("rtk")
    persist_mock.assert_called_once()


def test_step_services_no_init_system(monkeypatch) -> None:
    monkeypatch.setattr(setup_wizard_extra.shutil, "which", MagicMock(return_value=None))
    console = _console()
    status = setup_wizard_extra.step_services(console, interactive=False, options=SetupOptions())
    assert status == "none"


def test_step_summary_renders_panel() -> None:
    console = _console()
    setup_wizard_extra.step_summary(console, {"token": "minted", "telegram": "skipped"})
    text = console.export_text()
    assert "Setup summary" in text
    assert "Still to do" in text
