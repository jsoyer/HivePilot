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


# ---------------------------------------------------------------------------
# Phase 24b.2a follow-up — capture_definition clears the usage stash at entry
# so an earlier capture's usage (e.g. a debate/rebuttal call outside the main
# step loop, which never pops) can never bleed into a LATER capture's step
# via pop_last_usage().
# ---------------------------------------------------------------------------


def test_capture_definition_clears_stash_so_earlier_captures_dont_leak_into_later_ones(
    monkeypatch,
) -> None:
    from unittest.mock import MagicMock

    from hivepilot.models import RunnerDefinition
    from hivepilot.registry import RUNNER_MAP, RunnerRegistry
    from hivepilot.runners.base import UsageInfo, pop_last_usage, set_last_usage

    class ClaudeLikeRunner:
        """Simulates ClaudeRunner.capture() with usage capture ON, invoked
        from a NON-main-loop call site (e.g. a debate/rebuttal capture_definition
        call) that stashes usage but never pops it."""

        def __init__(self, definition, settings) -> None:
            pass

        def capture(self, payload):
            set_last_usage(UsageInfo(input_tokens=999, output_tokens=999, cost_usd=9.99))
            return "debate rebuttal output"

    class PlainRunner:
        """A later, unrelated (e.g. non-claude) step's runner — never touches
        usage at all."""

        def __init__(self, definition, settings) -> None:
            pass

        def capture(self, payload):
            return "shell output"

    monkeypatch.setitem(RUNNER_MAP, "claude", ClaudeLikeRunner)
    monkeypatch.setitem(RUNNER_MAP, "shell", PlainRunner)
    registry = RunnerRegistry({})

    # 1. A debate/rebuttal-style capture_definition call (outside the main
    #    step loop) stashes usage and never pops it — mirrors orchestrator.py
    #    call sites at ~914/~1075/~1142/~1830/~1991.
    out1 = registry.capture_definition(RunnerDefinition(kind="claude"), MagicMock())
    assert out1 == "debate rebuttal output"

    # 2. A later, unrelated step (non-claude runner) goes through
    #    capture_definition too — its own entry-clear must wipe the stale
    #    usage from step 1 BEFORE its runner (which never sets usage) runs.
    out2 = registry.capture_definition(RunnerDefinition(kind="shell"), MagicMock())
    assert out2 == "shell output"

    # 3. The main step loop pops right after — must see NO usage at all
    #    (input_tokens/output_tokens/cost_usd all None), proving step 1's
    #    usage was never misattributed to step 2.
    usage = pop_last_usage()
    assert usage is None
