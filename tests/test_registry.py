"""Tests for the runner registry mapping."""

from __future__ import annotations

from hivepilot.registry import RUNNER_MAP, RunnerRegistry
from hivepilot.runners.prompt_cli_runner import VibeRunner


def test_vibe_kind_is_registered() -> None:
    assert RUNNER_MAP.get("vibe") is VibeRunner


def test_known_kinds_returns_frozenset_of_builtins() -> None:
    builtins = {
        "claude",
        "shell",
        "langchain",
        "internal",
        "codex",
        "gemini",
        "opencode",
        "ollama",
        "container",
        "cursor",
        "vibe",
    }
    known = RunnerRegistry.known_kinds()
    assert isinstance(known, frozenset)
    assert builtins <= known


def test_capture_definition_routes_http_host_to_worker(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from hivepilot.models import RunnerDefinition
    from hivepilot.registry import RunnerRegistry

    class FakeWorker:
        def __init__(self, definition, settings) -> None:
            self.definition = definition

        def capture(self, payload):
            return "VIA WORKER"

    monkeypatch.setattr("hivepilot.runners.worker_runner.RemoteWorkerRunner", FakeWorker)
    rdef = RunnerDefinition(kind="claude", host="http://hostC:8900")
    out = RunnerRegistry({}).capture_definition(rdef, MagicMock())
    assert out == "VIA WORKER"


def test_capture_definition_falls_back_to_local_on_worker_failure(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from hivepilot import registry as reg
    from hivepilot.models import RunnerDefinition
    from hivepilot.registry import RUNNER_MAP, RunnerRegistry

    monkeypatch.setattr(reg.settings, "worker_fallback_local", True, raising=False)

    class BoomWorker:
        def __init__(self, definition, settings) -> None:
            pass

        def capture(self, payload):
            raise RuntimeError("worker down")

    class LocalRunner:
        def __init__(self, definition, settings) -> None:
            self.definition = definition

        def capture(self, payload):
            return f"LOCAL host={self.definition.host}"

    monkeypatch.setattr("hivepilot.runners.worker_runner.RemoteWorkerRunner", BoomWorker)
    monkeypatch.setitem(RUNNER_MAP, "claude", LocalRunner)
    rdef = RunnerDefinition(kind="claude", host="https://hostC:8900")
    out = RunnerRegistry({}).capture_definition(rdef, MagicMock())
    assert out == "LOCAL host=None"  # fell back to local, host cleared
