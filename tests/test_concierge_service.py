"""Tests for hivepilot/services/concierge_service.py — the natural-language
concierge classifier: `route(text, ...) -> ConciergeDecision`.

Every test mocks `concierge_service._get_orchestrator()` (returning a
MagicMock whose `.registry.capture_definition(...)` is stubbed) so no real
LLM call, subprocess, or network access ever happens. Fail-closed behaviour
(LLM error / malformed JSON / missing destructive / unknown role or project)
is the primary thing under test — see CLAUDE.md's Anti-Goodhart guidance.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import time
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

    def test_cli_mode_disables_all_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SECURITY: the classifier prompt embeds untrusted chat text. cli
        mode must run with NO tools available at all (`--tools ""` — see
        ClaudeRunner._resolve_tools) so a prompt-injected instruction has
        nothing to invoke. This is deny-by-default (tool set structurally
        empty), not merely permission-gated."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_mode", "cli")
        raw = json.dumps({"kind": "answer", "answer_text": "ok"})
        orch = _orch_with_capture(return_value=raw)
        self._route(orch)
        runner_def, _ = orch.registry.capture_definition.call_args.args
        assert runner_def.options.get("tools") == ""

    def test_cli_mode_never_sets_bypass_permissions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression guard: bypassPermissions must NEVER be reintroduced on
        the concierge path — with no tools available (see test above) there
        is nothing to gate behind a permission mode, and bypassPermissions
        would grant exactly the blanket tool authority the no-tools
        restriction exists to deny."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_mode", "cli")
        raw = json.dumps({"kind": "answer", "answer_text": "ok"})
        orch = _orch_with_capture(return_value=raw)
        self._route(orch)
        runner_def, _ = orch.registry.capture_definition.call_args.args
        assert runner_def.options.get("permission_mode") != "bypassPermissions"
        assert "permission_mode" not in runner_def.options

    def test_api_mode_does_not_set_tools_or_permission_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """api mode never has tool access at all (it's a raw Messages API
        call, see `_run_api`) — no need for either option there."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_mode", "api")
        raw = json.dumps({"kind": "answer", "answer_text": "ok"})
        orch = _orch_with_capture(return_value=raw)
        self._route(orch)
        runner_def, _ = orch.registry.capture_definition.call_args.args
        assert "permission_mode" not in runner_def.options
        assert "tools" not in runner_def.options

    def test_cli_no_tools_invariant_violation_refuses_and_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defense in depth: if the no-tools restriction were ever missing
        from the cli definition (e.g. a future edit to
        `_build_classifier_options` drops the `tools` assignment), the hard
        invariant check in `route()` must refuse to run the cli classifier at
        all and fail closed — never call `capture_definition` with a
        tool-capable cli session on untrusted input."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(concierge_service.settings, "chatops_concierge_mode", "cli")
        # Simulate the no-tools restriction failing to attach — a options
        # dict missing "tools" entirely, which is exactly what a regression
        # in _build_classifier_options would produce.
        monkeypatch.setattr(
            concierge_service, "_build_classifier_options", lambda mode: {"mode": mode}
        )
        orch = _orch_with_capture(return_value=json.dumps({"kind": "answer", "answer_text": "ok"}))
        decision = self._route(orch)
        assert decision.kind == "answer"
        # CONVERTED: was `_FALLBACK_ANSWER` ("I didn't quite get that. Try
        # rephrasing"). We refused to spawn the session at all, so the message
        # was never classified — blaming the operator's wording named the wrong
        # cause. The test's real subject, still asserted, is that it fails
        # CLOSED and never reaches the runner.
        assert decision.answer_text == concierge_service._INFRASTRUCTURE_FALLBACK_ANSWER
        orch.registry.capture_definition.assert_not_called()

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
        # CONVERTED: this test asserted `_FALLBACK_ANSWER` on a TIMEOUT, i.e. it
        # codified "tell the operator to rephrase" as the intended answer to an
        # infrastructure failure — which is exactly what happened to a real
        # operator on Telegram. Still fails closed (the subject of the test);
        # the message now names the real cause.
        assert decision.answer_text == concierge_service._INFRASTRUCTURE_FALLBACK_ANSWER

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


class TestSubstantiveQuestionAnswerRouting:
    """Regression coverage for the production UX gap: a substantive,
    open-ended question (e.g. "here's a use case — how could we cover it
    with the product? let's think/plan/decide") got the dismissive generic
    `_FALLBACK_ANSWER` ("I didn't quite get that...") instead of a genuine
    answer, because the model sometimes reaches for a made-up `action` name
    (e.g. "plan"/"discuss") for language that SOUNDS actionable but isn't one
    of the four real actions — and `_parse_raw` used to discard the model's
    own `answer_text` entirely in that case. The concierge prompt (see
    `hivepilot/prompts/concierge.md`) also now explicitly steers such
    messages to `kind: "answer"` with a genuine reply. The rephrase fallback
    is reserved for truly empty/unparseable model output."""

    USE_CASE_TEXT = (
        "here's a use case: our support team keeps losing track of escalated "
        "tickets across three tools. how could we cover this with the "
        "product? let's think it through and decide on an approach."
    )

    def test_substantive_question_classified_as_answer(self) -> None:
        """The straightforward case: the classifier itself returns a genuine
        `kind: "answer"` with real engagement — must NOT be the canned
        fallback text."""
        raw = json.dumps(
            {
                "kind": "answer",
                "answer_text": (
                    "You could model each tool as a source feeding a single "
                    "tracking pipeline, with a role that reconciles escalations "
                    "across them. Want me to loop in the PM role for a deeper plan?"
                ),
            }
        )
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                self.USE_CASE_TEXT, default_role="developer", default_target="acme"
            )
        assert decision.kind == "answer"
        assert decision.answer_text != concierge_service._FALLBACK_ANSWER
        assert "escalat" in (decision.answer_text or "").lower()

    def test_invalid_action_name_salvages_models_own_answer_text(self) -> None:
        """The model understood the question but wrongly reached for a
        made-up action name (e.g. "plan", not one of the four real
        actions) — if it ALSO supplied a genuine `answer_text` alongside
        that, salvage and use it instead of discarding everything for the
        generic dismissive fallback."""
        raw = json.dumps(
            {
                "kind": "action",
                "action": "plan",
                "answer_text": "Let's break the escalation-tracking use case into steps...",
                "params": {},
            }
        )
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                self.USE_CASE_TEXT, default_role="developer", default_target="acme"
            )
        assert decision.kind == "answer"
        assert decision.answer_text == "Let's break the escalation-tracking use case into steps..."
        assert decision.destructive is False

    def test_invalid_action_name_without_answer_text_still_falls_back(self) -> None:
        """No salvageable content at all — the generic fallback is still the
        correct (safe) degrade, unchanged from before."""
        raw = json.dumps({"kind": "action", "action": "plan", "params": {}})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                self.USE_CASE_TEXT, default_role="developer", default_target="acme"
            )
        assert decision.kind == "answer"
        assert decision.answer_text == concierge_service._FALLBACK_ANSWER

    def test_unknown_top_level_kind_salvages_models_own_answer_text(self) -> None:
        """Same salvage behaviour when the model botches the top-level
        `kind` field itself (not just `action`), as long as it supplied
        real answer_text alongside it."""
        raw = json.dumps({"kind": "discussion", "answer_text": "Here's a genuine take..."})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                self.USE_CASE_TEXT, default_role="developer", default_target="acme"
            )
        assert decision.kind == "answer"
        assert decision.answer_text == "Here's a genuine take..."

    def test_unknown_kind_with_blank_answer_text_falls_back(self) -> None:
        """Blank/whitespace-only answer_text is not salvageable content —
        must still degrade to the generic fallback, not an empty reply."""
        raw = json.dumps({"kind": "discussion", "answer_text": "   "})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                self.USE_CASE_TEXT, default_role="developer", default_target="acme"
            )
        assert decision.kind == "answer"
        assert decision.answer_text == concierge_service._FALLBACK_ANSWER

    def test_ambiguous_ish_message_never_yields_action_or_route(self) -> None:
        """Even when a substantive question is phrased with planning
        language, an invalid/ambiguous classifier response must never
        surface as a `route`/`action` decision (which the telegram layer
        would treat as destructive and needing confirmation) — it degrades
        to `answer`, never fabricating a command to run."""
        raw = json.dumps({"kind": "action", "action": "plan", "params": {}})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                self.USE_CASE_TEXT, default_role="developer", default_target="acme"
            )
        assert decision.kind == "answer"
        assert decision.kind not in ("route", "action")


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


class TestSharedOrchestratorSingleton:
    """Regression coverage for the production bug: `telegram.cmd_ask.error`
    'Runner kind gh is already registered ... refusing to silently replace
    it' — caused by concierge_service building its OWN `Orchestrator()`
    (and thus its own PluginManager, which re-scans plugins/*.py and
    collides with the kinds a first PluginManager already registered on the
    process-global RUNNER_MAP) instead of reusing the process-wide shared
    Orchestrator that `chatops_service`/`telegram_bot` already use. See
    `hivepilot/orchestrator.py`'s `_load()` comment for why RUNNER_MAP is
    process-global."""

    def setup_method(self) -> None:
        # chatops_service's module-level singleton is process-global state —
        # reset it around each test so tests don't leak into each other.
        from hivepilot.services import chatops_service

        chatops_service._orchestrator = None

    def teardown_method(self) -> None:
        from hivepilot.services import chatops_service

        chatops_service._orchestrator = None

    def test_returns_same_object_as_chatops_service(self) -> None:
        from hivepilot.services import chatops_service

        chatops_orch = chatops_service._get_orchestrator()
        concierge_orch = concierge_service._get_orchestrator()

        assert concierge_orch is chatops_orch

    def test_does_not_construct_a_second_orchestrator(self) -> None:
        """The core regression: obtaining the concierge orchestrator after
        chatops's singleton already exists must NOT construct a second
        `Orchestrator()` — a second construction means a second
        `PluginManager` re-scanning plugins, which is exactly what raised
        `Runner kind 'gh' is already registered ...` in production."""
        from hivepilot.services import chatops_service

        # chatops_service binds `Orchestrator` at module import time
        # (`from hivepilot.orchestrator import Orchestrator`), so that's the
        # name that must be patched to observe its construction calls.
        with patch.object(chatops_service, "Orchestrator") as mock_orchestrator_cls:
            mock_orchestrator_cls.return_value = MagicMock()

            first = chatops_service._get_orchestrator()
            assert mock_orchestrator_cls.call_count == 1

            second = concierge_service._get_orchestrator()
            assert mock_orchestrator_cls.call_count == 1  # no new construction

        assert second is first

    def test_no_double_registration_error_across_both_entry_points(self) -> None:
        """Mimics the live failure mode: a plugin-provided runner kind (e.g.
        "gh") registers once on a shared registry. Fetching the orchestrator
        via both `chatops_service` and `concierge_service` entry points must
        never trigger a second registration attempt (which is what raised
        the fail-closed "already registered" error in production)."""
        from hivepilot.services import chatops_service

        registered_kinds: dict[str, object] = {}

        def _register(kind: str, cls: object) -> None:
            if kind in registered_kinds and registered_kinds[kind] is not cls:
                raise RuntimeError(
                    f"Runner kind '{kind}' is already registered to "
                    f"{registered_kinds[kind]}; refusing to silently replace it "
                    f"with {cls}"
                )
            registered_kinds[kind] = cls

        fake_orch = MagicMock()
        with patch.object(chatops_service, "Orchestrator", return_value=fake_orch):
            chatops_service._get_orchestrator()
            _register("gh", "GhRunner")  # first (and only) PluginManager scan

            # concierge_service must reuse the same instance — no second scan,
            # no second _register("gh", "GhRunner") call, so no collision.
            concierge_service._get_orchestrator()

        assert registered_kinds == {"gh": "GhRunner"}


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


class TestPromptFilePackaging:
    """Regression coverage for the production bug: the classifier prompt
    wasn't shipped in the wheel (top-level prompts/ isn't packaged), so
    ClaudeRunner always raised "requires a prompt_file for Claude runner"
    and the classifier silently fell back to the fallback answer on EVERY
    pip-installed box. See concierge_service.py's PACKAGING NOTE."""

    def test_prompt_file_lives_inside_the_hivepilot_package(self) -> None:
        # `hivepilot/prompts/concierge.md` — package-relative, not
        # repo-relative — so `hivepilot.prompts` is a subdirectory of the
        # importable `hivepilot` package and ships via package-data in any
        # `pip install`, unlike the old repo-relative `prompts/agents/`.
        import hivepilot

        package_dir = pathlib.Path(hivepilot.__file__).resolve().parent
        assert concierge_service._PROMPT_FILE.is_relative_to(package_dir)
        assert concierge_service._PROMPT_FILE.name == "concierge.md"

    def test_prompt_file_exists_in_the_repo_checkout(self) -> None:
        assert concierge_service._PROMPT_FILE.exists()

    def test_resolve_prompt_file_returns_packaged_path_when_present(self) -> None:
        resolved = concierge_service._resolve_prompt_file()
        assert resolved == str(concierge_service._PROMPT_FILE)
        assert resolved  # never empty

    def test_resolve_prompt_file_falls_back_to_temp_file_when_missing(
        self, tmp_path, monkeypatch
    ) -> None:
        # Simulate the exact production bug: _PROMPT_FILE doesn't exist at
        # runtime. `_resolve_prompt_file` must NEVER return "" in that case
        # (an empty prompt_file is what made ClaudeRunner raise).
        missing = tmp_path / "does-not-exist" / "concierge.md"
        monkeypatch.setattr(concierge_service, "_PROMPT_FILE", missing)
        monkeypatch.setattr(concierge_service, "_prompt_fallback_path", None)

        resolved = concierge_service._resolve_prompt_file()

        assert resolved != ""
        assert pathlib.Path(resolved).exists()
        assert pathlib.Path(resolved).read_text(encoding="utf-8") == (
            concierge_service._CLASSIFIER_PROMPT_TEXT
        )

    def test_route_never_sends_empty_prompt_file_even_when_packaged_file_missing(
        self, tmp_path, monkeypatch
    ) -> None:
        missing = tmp_path / "does-not-exist" / "concierge.md"
        monkeypatch.setattr(concierge_service, "_PROMPT_FILE", missing)
        monkeypatch.setattr(concierge_service, "_prompt_fallback_path", None)

        raw = json.dumps({"kind": "answer", "answer_text": "ok"})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "hi", default_role="developer", default_target="acme"
            )

        assert decision.kind == "answer"  # classification still succeeded
        _, payload = orch.registry.capture_definition.call_args.args
        assert payload.step.prompt_file  # never "" — the bug this guards against

    def test_roster_tolerates_list_roles_error(self, monkeypatch) -> None:
        def _raise():
            raise RuntimeError("roles.yaml is broken")

        monkeypatch.setattr("hivepilot.roles.list_roles", _raise)

        roster = concierge_service._build_roster()  # must not raise

        assert roster == []


# ---------------------------------------------------------------------------
# Conversation memory (per-chat rolling history fed into the classifier
# prompt so a follow-up like "give them the orders" can resolve "them").
# Chat-scoped, in-process, bounded — see concierge_service._MAX_HISTORY_TURNS.
# ---------------------------------------------------------------------------


class TestConversationMemory:
    def teardown_method(self, method) -> None:
        concierge_service.clear_history()

    def test_history_recorded_and_included_in_next_prompt(self) -> None:
        raw1 = json.dumps({"kind": "answer", "answer_text": "ok turn one"})
        orch1 = _orch_with_capture(return_value=raw1)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch1):
            concierge_service.route(
                "hello", default_role="developer", default_target="acme", chat_id=100
            )

        raw2 = json.dumps({"kind": "answer", "answer_text": "ok turn two"})
        orch2 = _orch_with_capture(return_value=raw2)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch2):
            concierge_service.route(
                "follow up", default_role="developer", default_target="acme", chat_id=100
            )

        _, payload = orch2.registry.capture_definition.call_args.args
        prompt = payload.metadata["extra_prompt"]
        assert "hello" in prompt
        assert "ok turn one" in prompt

    def test_history_bounded_to_max_turns(self) -> None:
        raw = json.dumps({"kind": "answer", "answer_text": "ok"})
        for i in range(10):
            orch = _orch_with_capture(return_value=raw)
            with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
                concierge_service.route(
                    f"msg-{i}", default_role="developer", default_target="acme", chat_id=200
                )

        history = concierge_service._get_history(200)
        assert len(history) == concierge_service._MAX_HISTORY_TURNS
        assert history[0].user_text == "msg-4"  # oldest retained (10 - 6)
        assert history[-1].user_text == "msg-9"

    def test_no_chat_id_means_no_memory_recorded(self) -> None:
        raw = json.dumps({"kind": "answer", "answer_text": "ok"})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            concierge_service.route("hi", default_role="developer", default_target="acme")

        assert concierge_service._get_history(None) == []

    def test_history_is_chat_scoped_not_leaked_across_chats(self) -> None:
        """SAFETY: chat A's memory must never be visible to chat B's
        classifier prompt — cross-tenant/cross-chat reference resolution is
        forbidden."""
        raw1 = json.dumps({"kind": "answer", "answer_text": "chat A secret plan"})
        orch1 = _orch_with_capture(return_value=raw1)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch1):
            concierge_service.route(
                "plan for chat A", default_role="developer", default_target="acme", chat_id=1
            )

        raw2 = json.dumps({"kind": "answer", "answer_text": "ok"})
        orch2 = _orch_with_capture(return_value=raw2)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch2):
            concierge_service.route(
                "follow up", default_role="developer", default_target="acme", chat_id=2
            )

        _, payload = orch2.registry.capture_definition.call_args.args
        prompt = payload.metadata["extra_prompt"]
        assert "chat A secret plan" not in prompt
        assert "plan for chat A" not in prompt

    def test_clear_history_for_single_chat_leaves_others_intact(self) -> None:
        concierge_service._record_turn(10, "u1", "c1")
        concierge_service._record_turn(20, "u2", "c2")

        concierge_service.clear_history(10)

        assert concierge_service._get_history(10) == []
        assert len(concierge_service._get_history(20)) == 1


# ---------------------------------------------------------------------------
# Multi-agent dispatch (kind="multi_route") — a follow-up message can
# propose dispatching to MULTIPLE roles in one turn, but ONLY when each
# referent is grounded in the roster (configured) AND in the conversation
# history (actually named earlier) — never guessed. Always destructive;
# the caller (telegram_bot) requires one explicit confirmation for the
# whole batch before anything runs.
# ---------------------------------------------------------------------------


def _leadership_roles() -> list[SimpleNamespace]:
    return [
        _fake_role("cto", "CTO", "Blaise"),
        _fake_role("ciso", "CISO", "Hugo"),
        _fake_role("pm", "PM", "Camille"),
        _fake_role("developer", "Developer", "Gustave"),
    ]


class TestMultiDispatch:
    def teardown_method(self, method) -> None:
        concierge_service.clear_history()

    def test_followup_multi_dispatch_grounded_in_prior_answer(self, monkeypatch) -> None:
        """The motivating scenario: turn 1 names cto/ciso/pm in a genuine
        answer; turn 2 ("give them the orders") resolves "them" to exactly
        those three, grounded in history, and is marked destructive."""
        monkeypatch.setattr("hivepilot.roles.list_roles", lambda: _leadership_roles())

        turn1_answer = (
            "Get Blaise (CTO) to sketch the architecture, Hugo (CISO) to define "
            "the security review, and Camille (PM) to validate the rollout plan."
        )
        raw1 = json.dumps({"kind": "answer", "answer_text": turn1_answer})
        orch1 = _orch_with_capture(return_value=raw1)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch1):
            decision1 = concierge_service.route(
                "how should we approach the new payments feature?",
                default_role="developer",
                default_target="acme",
                chat_id=42,
            )
        assert decision1.kind == "answer"

        raw2 = json.dumps(
            {
                "kind": "multi_route",
                "dispatches": [
                    {"role_key": "cto", "target": "acme", "order": "sketch the architecture"},
                    {"role_key": "ciso", "target": "acme", "order": "define the security review"},
                    {"role_key": "pm", "target": "acme", "order": "validate the rollout plan"},
                ],
            }
        )
        orch2 = _orch_with_capture(return_value=raw2)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch2):
            decision2 = concierge_service.route(
                "donne leur les ordres",
                default_role="developer",
                default_target="acme",
                chat_id=42,
            )

        assert decision2.kind == "multi_route"
        assert decision2.destructive is True
        assert decision2.dispatches is not None
        assert {d.role_key for d in decision2.dispatches} == {"cto", "ciso", "pm"}
        assert all(d.target == "acme" for d in decision2.dispatches)

    def test_ambiguous_do_it_with_no_history_never_dispatches(self, monkeypatch) -> None:
        """SAFETY: an ambiguous message with NOTHING grounding it in history
        must never auto-dispatch — it degrades to a clarifying answer."""
        monkeypatch.setattr("hivepilot.roles.list_roles", lambda: _leadership_roles())
        raw = json.dumps(
            {
                "kind": "multi_route",
                "dispatches": [{"role_key": "cto", "target": "acme", "order": "do it"}],
            }
        )
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "do it", default_role="developer", default_target="acme", chat_id=999
            )
        assert decision.kind == "answer"
        assert decision.kind not in ("route", "action", "multi_route")
        assert decision.dispatches is None

    def test_ungrounded_referent_dropped_not_guessed(self, monkeypatch) -> None:
        """One dispatch is grounded (named in turn 1's answer); the other is
        a hallucinated role never mentioned anywhere — it must be dropped,
        not guessed, while the grounded one still survives."""
        monkeypatch.setattr("hivepilot.roles.list_roles", lambda: _leadership_roles())
        raw1 = json.dumps({"kind": "answer", "answer_text": "Get Blaise (CTO) to sketch X."})
        orch1 = _orch_with_capture(return_value=raw1)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch1):
            concierge_service.route(
                "plan?", default_role="developer", default_target="acme", chat_id=7
            )

        raw2 = json.dumps(
            {
                "kind": "multi_route",
                "dispatches": [
                    {"role_key": "cto", "target": "acme", "order": "sketch X"},
                    {"role_key": "pm", "target": "acme", "order": "validate Z"},
                ],
            }
        )
        orch2 = _orch_with_capture(return_value=raw2)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch2):
            decision = concierge_service.route(
                "give them the orders", default_role="developer", default_target="acme", chat_id=7
            )
        assert decision.kind == "multi_route"
        assert decision.dispatches is not None
        assert {d.role_key for d in decision.dispatches} == {"cto"}

    def test_all_referents_ungrounded_falls_back_to_clarifying_answer(self, monkeypatch) -> None:
        monkeypatch.setattr("hivepilot.roles.list_roles", lambda: _leadership_roles())
        raw = json.dumps(
            {
                "kind": "multi_route",
                "dispatches": [{"role_key": "pm", "target": "acme", "order": "validate Z"}],
            }
        )
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "give them the orders", default_role="developer", default_target="acme", chat_id=8
            )
        assert decision.kind == "answer"
        assert decision.answer_text

    def test_unknown_role_in_dispatch_dropped(self, monkeypatch) -> None:
        """A role name that isn't a configured role at all (not in the
        roster) must be dropped even if it was mentioned in history."""
        monkeypatch.setattr("hivepilot.roles.list_roles", lambda: _leadership_roles())
        raw1 = json.dumps({"kind": "answer", "answer_text": "Get Blaise (CTO) and Zorg to help."})
        orch1 = _orch_with_capture(return_value=raw1)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch1):
            concierge_service.route(
                "plan?", default_role="developer", default_target="acme", chat_id=9
            )

        raw2 = json.dumps(
            {
                "kind": "multi_route",
                "dispatches": [
                    {"role_key": "cto", "target": "acme", "order": "sketch X"},
                    {"role_key": "zorg", "target": "acme", "order": "help"},
                ],
            }
        )
        orch2 = _orch_with_capture(return_value=raw2)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch2):
            decision = concierge_service.route(
                "give them the orders", default_role="developer", default_target="acme", chat_id=9
            )
        assert decision.kind == "multi_route"
        assert {d.role_key for d in decision.dispatches} == {"cto"}

    def test_multi_route_missing_dispatches_field_falls_back(self) -> None:
        raw = json.dumps({"kind": "multi_route"})
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "give them the orders", default_role="developer", default_target="acme", chat_id=10
            )
        assert decision.kind == "answer"

    def test_multi_route_unknown_project_dropped(self, monkeypatch) -> None:
        monkeypatch.setattr("hivepilot.roles.list_roles", lambda: _leadership_roles())
        raw1 = json.dumps({"kind": "answer", "answer_text": "Get Blaise (CTO) to sketch X."})
        orch1 = _orch_with_capture(return_value=raw1)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch1):
            concierge_service.route(
                "plan?", default_role="developer", default_target="acme", chat_id=11
            )

        raw2 = json.dumps(
            {
                "kind": "multi_route",
                "dispatches": [
                    {"role_key": "cto", "target": "not-a-real-project", "order": "sketch X"}
                ],
            }
        )
        orch2 = _orch_with_capture(return_value=raw2)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch2):
            decision = concierge_service.route(
                "give them the orders", default_role="developer", default_target="acme", chat_id=11
            )
        assert decision.kind == "answer"

    def test_multi_route_always_destructive_even_if_model_says_otherwise(self, monkeypatch) -> None:
        monkeypatch.setattr("hivepilot.roles.list_roles", lambda: _leadership_roles())
        raw1 = json.dumps({"kind": "answer", "answer_text": "Get Blaise (CTO) to sketch X."})
        orch1 = _orch_with_capture(return_value=raw1)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch1):
            concierge_service.route(
                "plan?", default_role="developer", default_target="acme", chat_id=12
            )

        raw2 = json.dumps(
            {
                "kind": "multi_route",
                "dispatches": [{"role_key": "cto", "target": "acme", "order": "sketch X"}],
                "destructive": False,
            }
        )
        orch2 = _orch_with_capture(return_value=raw2)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch2):
            decision = concierge_service.route(
                "give them the orders", default_role="developer", default_target="acme", chat_id=12
            )
        assert decision.destructive is True

    def test_no_grounding_needed_check_history_none_never_crashes(self) -> None:
        """chat_id=None (memory disabled) must never crash `_clamp`'s
        grounding check — with no history, every multi_route dispatch is
        simply ungrounded and dropped."""
        raw = json.dumps(
            {
                "kind": "multi_route",
                "dispatches": [{"role_key": "developer", "target": "acme", "order": "do it"}],
            }
        )
        orch = _orch_with_capture(return_value=raw)
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "give them the orders", default_role="developer", default_target="acme"
            )
        assert decision.kind == "answer"


class TestGroundingSnapshotDisplaysLocalTime:
    """Reproduces the production incident: `_grounding_snapshot` feeds the
    classifier LLM raw stored timestamps, which it then echoes back to the
    operator verbatim (e.g. "failed this morning at 09:08" for an event that
    actually happened at 11:08 local time). The snapshot text itself must
    already carry the LOCAL, marked time — not the raw UTC string — so any
    NL answer built from it reads correctly."""

    def test_run_line_uses_local_display_time_not_raw_utc(self, monkeypatch) -> None:
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
        monkeypatch.setattr(
            "hivepilot.services.state_service.list_recent_runs",
            lambda limit=5: [
                {
                    "status": "failed",
                    "project": "groomer",
                    "task": "scan",
                    "started_at": "2026-07-27 09:08:32",
                }
            ],
        )
        monkeypatch.setattr("hivepilot.services.state_service.get_pending_approvals", lambda: [])
        snapshot = concierge_service._grounding_snapshot()
        assert "09:08" not in snapshot
        assert "11:08" in snapshot
        assert "CEST" in snapshot


# ---------------------------------------------------------------------------
# Pending follow-up offers — the concierge asks "want me to investigate?" and
# must be able to honour a bare "yes"/"oui" answering it.
#
# Production defect this covers: the classifier's `answer_text` was free-form
# prose, so the model could INVITE a reply the router had no way to execute.
# The operator answered "yes" and got the generic `_FALLBACK_ANSWER`.
#
# Fail-closed properties under test: an offer is bound to the conversation AND
# to the person who was asked, expires, is never honoured on an ambiguous
# reply, and is never even *rendered* unless it clamps to something the router
# can actually execute.
# ---------------------------------------------------------------------------


_OFFER_ROUTE = {
    "kind": "route",
    "role_key": "developer",
    "target": "acme",
    "order": "investigate the groomer-scan failure",
}


_UNSET = object()


def _answer_with_offer(
    answer_text: str = "groomer-scan failed this morning.",
    follow_up=_UNSET,
) -> str:
    return json.dumps(
        {
            "kind": "answer",
            "answer_text": answer_text,
            "follow_up": dict(_OFFER_ROUTE) if follow_up is _UNSET else follow_up,
        }
    )


def _exploding_orch() -> MagicMock:
    """An orchestrator whose classifier call fails the test if invoked — proves
    a resolved yes/no answer never costs an LLM round-trip and never depends on
    the model to interpret the affirmative."""
    return _orch_with_capture(
        side_effect=AssertionError("classifier must not be called for a resolved offer reply")
    )


def _make_offer(
    conversation_id: str = "telegram:1",
    user_id: str = "operator-1",
    raw: str | None = None,
) -> concierge_service.ConciergeDecision:
    orch = _orch_with_capture(return_value=raw if raw is not None else _answer_with_offer())
    with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
        return concierge_service.route(
            "tout va bien ?",
            default_role="developer",
            default_target="acme",
            conversation_id=conversation_id,
            user_id=user_id,
        )


def _reply(
    text: str,
    conversation_id: str = "telegram:1",
    user_id: str = "operator-1",
    orch: MagicMock | None = None,
) -> concierge_service.ConciergeDecision:
    with patch.object(
        concierge_service, "_get_orchestrator", return_value=orch or _exploding_orch()
    ):
        return concierge_service.route(
            text,
            default_role="developer",
            default_target="acme",
            conversation_id=conversation_id,
            user_id=user_id,
        )


class TestPendingOffer:
    def teardown_method(self, method) -> None:
        concierge_service.clear_pending_offers()
        concierge_service.clear_history()

    # -- the offer itself ---------------------------------------------------

    def test_offer_is_rendered_by_code_not_by_the_model(self) -> None:
        """The invitation the operator reads must be OURS, so the set of
        offers the bot can make is exactly the set the router can execute."""
        decision = _make_offer()

        assert decision.kind == "answer"
        assert decision.answer_text is not None
        assert decision.answer_text.startswith("groomer-scan failed this morning.")
        assert '"yes"' in decision.answer_text
        assert "developer" in decision.answer_text

    def test_affirmative_answers_the_offer_and_yields_the_executable_action(self) -> None:
        _make_offer()

        decision = _reply("yes")

        assert decision.kind == "route"
        assert decision.role_key == "developer"
        assert decision.target == "acme"
        assert decision.order == "investigate the groomer-scan failure"
        # Still gated by the existing destructive-confirmation path.
        assert decision.destructive is True

    def test_offer_is_single_use(self) -> None:
        _make_offer()
        _reply("yes")

        orch = _orch_with_capture(return_value=json.dumps({"kind": "answer", "answer_text": "?"}))
        again = _reply("yes", orch=orch)

        assert again.kind == "answer"
        assert orch.registry.capture_definition.called

    # -- vocabulary, both languages ----------------------------------------

    @pytest.mark.parametrize(
        "text",
        ["yes", "Yes!", "ok", "OK.", "sure", "go", "go ahead", "do it", "yep", "confirm"],
    )
    def test_english_affirmatives(self, text: str) -> None:
        _make_offer()
        assert _reply(text).kind == "route"

    @pytest.mark.parametrize(
        "text",
        ["oui", "OUI !", "ouais", "vas-y", "vas y", "allez-y", "fais-le", "d'accord", "c'est bon"],
    )
    def test_french_affirmatives(self, text: str) -> None:
        _make_offer()
        assert _reply(text).kind == "route"

    @pytest.mark.parametrize("text", ["no", "Nope", "cancel", "non", "non merci", "laisse tomber"])
    def test_negatives_dismiss_cleanly(self, text: str) -> None:
        _make_offer()

        decision = _reply(text)

        assert decision.kind == "answer"
        assert decision.answer_text == concierge_service._OFFER_DECLINED_TEXT
        assert concierge_service._pending_offers == {}

    # -- fail-closed --------------------------------------------------------

    @pytest.mark.parametrize(
        "text",
        [
            "yes but check the logs first",
            "peut-etre",
            "peut-être",
            "hmm",
            "yes/no",
            "oui mais pas maintenant",
            "",
            "   ",
        ],
    )
    def test_ambiguous_reply_executes_nothing_and_leaves_the_offer_pending(self, text: str) -> None:
        _make_offer()

        orch = _orch_with_capture(
            return_value=json.dumps({"kind": "answer", "answer_text": "not sure"})
        )
        decision = _reply(text, orch=orch)

        assert decision.kind == "answer"
        assert "telegram:1" in concierge_service._pending_offers
        # …and the operator can still say yes afterwards.
        assert _reply("oui").kind == "route"

    def test_a_different_persons_affirmative_never_triggers_the_offer(self) -> None:
        """A colleague's unrelated "yes" in a shared channel must not fire
        someone else's pending action."""
        _make_offer(conversation_id="slack:C1", user_id="operator-1")

        orch = _orch_with_capture(
            return_value=json.dumps({"kind": "answer", "answer_text": "hello colleague"})
        )
        decision = _reply("yes", conversation_id="slack:C1", user_id="colleague-2", orch=orch)

        assert decision.kind == "answer"
        assert orch.registry.capture_definition.called  # fell through to normal handling
        # The real owner's offer survives untouched.
        assert _reply("oui", conversation_id="slack:C1", user_id="operator-1").kind == "route"

    def test_expired_offer_is_not_honoured_and_falls_through(self) -> None:
        _make_offer()
        stored = concierge_service._pending_offers["telegram:1"]
        concierge_service._pending_offers["telegram:1"] = dataclasses.replace(
            stored, expires_at=time.time() - 1
        )

        orch = _orch_with_capture(
            return_value=json.dumps({"kind": "answer", "answer_text": "normal handling"})
        )
        decision = _reply("oui", orch=orch)

        assert decision.kind == "answer"
        assert decision.answer_text == "normal handling"
        assert orch.registry.capture_definition.called
        assert concierge_service._pending_offers == {}

    def test_offer_is_conversation_scoped(self) -> None:
        _make_offer(conversation_id="telegram:1", user_id="operator-1")

        orch = _orch_with_capture(
            return_value=json.dumps({"kind": "answer", "answer_text": "other chat"})
        )
        decision = _reply("oui", conversation_id="telegram:2", user_id="operator-1", orch=orch)

        assert decision.kind == "answer"
        assert "telegram:1" in concierge_service._pending_offers

    @pytest.mark.parametrize(
        ("conversation_id", "user_id"),
        [(None, "operator-1"), ("telegram:1", None), (None, None), ("", "operator-1")],
    )
    def test_missing_conversation_or_owner_never_offers(
        self, conversation_id: str | None, user_id: str | None
    ) -> None:
        """A missing id means "do not execute", never "execute anyway" — and
        we must not even render an invitation we could not honour."""
        orch = _orch_with_capture(return_value=_answer_with_offer())
        with patch.object(concierge_service, "_get_orchestrator", return_value=orch):
            decision = concierge_service.route(
                "tout va bien ?",
                default_role="developer",
                default_target="acme",
                conversation_id=conversation_id,
                user_id=user_id,
            )

        assert decision.answer_text == "groomer-scan failed this morning."
        assert concierge_service._pending_offers == {}

    def test_owner_less_stored_offer_is_never_honoured(self) -> None:
        concierge_service._store_offer(
            "telegram:1", "", concierge_service.ConciergeDecision("route")
        )
        assert concierge_service._pending_offers == {}

    @pytest.mark.parametrize(
        "follow_up",
        [
            {"kind": "route", "role_key": "ghost", "target": "acme", "order": "x"},
            {"kind": "route", "role_key": "developer", "target": "unknown-project", "order": "x"},
            {"kind": "answer", "answer_text": "not an offer"},
            {"kind": "action", "action": "meditate"},
            {"kind": "action", "action": "approve"},  # no run_id -> unexecutable
            "not-an-object",
            None,
        ],
    )
    def test_unexecutable_follow_up_is_never_offered(self, follow_up) -> None:
        """Never offer what cannot be honoured: a follow-up that does not clamp
        to a real, executable route/action is dropped AND no invitation is
        rendered."""
        _make_offer(raw=_answer_with_offer(follow_up=follow_up))

        assert concierge_service._pending_offers == {}
        orch = _orch_with_capture(
            return_value=json.dumps({"kind": "answer", "answer_text": "normal handling"})
        )
        assert _reply("oui", orch=orch).answer_text == "normal handling"

    def test_action_offer_round_trips(self) -> None:
        _make_offer(
            raw=_answer_with_offer(
                follow_up={"kind": "action", "action": "approve", "params": {"run_id": 42}}
            )
        )

        decision = _reply("vas-y")

        assert decision.kind == "action"
        assert decision.action == "approve"
        assert decision.params == {"run_id": 42}
        assert decision.destructive is True


class TestReplyVocabulary:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Oui", "yes"),
            ("  OUI !!  ", "yes"),
            ("vas-y", "yes"),
            ("Vas-y…", "yes"),
            ("d’accord", "yes"),  # typographic apostrophe
            ("Bien sûr", "yes"),
            ("yes", "yes"),
            ("Go ahead.", "yes"),
            ("non", "no"),
            ("Non, merci", "no"),
            ("arrête", "no"),
            ("nope", "no"),
            ("", None),
            ("   ", None),
            ("yes but wait", None),
            ("oui si tu veux", None),
            ("okay so what about the other run", None),
            ("no idea", None),
        ],
    )
    def test_classify_reply(self, text: str, expected: str | None) -> None:
        assert concierge_service._classify_reply(text) == expected

    def test_affirmative_and_negative_vocabularies_are_disjoint(self) -> None:
        assert not (concierge_service._AFFIRMATIVE_REPLIES & concierge_service._NEGATIVE_REPLIES)
