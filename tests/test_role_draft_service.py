"""Tests for hivepilot/services/role_draft_service.py — Agent Studio Phase 3
(HP-27). `draft_role(spec)` turns a natural-language spec into a RoleWrite
proposal via the concierge/OSS model.

Every test mocks `concierge_service._get_orchestrator()` so no real LLM
call, subprocess, or network access happens. Fail-closed behaviour (empty
spec, LLM error, malformed JSON, no-tools invariant) is the primary
contract — the draft is never persisted.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.roles import validate_role_fields
from hivepilot.services import role_draft_service
from hivepilot.services.role_draft_service import RoleDraftError, draft_role, lint_role_draft


def _orch_with_capture(return_value: str | None = None, side_effect=None) -> MagicMock:
    orch = MagicMock()
    if side_effect is not None:
        orch.registry.capture_definition.side_effect = side_effect
    else:
        orch.registry.capture_definition.return_value = return_value
    return orch


def _auditor_json(**over) -> str:
    body = {
        "name": "tf_auditor",
        "title": "Terraform Security Auditor",
        "display_name": "Ada",
        "model_profile": "architecture",
        "runner": "claude",
        "prompt_text": "Review Terraform for security defects. Emit a report.",
        "inputs": ["terraform"],
        "outputs": ["security_report"],
        "can_block": True,
        "order": 6,
    }
    body.update(over)
    return json.dumps(body)


@pytest.fixture(autouse=True)
def _api_key_for_api_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the concierge helper in api mode so most tests don't require a
    cli `tools` flag — the no-tools suite sets cli mode explicitly."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    monkeypatch.setattr(
        role_draft_service.concierge_service.settings, "chatops_concierge_mode", "api"
    )
    monkeypatch.setattr(
        role_draft_service.concierge_service.settings, "chatops_concierge_model", "haiku"
    )


class TestDraftRoleHappyPath:
    def test_sample_spec_returns_linter_passing_skeleton(self) -> None:
        orch = _orch_with_capture(return_value=_auditor_json())
        with patch.object(role_draft_service.concierge_service, "_get_orchestrator", return_value=orch):
            result = draft_role(
                "a security auditor that reviews Terraform, can block release"
            )
        assert result.saved is False
        draft = result.fields
        assert draft["name"] == "tf_auditor"
        assert draft["title"] == "Terraform Security Auditor"
        assert draft["runner"] == "claude"
        assert draft["model_profile"] == "architecture"
        assert draft["can_block"] is True
        assert draft["inputs"] == ["terraform"]
        assert draft["outputs"] == ["security_report"]
        assert "Terraform" in draft["prompt_text"]
        validate_role_fields(draft)
        assert lint_role_draft(draft) == []
        assert result.lint == []

    def test_does_not_persist_to_the_store(self) -> None:
        from hivepilot.services import state_service

        orch = _orch_with_capture(return_value=_auditor_json())
        with patch.object(role_draft_service.concierge_service, "_get_orchestrator", return_value=orch):
            draft_role("a security auditor that reviews Terraform, can block release")
        assert state_service.get_role_row("tf_auditor") is None

    def test_strips_dangerous_capabilities(self) -> None:
        raw = _auditor_json(
            permission_mode="bypassPermissions",
            allowed_tools=["Bash", "Edit"],
        )
        orch = _orch_with_capture(return_value=raw)
        with patch.object(role_draft_service.concierge_service, "_get_orchestrator", return_value=orch):
            result = draft_role("auditor with tools please")
        assert "permission_mode" not in result.fields
        assert "allowed_tools" not in result.fields
        assert any("permission_mode" in note for note in result.notes)
        assert any("allowed_tools" in note for note in result.notes)
        assert lint_role_draft(result.fields) == []

    def test_accepts_fenced_json(self) -> None:
        fenced = "```json\n" + _auditor_json() + "\n```"
        orch = _orch_with_capture(return_value=fenced)
        with patch.object(role_draft_service.concierge_service, "_get_orchestrator", return_value=orch):
            result = draft_role("security auditor")
        assert result.fields["name"] == "tf_auditor"


class TestDraftRoleFailClosed:
    def test_empty_spec_raises(self) -> None:
        with pytest.raises(RoleDraftError, match="empty spec"):
            draft_role("   ")

    def test_llm_error_raises_and_does_not_invent_a_role(self) -> None:
        orch = _orch_with_capture(side_effect=RuntimeError("timeout"))
        with patch.object(role_draft_service.concierge_service, "_get_orchestrator", return_value=orch):
            with pytest.raises(RoleDraftError, match="did not return"):
                draft_role("a security auditor")

    def test_malformed_json_raises(self) -> None:
        orch = _orch_with_capture(return_value="not json at all")
        with patch.object(role_draft_service.concierge_service, "_get_orchestrator", return_value=orch):
            with pytest.raises(RoleDraftError, match="unparseable"):
                draft_role("a security auditor")

    def test_empty_model_output_raises(self) -> None:
        orch = _orch_with_capture(return_value="   ")
        with patch.object(role_draft_service.concierge_service, "_get_orchestrator", return_value=orch):
            with pytest.raises(RoleDraftError, match="empty"):
                draft_role("a security auditor")


class TestNoToolsPath:
    def test_cli_mode_sets_empty_tools_and_no_permission_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            role_draft_service.concierge_service.settings, "chatops_concierge_mode", "cli"
        )
        orch = _orch_with_capture(return_value=_auditor_json())
        with patch.object(role_draft_service.concierge_service, "_get_orchestrator", return_value=orch):
            draft_role("a security auditor")
        runner_def, payload = orch.registry.capture_definition.call_args.args
        assert runner_def.options.get("mode") == "cli"
        assert runner_def.options.get("tools") == ""
        assert "permission_mode" not in runner_def.options
        assert "Terraform" in payload.metadata["extra_prompt"] or "auditor" in payload.metadata[
            "extra_prompt"
        ].lower()

    def test_cli_no_tools_invariant_violation_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            role_draft_service.concierge_service.settings, "chatops_concierge_mode", "cli"
        )
        monkeypatch.setattr(
            role_draft_service.concierge_service,
            "_build_classifier_options",
            lambda mode: {"mode": mode, "tools": "Bash"},
        )
        orch = _orch_with_capture(return_value=_auditor_json())
        with patch.object(role_draft_service.concierge_service, "_get_orchestrator", return_value=orch):
            with pytest.raises(RoleDraftError, match="tools enabled"):
                draft_role("a security auditor")
        orch.registry.capture_definition.assert_not_called()


class TestSanitizeAndLint:
    def test_unknown_runner_and_profile_are_coerced(self) -> None:
        raw = _auditor_json(runner="not-a-runner", model_profile="turbo")
        orch = _orch_with_capture(return_value=raw)
        with patch.object(role_draft_service.concierge_service, "_get_orchestrator", return_value=orch):
            result = draft_role("auditor")
        assert result.fields["runner"] == "claude"
        assert result.fields["model_profile"] == "architecture"
        assert result.lint == []

    def test_missing_prompt_is_synthesized_from_spec(self) -> None:
        raw = _auditor_json()
        data = json.loads(raw)
        data.pop("prompt_text")
        orch = _orch_with_capture(return_value=json.dumps(data))
        with patch.object(role_draft_service.concierge_service, "_get_orchestrator", return_value=orch):
            result = draft_role("reviews Terraform and can block release")
        assert result.fields["prompt_text"]
        assert "Terraform" in result.fields["prompt_text"]
        assert lint_role_draft(result.fields) == []

    def test_collision_is_a_lint_finding_not_a_save(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(role_draft_service, "api_roster", lambda: [{"name": "tf_auditor"}])
        orch = _orch_with_capture(return_value=_auditor_json())
        with patch.object(role_draft_service.concierge_service, "_get_orchestrator", return_value=orch):
            result = draft_role("auditor")
        assert result.saved is False
        assert any("already exists" in item for item in result.lint)
