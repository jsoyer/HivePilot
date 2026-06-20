"""Tests for the runner registry mapping."""

from __future__ import annotations

from hivepilot.registry import RUNNER_MAP
from hivepilot.runners.prompt_cli_runner import VibeRunner


def test_vibe_kind_is_registered() -> None:
    assert RUNNER_MAP.get("vibe") is VibeRunner


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
