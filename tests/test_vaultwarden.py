"""Follow-up hardening test for ``plugins/vaultwarden.py`` (post
plugin-arch-overhaul code review).

``bw`` has no per-invocation ``--server`` flag, so this backend pins the CLI at
the self-hosted server with ``bw config server <url>`` and then fetches — two
separate ``bw`` processes against GLOBAL, persisted CLI state. Those two steps
must run under the process-wide ``_BW_CLI_LOCK`` so a concurrent vaultwarden
resolve can't re-point the server between the pin and the fetch. Complements
``tests/test_plugin_vaultwarden.py``; named ``test_vaultwarden.py`` to satisfy
the repo's TDD test-exists hook for ``plugins/vaultwarden.py``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from hivepilot.config import Settings
from hivepilot.registry import SecretRef

REPO_ROOT = Path(__file__).parent.parent
VW_PLUGIN_PATH = REPO_ROOT / "plugins" / "vaultwarden.py"


def _load_vaultwarden_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_vaultwarden_hardening", VW_PLUGIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def vw_module() -> ModuleType:
    return _load_vaultwarden_module()


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def test_both_bw_calls_run_under_the_cli_lock(
    vw_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every ``bw`` invocation in ``_fetch`` (both ``config server`` and
    ``get item``) must execute while ``_BW_CLI_LOCK`` is held; the lock must be
    released once ``resolve`` returns."""
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], **_kwargs: object) -> _FakeCompleted:
        assert vw_module._BW_CLI_LOCK.locked(), f"bw invoked without the CLI lock held: {argv[:3]}"
        calls.append(list(argv))
        return _FakeCompleted(json.dumps({"data": {"login": {"password": "the-value"}}}))

    monkeypatch.setattr(vw_module.subprocess, "run", _fake_run)
    monkeypatch.setattr(vw_module.shutil, "which", lambda _binary: "/usr/bin/bw")
    monkeypatch.setenv("BW_SESSION", "SESSION-TOKEN-SHOULD-NOT-LEAK")

    settings = Settings(vaultwarden_server_url="https://vault.example")
    backend = vw_module.VaultwardenBackend()
    ref = SecretRef(source="vaultwarden", spec={"item": "my-item"})

    value = backend.resolve(ref, settings)

    assert value == "the-value"
    # Server pin first, then the fetch — both recorded, both asserted under lock.
    assert calls[0][:3] == ["bw", "config", "server"]
    assert calls[1][:3] == ["bw", "get", "item"]
    # Lock is a plain (non-reentrant) mutex, released after resolve returns.
    assert not vw_module._BW_CLI_LOCK.locked()
