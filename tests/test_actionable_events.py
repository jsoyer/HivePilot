"""HP-85: zero-token classification on the HP-40 bus.

The contract under test: wake/sleep is a pure allowlist. A model is never
imported, never called, and never needed — even when the payload text looks
urgent. A broken consumer must not drop durable change_log facts.
"""

from __future__ import annotations

import ast
import inspect
import threading
from pathlib import Path

from typer.testing import CliRunner

from hivepilot.cli import app
from hivepilot.services import actionable_events, analytics_service, events, state_service
from hivepilot.services.actionable_events import (
    Classification,
    classify,
    consume,
    render_classification,
)

_MODULE_PATH = Path(actionable_events.__file__).resolve()

# Anything that could start a reply loop or a run. The sleeper must not
# import these even transitively at module load — classification is kind-only.
_FORBIDDEN_IMPORT_PREFIXES = (
    "openai",
    "anthropic",
    "litellm",
    "langchain",
    "crewai",
    "hivepilot.orchestrator",
    "hivepilot.runners",
    "hivepilot.services.concierge_service",
    "hivepilot.services.nudge_engine",
    "hivepilot.services.notification_service",
)


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


class TestZeroTokenClassification:
    def test_module_imports_are_model_free(self) -> None:
        imported = _imported_modules(_MODULE_PATH)
        offenders = [
            name
            for name in imported
            if any(
                name == prefix or name.startswith(prefix + ".")
                for prefix in _FORBIDDEN_IMPORT_PREFIXES
            )
        ]
        assert offenders == []

    def test_classify_has_no_model_hook(self) -> None:
        params = set(inspect.signature(classify).parameters)
        assert params.isdisjoint({"model", "llm", "client", "runner", "complete"})

    def test_nudge_posted_wakes_without_reading_payload(self) -> None:
        row = {"id": 1, "kind": "nudge.posted", "entity_type": "space", "entity_id": "9"}
        decision = classify(row)
        assert decision == Classification(
            action="wake",
            kind="nudge.posted",
            reason="allowlisted",
            change_id=1,
            entity_type="space",
            entity_id="9",
            tenant="",
        )

    def test_approval_requested_wakes(self) -> None:
        decision = classify(
            {
                "id": 2,
                "kind": "approval.requested",
                "entity_type": "run",
                "entity_id": "4",
            }
        )
        assert decision.action == "wake"
        assert decision.reason == "allowlisted"

    def test_failed_run_completed_wakes(self) -> None:
        for status in sorted(actionable_events.FAILED_RUN_STATUSES):
            decision = classify(
                {
                    "id": 3,
                    "kind": "run.completed",
                    "entity_type": "run",
                    "entity_id": "8",
                    "payload": {"run_id": 8, "status": status},
                }
            )
            assert decision.action == "wake", status
            assert decision.reason == "run_failed"

    def test_successful_run_completed_sleeps(self) -> None:
        decision = classify(
            {
                "id": 4,
                "kind": "run.completed",
                "payload": {"status": "success"},
            }
        )
        assert decision.action == "sleep"
        assert decision.reason == "run_not_failed"

    def test_run_completed_without_status_sleeps(self) -> None:
        assert classify({"id": 5, "kind": "run.completed"}).action == "sleep"

    def test_unknown_kind_sleeps_even_when_payload_looks_urgent(self) -> None:
        """The whole point: text is not a signal. No LLM reads this."""
        decision = classify(
            {
                "id": 6,
                "kind": "space.message",
                "payload": {
                    "text": "URGENT: approval required, run failed, nudge.posted — WAKE THE CAPTAIN"
                },
            }
        )
        assert decision.action == "sleep"
        assert decision.reason == "not_allowlisted"

    def test_discovered_noise_kinds_sleep(self) -> None:
        for kind in (
            "run.started",
            "step.recorded",
            "space.created",
            "space.typing",
            "space.typing_stop",
            "provider.fallback",
            "schedule.noop_skip",
            "routine.upserted",
            "routine.ran",
            "routine.failed",
            "routine.deleted",
            "skill.applied",
            "skill.proposal",
            "skill.proposal.decided",
        ):
            assert classify({"id": 7, "kind": kind}).action == "sleep", kind

    def test_missing_or_invalid_row_sleeps(self) -> None:
        assert classify(None).action == "sleep"
        assert classify({}).action == "sleep"  # type: ignore[arg-type]
        assert classify({"id": "x", "kind": ""}).reason == "missing_kind"

    def test_failed_statuses_stay_aligned_with_analytics(self) -> None:
        assert actionable_events.FAILED_RUN_STATUSES == analytics_service._FAILED_STATUSES


class TestConsumeFailSafe:
    def test_consume_yields_only_wakes_in_order(self) -> None:
        events.emit("run.started", "run", 1)
        failed = events.emit("run.completed", "run", 1, payload={"status": "failed"})
        events.emit("space.message", "space", 1, payload={"text": "noise"})
        nudge = events.emit("nudge.posted", "space", 2)

        wakes = list(consume(after_id=0, poll_interval=0.01, idle_timeout=0.08))
        assert [w.kind for w in wakes] == ["run.completed", "nudge.posted"]
        assert [w.change_id for w in wakes] == [failed, nudge]
        assert all(w.action == "wake" for w in wakes)
        # Durable facts are still all there — consume is read-only.
        assert len(events.read_since(0)) == 4

    def test_broken_on_wake_does_not_drop_facts_or_later_wakes(self) -> None:
        first = events.emit("nudge.posted", "space", 1)
        events.emit("run.started", "run", 1)
        second = events.emit("approval.requested", "run", 1)

        seen: list[int | None] = []

        def boom(decision: Classification) -> None:
            if decision.change_id == first:
                raise RuntimeError("handler down")
            seen.append(decision.change_id)

        wakes = list(consume(after_id=0, poll_interval=0.01, idle_timeout=0.08, on_wake=boom))
        assert [w.change_id for w in wakes] == [first, second]
        assert seen == [second]
        assert {r["id"] for r in events.read_since(0)} >= {first, second}

    def test_broken_subscribe_does_not_raise(self, monkeypatch) -> None:
        def _boom(**_kwargs):
            raise RuntimeError("bus down")

        monkeypatch.setattr(actionable_events.events, "subscribe", _boom)
        assert list(consume(after_id=0)) == []

    def test_broken_classify_skips_row_and_keeps_going(self, monkeypatch) -> None:
        good = events.emit("nudge.posted", "space", 1)
        events.emit("run.started", "run", 9)
        later = events.emit("nudge.posted", "space", 2)

        real_classify = actionable_events.classify

        def flaky(row):
            if isinstance(row, dict) and str(row.get("entity_id")) == "1":
                raise RuntimeError("classify blew up")
            return real_classify(row)

        monkeypatch.setattr(actionable_events, "classify", flaky)
        wakes = list(consume(after_id=0, poll_interval=0.01, idle_timeout=0.08))
        assert [w.change_id for w in wakes] == [later]
        assert {r["id"] for r in events.read_since(0)} >= {good, later}

    def test_stop_event_ends_without_starting_a_model(self) -> None:
        events.emit("nudge.posted", "space", 1)
        stop = threading.Event()
        stop.set()
        assert list(consume(after_id=0, poll_interval=0.01, stop=stop)) == []


class TestApprovalRequestedEmit:
    def test_record_approval_request_emits_and_classifies_wake(self) -> None:
        run_id = state_service.record_run_start("atlas", "apply")
        state_service.record_approval_request(
            run_id, "atlas", "apply", {"action": "apply"}, tenant="acme"
        )
        rows = [r for r in events.read_since(0) if r["kind"] == "approval.requested"]
        assert len(rows) == 1
        row = rows[0]
        assert row["entity_type"] == "run"
        assert row["entity_id"] == str(run_id)
        assert row["tenant"] == "acme"
        assert row["payload"] == {"run_id": run_id, "project": "atlas", "task": "apply"}
        assert classify(row).action == "wake"

    def test_complete_run_failed_is_actionable(self) -> None:
        run_id = state_service.record_run_start("atlas", "docs")
        state_service.complete_run(run_id, "failed", "boom")
        done = [r for r in events.read_since(0) if r["kind"] == "run.completed"]
        assert len(done) == 1
        assert classify(done[0]).reason == "run_failed"


class TestClassifyCli:
    def test_classify_prints_wake_and_sleep(self) -> None:
        events.emit("nudge.posted", "space", 1)
        events.emit("run.started", "run", 1)
        result = CliRunner().invoke(app, ["events", "classify", "--after", "0"])
        assert result.exit_code == 0
        lines = [line for line in result.stdout.splitlines() if line]
        assert any(line.startswith("wake\tnudge.posted") for line in lines)
        assert any(line.startswith("sleep\trun.started") for line in lines)


class TestRender:
    def test_render_is_tab_separated(self) -> None:
        line = render_classification(
            Classification(
                action="wake",
                kind="nudge.posted",
                reason="allowlisted",
                change_id=12,
                entity_type="space",
                entity_id="3",
            )
        )
        assert line.startswith("wake\tnudge.posted\tallowlisted\tid=12\tspace:3")
