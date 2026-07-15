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
from pathlib import Path
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

# Unlike RUNNER_MAP/NOTIFIER_MAP (populated at import time by their own
# owning modules, hivepilot.registry / hivepilot.services.notification_service
# respectively), SECRETS_MAP's builtins are registered by a `_BUILTIN_SECRETS`
# loop that lives in hivepilot.services.secrets_service (kept there instead of
# hivepilot.registry to avoid a circular import — see registry.py's
# SecretsRegistry comment). Import it explicitly so SECRETS_MAP is already
# populated with builtins before we snapshot the baseline below.
import hivepilot.services.secrets_service  # noqa: E402,F401
from hivepilot.registry import RUNNER_MAP, SECRETS_MAP  # noqa: E402
from hivepilot.services.notification_service import NOTIFIER_MAP  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Pristine, built-ins-only snapshot — captured at conftest import time, before
# any test (or test-triggered production code path) has run. Used by
# `_isolate_runner_and_notifier_maps` below to reset process-global plugin
# registration state after every test.
_RUNNER_MAP_BASELINE = dict(RUNNER_MAP)
_NOTIFIER_MAP_BASELINE = dict(NOTIFIER_MAP)
_SECRETS_MAP_BASELINE = dict(SECRETS_MAP)


@pytest.fixture(scope="session", autouse=True)
def _isolate_config_resolution(tmp_path_factory):
    """Prevent a developer's machine-global config from shadowing the repo config.

    `Settings.resolve_config_path()` (hivepilot/config.py) resolves config files
    in this priority order:
        1. $XDG_CONFIG_HOME/hivepilot/<file>  (or ~/.config/hivepilot/<file>)
        2. config_repo/<file>                 (shared config, local path)
        3. base_dir/<file>                    (repo-root fallback)
    On a dev machine, a stale ~/.config/hivepilot/{pipelines,groups,tasks}.yaml
    silently wins step 1 and makes tests read the wrong config instead of the
    repo's own fixtures — CI has no such directory so this only bites locally.

    Fix: point XDG_CONFIG_HOME at an empty, session-scoped tmp dir so step 1
    never finds anything, and unset/reset config_repo + base_dir on the
    already-constructed `settings` singleton (its values were captured at
    import time via pydantic-settings' env parsing, so a later env var change
    alone would not affect it) so resolution always falls through to
    base_dir/<file> = the repo root. Test isolation only — production XDG-first
    behavior in hivepilot/config.py is intentional and left untouched.
    """
    mp = pytest.MonkeyPatch()

    # Step 1: make the XDG branch miss for every test, regardless of what the
    # developer running the suite actually has in ~/.config/hivepilot.
    empty_xdg = tmp_path_factory.mktemp("hivepilot-xdg")
    mp.setenv("XDG_CONFIG_HOME", str(empty_xdg))
    # Belt-and-suspenders: clear the env var too, in case any code path
    # constructs a fresh Settings() during the test run.
    mp.delenv("HIVEPILOT_CONFIG_REPO", raising=False)

    # Steps 2 & 3: patch the already-constructed singleton directly, since its
    # attributes were resolved from the environment at import time.
    from hivepilot.config import settings

    mp.setattr(settings, "config_repo", None, raising=False)
    mp.setattr(settings, "base_dir", _REPO_ROOT, raising=False)

    yield

    mp.undo()


@pytest.fixture(autouse=True)
def _isolate_state_db(tmp_path, monkeypatch):
    """Redirect the SQLite state DB to a per-test tmp file so tests never
    touch the real ./state.db. DB_PATH is captured at import time, so patch
    the module attribute directly."""
    from hivepilot.services import state_service

    monkeypatch.setattr(state_service, "DB_PATH", tmp_path / "test_state.db")
    yield


@pytest.fixture(autouse=True)
def _isolate_runner_and_notifier_maps():
    """`RUNNER_MAP` / `NOTIFIER_MAP` / `SECRETS_MAP` (`hivepilot.registry` /
    `hivepilot.services.notification_service`) are process-global mutable
    dicts, populated with built-ins at import time and then mutated by any
    REAL (unmocked) `PluginManager()` construction — including indirectly,
    e.g. `hivepilot.services.api_service._get_orchestrator()`'s cached
    singleton, which lazily constructs a real `Orchestrator` (and therefore a
    real `PluginManager` that scans the actual `plugins/` directory) the
    first time any test exercises an API endpoint.

    Without this, the first test in the session to trigger a real plugin
    scan permanently registers that plugin's runner/notifier kinds; every
    later unmocked `PluginManager()` construction elsewhere in the suite then
    re-execs the same local plugin file (a fresh, distinct class object per
    load, since local-file plugins are loaded via
    `importlib.util.spec_from_file_location` rather than a cached import) and
    collides with itself via `RunnerRegistry.register`'s
    same-kind-different-object guard.

    `SECRETS_MAP` is the same kind of process-global mutable dict (populated
    with builtins at import time by `hivepilot.services.secrets_service`) and
    is included here for the same reason: a test or plugin that registers a
    custom secrets backend and forgets manual cleanup would otherwise
    order-dependently corrupt it for every later test.

    Restoring to the pristine, built-ins-only baseline (captured at conftest
    import time, before any test runs) after every test guarantees each test
    starts from the same clean slate regardless of run order.
    """
    yield
    RUNNER_MAP.clear()
    RUNNER_MAP.update(_RUNNER_MAP_BASELINE)
    NOTIFIER_MAP.clear()
    NOTIFIER_MAP.update(_NOTIFIER_MAP_BASELINE)
    SECRETS_MAP.clear()
    SECRETS_MAP.update(_SECRETS_MAP_BASELINE)


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
