"""Follow-up hardening tests for ``plugins/tmux.py`` (post plugin-arch-overhaul
code review).

The private temp files the tmux runner writes — the env/secrets overlay
(``0600``) and the exit-code marker — must be created INSIDE the ``try``/
``finally`` (and, for the env file, immediately before the ``try`` that removes
it on failure), so a failure between ``mkstemp`` and the cleanup guard can never
leak a secret-bearing file on disk. Complements ``tests/test_plugin_tmux.py``
(the main behavioural suite); named ``test_tmux.py`` to satisfy the repo's
TDD test-exists hook for ``plugins/tmux.py``.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload

REPO_ROOT = Path(__file__).parent.parent
TMUX_PLUGIN_PATH = REPO_ROOT / "plugins" / "tmux.py"


def _load_tmux_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hivepilot_plugin_tmux_hardening", TMUX_PLUGIN_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tmux_module() -> ModuleType:
    return _load_tmux_module()


def _payload(tmp_path: Path, secrets: dict[str, str] | None = None) -> RunnerPayload:
    return RunnerPayload(
        project_name="proj",
        project=ProjectConfig(path=tmp_path, env={}),
        task_name="t",
        step=TaskStep(name="s", runner="tmux", command="echo hi"),
        metadata={},
        secrets=secrets or {},
    )


def _leftover_env_files() -> set[str]:
    return {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("hivepilot-tmux-env-")}


def _raise(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("simulated failure")


def test_write_env_file_is_owner_only_and_written(tmux_module: ModuleType, tmp_path: Path) -> None:
    runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
    path = runner._write_env_file(_payload(tmp_path, secrets={"API_TOKEN": "s3cr3t"}))
    try:
        assert os.path.exists(path)
        # 0600 — a secret overlay must never be group/world readable.
        assert (os.stat(path).st_mode & 0o777) == 0o600
        assert "export API_TOKEN=" in Path(path).read_text()
    finally:
        os.remove(path)


def test_write_env_file_removes_temp_on_failure(
    tmux_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure at the (now inside-``try``) ``fchmod`` must remove the 0600
    temp file rather than leak it — this is the exact window the review flagged.
    """
    before = _leftover_env_files()
    monkeypatch.setattr(tmux_module.os, "fchmod", _raise)

    runner = tmux_module.TmuxRunner(RunnerDefinition(name="tmux", kind="tmux"), settings)
    with pytest.raises(RuntimeError):
        runner._write_env_file(_payload(tmp_path, secrets={"API_TOKEN": "s3cr3t"}))

    leaked = _leftover_env_files() - before
    assert not leaked, f"leaked 0600 temp env file(s): {leaked}"
