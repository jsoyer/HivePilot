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
        # An ANTHROPIC_API_KEY must be present for "api" mode to stay "api" —
        # see TestConciergeModeResolution for the no-key auto-fallback path.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_model", "haiku")
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_mode", "api")
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


class TestConciergeModeResolution:
    """`settings.chatops_concierge_mode` ("api" | "cli") + the automatic
    api -> cli fallback when no ANTHROPIC_API_KEY is present, so the
    classifier works on a subscription/OAuth-only box (the operator's
    `claude` CLI) with zero config. See docs/INTEGRATIONS.md."""

    def _route(self, orch: MagicMock, text: str = "hi") -> concierge_service.ConciergeDecision:
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            return concierge_service.route(text, default_role="developer", default_target="acme")

    def test_api_mode_with_key_present_stays_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_mode", "api")
        raw = json.dumps({"kind": "answer", "answer_text": "ok"})
        orch = _orch_with_capture(return_value=raw)
        self._route(orch)
        runner_def, _ = orch.registry.capture_definition.call_args.args
        assert runner_def.options.get("mode") == "api"

    def test_api_mode_without_key_auto_falls_back_to_cli(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_mode", "api")
        monkeypatch.setattr(concierge_service, "_cli_fallback_logged", False)
        raw = json.dumps({"kind": "answer", "answer_text": "ok"})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "logger", MagicMock()) as mock_logger:
            self._route(orch)
            assert mock_logger.info.call_count == 1
            logged_msg = mock_logger.info.call_args.args[0]
            assert "ANTHROPIC_API_KEY" in logged_msg
            assert "claude CLI" in logged_msg
        runner_def, _ = orch.registry.capture_definition.call_args.args
        assert runner_def.options.get("mode") == "cli"

    def test_no_key_fallback_logs_only_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_mode", "api")
        monkeypatch.setattr(concierge_service, "_cli_fallback_logged", False)
        raw = json.dumps({"kind": "answer", "answer_text": "ok"})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "logger", MagicMock()) as mock_logger:
            self._route(orch)
            self._route(orch)
            assert mock_logger.info.call_count == 1

    def test_explicit_cli_mode_used_regardless_of_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_mode", "cli")
        raw = json.dumps({"kind": "answer", "answer_text": "ok"})
        orch = _orch_with_capture(return_value=raw)
        self._route(orch)
        runner_def, _ = orch.registry.capture_definition.call_args.args
        assert runner_def.options.get("mode") == "cli"

    def test_cli_mode_sets_non_interactive_permission_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: `claude --print` hangs on an interactive
        permission prompt in cli mode unless `--permission-mode` is passed
        (ClaudeRunner._build_invocation reads `definition.options
        ["permission_mode"]`; roles.py's developer role — the only other
        headless-claude caller in this codebase — sets the same field for the
        same reason). If this regresses, the classifier silently hangs the
        chat bot process instead of degrading to the fail-closed answer."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_mode", "cli")
        raw = json.dumps({"kind": "answer", "answer_text": "ok"})
        orch = _orch_with_capture(return_value=raw)
        self._route(orch)
        runner_def, _ = orch.registry.capture_definition.call_args.args
        assert runner_def.options.get("permission_mode") == "bypassPermissions"

    def test_api_mode_does_not_set_permission_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_mode", "api")
        raw = json.dumps({"kind": "answer", "answer_text": "ok"})
        orch = _orch_with_capture(return_value=raw)
        self._route(orch)
        runner_def, _ = orch.registry.capture_definition.call_args.args
        assert "permission_mode" not in runner_def.options

    def test_cli_mode_classification_success_returns_real_decision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proves the non-api path produces a real, validated decision — not
        just the fallback answer."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_mode", "cli")
        raw = json.dumps(
            {"kind": "route", "role_key": "developer", "target": "acme", "order": "fix the bug"}
        )
        orch = _orch_with_capture(return_value=raw)
        decision = self._route(orch, "ask Gustave to fix the bug")
        assert decision.kind == "route"
        assert decision.role_key == "developer"
        assert decision.target == "acme"
        assert decision.destructive is True

    def test_cli_mode_timeout_still_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_mode", "cli")
        orch = _orch_with_capture(side_effect=TimeoutError("claude cli timed out"))
        decision = self._route(orch, "do something")
        assert decision.kind == "answer"
        assert decision.answer_text == concierge_service._FALLBACK_ANSWER

    def test_classifier_sets_a_sane_capture_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A per-call timeout on the classifier's RunnerDefinition means a
        hung `claude` CLI degrades to the fail-closed answer instead of
        blocking the bot process indefinitely."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
        raw = json.dumps({"kind": "answer", "answer_text": "ok"})
        orch = _orch_with_capture(return_value=raw)
        self._route(orch)
        runner_def, _ = orch.registry.capture_definition.call_args.args
        assert isinstance(runner_def.timeout_seconds, int)
        assert 0 < runner_def.timeout_seconds <= 120


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
