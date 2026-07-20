"""Tests for hivepilot/services/concierge_service.py — the natural-language
concierge classifier: `route(text, ...) -> ConciergeDecision`.

Every test mocks `concierge_service._get_orchestrator()` (returning a
MagicMock whose `.registry.capture_definition(...)` is stubbed) so no real
LLM call, subprocess, or network access ever happens. Fail-closed behaviour
(LLM error / malformed JSON / missing destructive / unknown role or project)
is the primary thing under test — see CLAUDE.md's Anti-Goodhart guidance.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.services import concierge_service


def _fake_role(
    name: str,
    title: str,
    display_name: str | None = None,
    prompt_file=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        title=title,
        display_name=display_name,
        prompt_file=prompt_file,
    )


def _orch_with_capture(return_value: str | None = None, side_effect=None) -> MagicMock:
    orch = MagicMock()
    if side_effect is not None:
        orch.registry.capture_definition.side_effect = side_effect
    else:
        orch.registry.capture_definition.return_value = return_value
    return orch


@pytest.fixture(autouse=True)
def _stub_roster_and_projects(monkeypatch: pytest.MonkeyPatch):
    """Default roster/projects/state stubs so `route()` doesn't hit real config."""
    roles = [
        _fake_role("developer", "Developer", "Gustave"),
        _fake_role("ceo", "CEO", "Alienor"),
    ]
    monkeypatch.setattr("hivepilot.roles.list_roles", lambda: roles)
    projects = SimpleNamespace(projects={"acme": object(), "acme-api": object()})
    monkeypatch.setattr("hivepilot.services.project_service.load_projects", lambda: projects)
    monkeypatch.setattr("hivepilot.services.state_service.list_recent_runs", lambda limit=5: [])
    monkeypatch.setattr("hivepilot.services.state_service.get_pending_approvals", lambda: [])
    yield


class TestRouteAnswer:
    def test_answer_kind_returned(self) -> None:
        raw = json.dumps({"kind": "answer", "answer_text": "Hello there!"})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "hi", default_role="developer", default_target="acme"
            )
        assert decision.kind == "answer"
        assert decision.answer_text == "Hello there!"
        assert decision.destructive is False


class TestRouteApiModeCall:
    def test_capture_definition_called_with_api_mode_and_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_model", "haiku")
        raw = json.dumps({"kind": "answer", "answer_text": "ok"})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            concierge_service.route("hi", default_role="developer", default_target="acme")

        orch.registry.capture_definition.assert_called_once()
        runner_def, payload = orch.registry.capture_definition.call_args.args
        assert runner_def.kind == "claude"
        assert runner_def.options.get("mode") == "api"
        assert runner_def.model == "haiku"
        assert payload.metadata.get("extra_prompt")

    def test_default_model_used_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_model", None)
        raw = json.dumps({"kind": "answer", "answer_text": "ok"})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            concierge_service.route("hi", default_role="developer", default_target="acme")
        runner_def, _ = orch.registry.capture_definition.call_args.args
        assert runner_def.model  # some sensible non-empty default


class TestRouteKind:
    def test_route_kind_returned_and_destructive(self) -> None:
        raw = json.dumps(
            {
                "kind": "route",
                "role_key": "developer",
                "target": "acme",
                "order": "fix the bug",
            }
        )
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "ask gustave to fix the bug",
                default_role="developer",
                default_target="acme",
            )
        assert decision.kind == "route"
        assert decision.role_key == "developer"
        assert decision.target == "acme"
        assert decision.destructive is True

    def test_route_missing_role_key_uses_default(self) -> None:
        raw = json.dumps({"kind": "route", "target": "acme", "order": "do it"})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "do it", default_role="developer", default_target="acme"
            )
        assert decision.kind == "route"
        assert decision.role_key == "developer"

    def test_route_missing_target_uses_default(self) -> None:
        raw = json.dumps({"kind": "route", "role_key": "developer", "order": "do it"})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "do it", default_role="developer", default_target="acme"
            )
        assert decision.target == "acme"

    def test_route_to_unknown_role_degrades_to_answer(self) -> None:
        raw = json.dumps(
            {"kind": "route", "role_key": "nope-not-a-role", "target": "acme", "order": "x"}
        )
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route("x", default_role="developer", default_target="acme")
        assert decision.kind == "answer"
        assert decision.answer_text

    def test_route_to_unknown_project_degrades_to_answer(self) -> None:
        raw = json.dumps(
            {
                "kind": "route",
                "role_key": "developer",
                "target": "not-a-real-project",
                "order": "x",
            }
        )
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route("x", default_role="developer", default_target="acme")
        assert decision.kind == "answer"
        assert decision.answer_text


class TestActionKind:
    def test_action_run_pipeline_destructive_true(self) -> None:
        raw = json.dumps(
            {
                "kind": "action",
                "action": "run_pipeline",
                "target": "acme",
                "params": {"pipeline": "company"},
                "destructive": True,
            }
        )
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "run the company pipeline on acme",
                default_role="developer",
                default_target="acme",
            )
        assert decision.kind == "action"
        assert decision.action == "run_pipeline"
        assert decision.destructive is True

    def test_action_missing_destructive_field_treated_as_true(self) -> None:
        """Empty/missing `destructive` on an action MUST be treated as True —
        the recurring 'empty-value fail-open' bug class. Even if the model
        omits it (or says False), the concierge hardcodes destructive=True
        for every currently-known action kind."""
        raw = json.dumps(
            {
                "kind": "action",
                "action": "run",
                "target": "acme",
                "params": {"task": "deploy"},
                # "destructive" deliberately omitted
            }
        )
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "deploy acme", default_role="developer", default_target="acme"
            )
        assert decision.kind == "action"
        assert decision.destructive is True

    def test_action_explicit_destructive_false_still_forced_true(self) -> None:
        raw = json.dumps(
            {
                "kind": "action",
                "action": "run",
                "target": "acme",
                "params": {"task": "deploy"},
                "destructive": False,
            }
        )
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "deploy acme", default_role="developer", default_target="acme"
            )
        assert decision.destructive is True

    def test_action_approve_requires_run_id(self) -> None:
        raw = json.dumps({"kind": "action", "action": "approve", "params": {}})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "approve it", default_role="developer", default_target="acme"
            )
        assert decision.kind == "answer"

    def test_action_approve_with_run_id(self) -> None:
        raw = json.dumps({"kind": "action", "action": "approve", "params": {"run_id": 42}})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "approve run 42", default_role="developer", default_target="acme"
            )
        assert decision.kind == "action"
        assert decision.action == "approve"
        assert decision.params == {"run_id": 42}
        assert decision.destructive is True

    def test_unknown_action_name_degrades_to_answer(self) -> None:
        raw = json.dumps({"kind": "action", "action": "delete_everything", "params": {}})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "wipe it", default_role="developer", default_target="acme"
            )
        assert decision.kind == "answer"


class TestFailClosed:
    def test_capture_definition_raises_returns_answer(self) -> None:
        orch = _orch_with_capture(side_effect=RuntimeError("boom"))
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "do something", default_role="developer", default_target="acme"
            )
        assert decision.kind == "answer"
        assert decision.answer_text

    def test_malformed_json_returns_answer(self) -> None:
        orch = _orch_with_capture(return_value="not json at all {{{")
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "do something", default_role="developer", default_target="acme"
            )
        assert decision.kind == "answer"
        assert decision.answer_text

    def test_unknown_kind_returns_answer(self) -> None:
        raw = json.dumps({"kind": "delete_the_universe"})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "do something", default_role="developer", default_target="acme"
            )
        assert decision.kind == "answer"

    def test_empty_string_response_returns_answer(self) -> None:
        orch = _orch_with_capture(return_value="")
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "do something", default_role="developer", default_target="acme"
            )
        assert decision.kind == "answer"

    def test_non_dict_json_returns_answer(self) -> None:
        orch = _orch_with_capture(return_value=json.dumps(["not", "a", "dict"]))
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "do something", default_role="developer", default_target="acme"
            )
        assert decision.kind == "answer"


class TestRosterBuild:
    def test_roster_includes_mission_line(self, tmp_path, monkeypatch) -> None:
        prompt = tmp_path / "developer.md"
        prompt.write_text("# Developer\n\n## Mission\nBuild things well.\n")
        roles = [_fake_role("developer", "Developer", "Gustave", prompt_file=prompt)]
        monkeypatch.setattr("hivepilot.roles.list_roles", lambda: roles)

        roster = concierge_service._build_roster()

        assert len(roster) == 1
        assert roster[0]["role_key"] == "developer"
        assert roster[0]["mission"] == "Build things well."

    def test_roster_tolerates_missing_prompt_file(self, tmp_path, monkeypatch) -> None:
        missing = tmp_path / "does-not-exist.md"
        roles = [_fake_role("developer", "Developer", "Gustave", prompt_file=missing)]
        monkeypatch.setattr("hivepilot.roles.list_roles", lambda: roles)

        roster = concierge_service._build_roster()  # must not raise

        assert len(roster) == 1
        assert roster[0]["mission"] == ""

    def test_roster_tolerates_none_prompt_file(self, monkeypatch) -> None:
        roles = [_fake_role("developer", "Developer", "Gustave", prompt_file=None)]
        monkeypatch.setattr("hivepilot.roles.list_roles", lambda: roles)

        roster = concierge_service._build_roster()  # must not raise

        assert roster[0]["mission"] == ""

    def test_roster_tolerates_list_roles_error(self, monkeypatch) -> None:
        def _raise():
            raise RuntimeError("roles.yaml is broken")

        monkeypatch.setattr("hivepilot.roles.list_roles", _raise)

        roster = concierge_service._build_roster()  # must not raise

        assert roster == []
