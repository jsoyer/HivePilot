"""
Shared pytest configuration and fixtures.

Stubs optional heavy dependencies (langchain, etc.) that are not installed
in the CI/test venv so that orchestrator-level tests can import without error.

This module is loaded by pytest BEFORE any test module is imported, which is
what allows the module-level `import hivepilot.orchestrator` in
test_pipeline_execution.py to succeed even though langchain is not installed.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Pin the config home BEFORE the first `hivepilot` import below.
#
# The roles registry is built at IMPORT time (`hivepilot/roles.py`:
# `ROLES = load_roles()`), reading `~/.config/hivepilot/roles.yaml` when one
# exists. On a machine that also RUNS HivePilot, that file is the deployment's
# roster — 20 noxys roles instead of the 8 built-in defaults — so every test
# asserting the default roster failed. That was 49 failures locally against a
# permanently green CI, which has no such file, and the divergence went
# undiagnosed for weeks: it looked like flakiness or test-ordering because it
# tracked neither the code nor the branch.
#
# It has to happen here, at module scope, rather than in a fixture: a fixture
# runs long after `ROLES` is bound. Rebinding it afterwards was tried and made
# things worse (49 failures became 61) — `refresh_roles()` rebinds the module
# global while callers already hold the old mapping.
#
# `HIVEPILOT_TEST_KEEP_CONFIG_HOME=1` opts out, for the rare case of
# reproducing a deployment-specific failure on purpose.
# ---------------------------------------------------------------------------
if not os.environ.get("HIVEPILOT_TEST_KEEP_CONFIG_HOME"):
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hivepilot-test-config-")


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
import hivepilot.swarm  # noqa: E402,F401 — populates TRANSPORT_MAP with builtins
from hivepilot.registry import RUNNER_MAP, SECRETS_MAP  # noqa: E402
from hivepilot.services.notification_service import NOTIFIER_MAP  # noqa: E402
from hivepilot.swarm.transport import TRANSPORT_MAP  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Pristine, built-ins-only snapshot — captured at conftest import time, before
# any test (or test-triggered production code path) has run. Used by
# `_isolate_runner_and_notifier_maps` below to reset process-global plugin
# registration state after every test.
_RUNNER_MAP_BASELINE = dict(RUNNER_MAP)
_NOTIFIER_MAP_BASELINE = dict(NOTIFIER_MAP)
_SECRETS_MAP_BASELINE = dict(SECRETS_MAP)
_TRANSPORT_MAP_BASELINE = dict(TRANSPORT_MAP)


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


# ---------------------------------------------------------------------------
# Vault folder taxonomy
# ---------------------------------------------------------------------------
# The engine no longer hardcodes any vault folder names — they come from the
# `folders:` key of vault.yaml (hivepilot/services/vault_layout.py). Tests that
# exercise a vault write therefore need a layout, and the suite deliberately
# pins a GENERIC one whose names match no engine default and no organisation's
# filing scheme. That is the point: if a folder name were ever hardcoded again,
# these tests would keep passing against the hardcoded value and FAIL against
# this fixture's value, which is exactly the signal wanted.
#
# `TEST_VAULT_FOLDERS` is exported so individual test modules can build the same
# vault on disk. Tests covering the UNCONFIGURED case override this fixture with
# their own empty layout.
TEST_VAULT_HIVEPILOT_FOLDER = "Robot Workspace"
TEST_VAULT_ARTIFACTS_FOLDER = "Deliverables"
TEST_VAULT_DECISIONS_FOLDER = "ADRs"
TEST_VAULT_SECURITY_FOLDER = "InfoSec"

#: An arbitrary expected-layout declaration. Most entries have no writer
#: anywhere in the engine — that is what `expected_folders:` is for.
TEST_VAULT_EXPECTED_FOLDERS = (
    "Inbox",
    "Journal",
    "Knowledge",
    "Architecture",
    "Design",
    TEST_VAULT_DECISIONS_FOLDER,
    "Research",
    "Engineering",
    "Roadmap",
    "Infrastructure",
    TEST_VAULT_SECURITY_FOLDER,
    "People",
    "Templates",
    "Projects",
    TEST_VAULT_HIVEPILOT_FOLDER,
    "Archive",
)

TEST_VAULT_FROZEN_FOLDERS = (
    TEST_VAULT_SECURITY_FOLDER,
    TEST_VAULT_DECISIONS_FOLDER,
    "Architecture",
    "Journal",
)


def _make_test_vault_layout():
    from hivepilot.services.vault_layout import (
        SLOT_ARTIFACTS,
        SLOT_DECISIONS,
        SLOT_HIVEPILOT,
        SLOT_SECURITY,
        VaultLayout,
    )

    return VaultLayout(
        folders={
            SLOT_HIVEPILOT: TEST_VAULT_HIVEPILOT_FOLDER,
            SLOT_ARTIFACTS: TEST_VAULT_ARTIFACTS_FOLDER,
            SLOT_DECISIONS: TEST_VAULT_DECISIONS_FOLDER,
            SLOT_SECURITY: TEST_VAULT_SECURITY_FOLDER,
        },
        expected_folders=TEST_VAULT_EXPECTED_FOLDERS,
        frozen_folders=TEST_VAULT_FROZEN_FOLDERS,
    )


@pytest.fixture(autouse=True)
def _pin_vault_layout(monkeypatch):
    """Pin the deployment-resolved vault taxonomy to the generic test one.

    `VAULT_LAYOUT` is a module-level singleton resolved at import (like every
    other config-derived constant in `agent_rules`), so it is patched directly.
    `ObsidianService` reads it through `current_layout()` at call time, so this
    reaches every service instance a test constructs without the test having to
    thread a `layout=` argument through.
    """
    from hivepilot.services import vault_layout

    monkeypatch.setattr(vault_layout, "VAULT_LAYOUT", _make_test_vault_layout())
    yield


# Capability tokens the repo's OWN bundled plugins declare, and which an
# operator must therefore allowlist. Deliberately a hardcoded literal rather
# than something derived by scanning `plugins/`: a plugin newly declaring a
# capability is a deployment-affecting change (every operator must add the
# token or lose that plugin at load), so it must break this list and force a
# deliberate update. `tests/test_plugins.py::TestPluginCapabilityManifest`
# guards which plugins declare what.
_BUNDLED_PLUGIN_CAPABILITIES = ["subprocess"]


@pytest.fixture(scope="session", autouse=True)
def _allow_bundled_plugin_capabilities():
    """Give the suite the capability policy a real deployment must now set.

    `plugins/gh.py` and `plugins/rtk.py` declare `capabilities:
    ["subprocess"]` because they genuinely `subprocess.run()`. Both are
    enabled by DEFAULT (`gh_enabled`/`rtk_enabled` are opt-OUT in
    `hivepilot/config.py`), and `_isolate_config_resolution` above points
    `base_dir` at the repo root — so every test that constructs a real
    `PluginManager()` scans the repo's actual `plugins/` directory and reaches
    those two declarations.

    `plugins_capability_policy` defaults to `[]` = deny every declared
    capability, so without this fixture `gh`/`rtk` are denied and every test
    that needs one of their contributions fails.

    NOTE (corrected after #377): this docstring used to say a denial
    "propagates out of `PluginManager()` construction entirely" and that 133
    tests across ~30 modules would fail without this fixture. That WAS true
    when this fixture was written, and it was the defect #377 fixed — the
    capability gate's `except` now logs and skips the one offending plugin
    instead of re-raising, so an unconfigured policy no longer takes down the
    whole plugin subsystem. The incidental casualties are gone; this fixture
    is now about making the test environment mirror a correctly configured
    deployment, not about keeping the suite runnable.

    This fixture is therefore the SINGLE conversion point for the old
    "bundled plugins declare nothing, so policy never matters" assumption
    that was previously implicit in the whole suite: it makes the test
    environment mirror a CORRECTLY CONFIGURED deployment, exactly as it
    already fabricates `base_dir` and `XDG_CONFIG_HOME`.

    It deliberately does NOT weaken the gate. The gate's behaviour is pinned
    explicitly and independently in
    `tests/test_plugin_subprocess_capability.py`, which monkeypatches the
    policy back to `[]` (and to malformed values) at function scope to assert
    that `gh`/`rtk` really are denied and rolled back — so the fail-closed
    default is tested by intent rather than by accidentally bricking
    unrelated tests.
    """
    mp = pytest.MonkeyPatch()

    from hivepilot.config import settings

    mp.setattr(
        settings, "plugins_capability_policy", list(_BUNDLED_PLUGIN_CAPABILITIES), raising=False
    )

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
    # Drain any async run this test started BEFORE its database goes away.
    #
    # `async_run_service` submits to a module-level ThreadPoolExecutor shared
    # for the whole session, and `submit_run` never joins the task — correct
    # in production, where `POST /v1/runs` must return in <500ms. But every
    # test gets a fresh DB above, so every test's first run is `id = 1`, and
    # a straggler from an earlier test calling `complete_run(1, ...)` resolves
    # its path at call time and lands HERE, stamping a status and
    # `finished_at` on a run it knows nothing about.
    #
    # That is what made `test_failure_never_surfaces_raw_exception_text` fail
    # about one full-suite run in six while passing in isolation — a security
    # assertion (raw secret-bearing exception text must never reach the
    # persisted `detail`) that sometimes did not run. Two earlier attempts
    # treated the symptom in the waiter; this is the source.
    #
    # Bounded: `wait_until_idle` returns False rather than blocking, so one
    # stuck task cannot hang the suite.
    from hivepilot.services import async_run_service

    async_run_service.wait_until_idle(10.0)


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
    TRANSPORT_MAP.clear()
    TRANSPORT_MAP.update(_TRANSPORT_MAP_BASELINE)


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


@pytest.fixture(autouse=True)
def _isolate_governance_config(monkeypatch):
    """Pin `governance_files` to the CODE-OWNED default for every test.

    These settings are deployment config: a real `.env` can list eleven
    governance documents where the shipped default lists six. Tests that
    assert on the list then pass in CI (no `.env`) and fail locally — which
    trains everyone to dismiss local failures as "environment", and that is
    exactly how a genuine misconfiguration went unnoticed for a full day.

    A test that wants a different list must set it explicitly.
    """
    from hivepilot.config import Settings, settings

    default = Settings.model_fields["governance_files"].default_factory()  # type: ignore[misc]
    monkeypatch.setattr(settings, "governance_files", list(default))
    yield
