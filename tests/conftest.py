"""
Shared pytest configuration and fixtures.

Stubs optional heavy dependencies (langchain, etc.) that are not installed
in the CI/test venv so that orchestrator-level tests can import without error.

This module is loaded by pytest BEFORE any test module is imported, which is
what allows the module-level `import hivepilot.orchestrator` in
test_pipeline_execution.py to succeed even though langchain is not installed.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock


def _make_stub(name: str) -> types.ModuleType:
    """Create a ModuleType stub that delegates attribute access to a MagicMock.

    Using a plain MagicMock as the module directly doesn't satisfy
    `isinstance(mod, types.ModuleType)` checks inside importlib, so we wrap:
    the module's __getattr__ falls back to a MagicMock so that
    `from stub_mod.submod import SomeClass` yields a MagicMock() callable.
    """
    mod = types.ModuleType(name)
    # __getattr__ is called for any attribute not found on the module object.
    # Returning a MagicMock means `from mod import Anything` gets a callable stub.
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    sys.modules[name] = mod
    return mod


# ---------------------------------------------------------------------------
# Stub langchain and its sub-packages used by knowledge_service at import time.
# The order matters: parent packages must be registered before children.
# ---------------------------------------------------------------------------
_LANGCHAIN_MODULES = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
    "langchain_openai",
    "faiss",
    "boto3",
    "boto3.session",
    "botocore",
    "botocore.exceptions",
]

for _mod_name in _LANGCHAIN_MODULES:
    if _mod_name not in sys.modules:
        _make_stub(_mod_name)


# ---------------------------------------------------------------------------
# DB isolation — redirect state DB to a per-test tmp file
# ---------------------------------------------------------------------------

import pytest  # noqa: E402  (must come after sys.modules stubs are installed)


@pytest.fixture(autouse=True)
def _isolate_state_db(tmp_path, monkeypatch):
    """Redirect the SQLite state DB to a per-test tmp file so tests never
    touch the real ./state.db. DB_PATH is captured at import time, so patch
    the module attribute directly."""
    from hivepilot.services import state_service

    monkeypatch.setattr(state_service, "DB_PATH", tmp_path / "test_state.db")
    yield


@pytest.fixture(autouse=True)
def _use_noxys_config_repo(monkeypatch):
    """Point config_repo at examples/noxys/ for every test.

    After the clean-release refactor, noxys config files (roles.yaml, tasks.yaml,
    pipelines.yaml, …) live under examples/noxys/ rather than the repo root.
    All service loaders call settings.resolve_config_path(), which checks
    config_repo first.  Patching the config_repo field on the shared settings
    singleton makes every loader find the files without per-test setup.

    Tests that need a different config_repo (or no config at all) override this
    via their own monkeypatch.setattr / patch.object calls, which take priority.
    """
    from pathlib import Path

    import hivepilot.config as config_module

    noxys_dir = str(Path(__file__).parent.parent / "examples" / "noxys")
    monkeypatch.setattr(config_module.settings, "config_repo", noxys_dir)
    yield


@pytest.fixture(autouse=True)
def _no_outbound_notifications(monkeypatch):
    """Tests must NEVER send real Slack/Discord/Telegram messages.

    Pipeline tests exercise run_pipeline, which live-streams agent turns; without
    this guard a configured Telegram chat gets spammed with test pipeline output.
    Disable the live stream and stub the low-level senders. Tests that specifically
    exercise the senders re-enable/override these via their own monkeypatch.
    """
    from hivepilot.services import notification_service

    monkeypatch.setattr(notification_service.settings, "telegram_stream_live", False, raising=False)
    monkeypatch.setattr(notification_service, "_send_telegram", lambda *a, **k: None)
    monkeypatch.setattr(notification_service, "_send_slack", lambda *a, **k: None)
    monkeypatch.setattr(notification_service, "_send_discord", lambda *a, **k: None)
    monkeypatch.setattr(notification_service, "send_approval_keyboard", lambda *a, **k: None)
    # Henri's auto-observation runs vibe (not installed in CI) — keep it off in tests.
    monkeypatch.setattr(notification_service.settings, "auditor_auto", False, raising=False)
    yield
