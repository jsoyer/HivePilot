"""Tests for the human challenge/ask feature at plan checkpoints."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Keyboard tests ────────────────────────────────────────────────────────────


def test_keyboard_includes_challenge_button():
    """The approval keyboard must include a challenge:{run_id} button."""

    from hivepilot.services import telegram_bot

    src = inspect.getsource(telegram_bot._send_approval_keyboard_message)
    assert "challenge:" in src, "challenge button missing from _send_approval_keyboard_message"


def test_callback_pattern_matches_challenge():
    """The CallbackQueryHandler pattern must match 'challenge:42'."""
    import re

    pattern = r"^(approve|deny|challenge):\d+$"
    assert re.match(pattern, "challenge:42")
    assert re.match(pattern, "approve:1")
    assert re.match(pattern, "deny:99")
    assert not re.match(pattern, "foo:1")


# ── Challenge tap → pending state ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_challenge_tap_sets_pending_state():
    """A challenge: callback tap sets _pending_challenges and replies to user."""
    from hivepilot.services import telegram_bot

    run_id = 42
    chat_id = 999

    mock_query = MagicMock()
    mock_query.data = f"challenge:{run_id}"
    mock_query.answer = AsyncMock()
    mock_query.message = MagicMock()
    mock_query.message.chat.id = chat_id
    # A plain (non-forum) Telegram Message always carries a real
    # `message_thread_id` attribute defaulting to None -- set it explicitly
    # so this mock accurately represents that case (a bare MagicMock()
    # would otherwise auto-vivify a truthy, non-None attribute here and get
    # mistaken for a forum-topic message by `_challenge_key`).
    mock_query.message.message_thread_id = None
    mock_query.message.reply_text = AsyncMock()
    mock_query.from_user.username = "testuser"

    mock_update = MagicMock()
    mock_update.callback_query = mock_query

    with patch.object(telegram_bot, "_require_allowed", return_value=True):
        telegram_bot._pending_challenges.clear()
        await telegram_bot._callback_approval(mock_update, MagicMock())

    assert chat_id in telegram_bot._pending_challenges
    stored_run_id, stored_approver = telegram_bot._pending_challenges[chat_id]
    assert stored_run_id == run_id
    mock_query.message.reply_text.assert_called_once()
    reply_text = mock_query.message.reply_text.call_args[0][0]
    assert str(run_id) in reply_text


# ── orchestrator.human_challenge ─────────────────────────────────────────────


def _orchestrator_class():
    """Locate the live Orchestrator class without importing its (private) name."""
    orch_module = importlib.import_module("hivepilot.orchestrator")
    orch_classes = [
        obj
        for name, obj in inspect.getmembers(orch_module, inspect.isclass)
        if "orchestrator" in name.lower() and obj.__module__ == "hivepilot.orchestrator"
    ]
    assert orch_classes, "Could not find orchestrator class"
    return orch_classes[0]


def _make_orchestrator(cos_reply: str = "ok"):
    """Build a minimal orchestrator instance (bypassing __init__) with a
    mocked registry, ready to call human_challenge()."""
    OrchestratorClass = _orchestrator_class()
    orch = object.__new__(OrchestratorClass)
    orch.registry = MagicMock()
    orch.registry.capture_definition = MagicMock(return_value=cos_reply)
    for attr in ("_policy", "policy", "config", "_config"):
        try:
            setattr(orch, attr, MagicMock())
        except Exception:
            pass
    return orch


def test_human_challenge_invokes_cos_and_stays_paused():
    """human_challenge re-invokes CoS, appends to context, keeps run paused."""
    run_id = 7
    challenge_text = "Why is step 3 before step 2?"
    approver = "telegram:alice"
    cos_reply = "Good point. Step 3 was moved before step 2 because of dependency X."

    approval_row = {
        "run_id": run_id,
        "project": "myproject",
        "task": "mytask",
        "status": "pending",
        "metadata": '{"kind": "pipeline_checkpoint", "planning_context": "Plan: do A then B then C.", "pipeline": ["step1", "step2"], "resume_from_index": 0}',
    }

    with (
        patch("hivepilot.orchestrator.state_service") as mock_ss,
        patch("hivepilot.orchestrator.log_challenge_interaction"),
    ):
        mock_ss.get_approval.return_value = approval_row
        orch = _make_orchestrator(cos_reply)

        result = orch.human_challenge(run_id, challenge_text, approver)

    assert result == cos_reply
    # planning_context was updated with challenge+response
    call_args = mock_ss.update_approval_metadata.call_args
    assert call_args is not None, "update_approval_metadata was not called"
    updated_meta = call_args[0][1]
    assert challenge_text in updated_meta["planning_context"]
    assert cos_reply in updated_meta["planning_context"]


_APPROVAL_ROW = {
    "run_id": 1,
    "project": "myproject",
    "task": "mytask",
    "status": "pending",
    "metadata": '{"planning_context": "Plan."}',
}


def test_human_challenge_resolves_real_cos_role_key():
    """Regression for the live bug: 'Step human_challenge:cos requires a
    prompt_file for Claude runner'.

    The hard-coded lookup key used to be the literal string "cos", which
    never matches any entry in ROLES (roles.yaml/`_DEFAULT_ROLES` name the
    role "chief_of_staff" — "cos" is only a Telegram command *alias*, see
    `telegram_bot._ALIAS_HANDLERS`). `get_role("cos")` therefore ALWAYS
    raised KeyError, `cos_role` was ALWAYS None, and the `else` fallback
    branch handed the Claude runner a hard-coded empty `prompt_file` no
    matter what roles.yaml declared. This asserts the real "chief_of_staff"
    role is now resolved and reaches the runner with a non-empty, existing
    prompt_file.
    """
    with (
        patch("hivepilot.orchestrator.state_service") as mock_ss,
        patch("hivepilot.orchestrator.log_challenge_interaction"),
    ):
        mock_ss.get_approval.return_value = _APPROVAL_ROW
        orch = _make_orchestrator()

        orch.human_challenge(1, "why?", "alice")

    payload = orch.registry.capture_definition.call_args[0][1]
    assert payload.step.prompt_file, "prompt_file must not be empty"
    assert Path(payload.step.prompt_file).exists(), "resolved prompt_file must exist on disk"


def test_human_challenge_resolves_prompt_file_regardless_of_cwd(tmp_path, monkeypatch):
    """Live-bug regression: with the process CWD changed away from the repo
    (mirrors the OpenRC service's cwd=/), human_challenge() must still
    resolve the Chief-of-Staff role's real prompt file through the settings
    config chain -- never a bare, cwd-relative Path.exists() check -- and
    must not hand the runner an empty prompt_file."""
    monkeypatch.chdir(tmp_path)

    with (
        patch("hivepilot.orchestrator.state_service") as mock_ss,
        patch("hivepilot.orchestrator.log_challenge_interaction"),
    ):
        mock_ss.get_approval.return_value = _APPROVAL_ROW
        orch = _make_orchestrator()

        orch.human_challenge(1, "why?", "alice")

    payload = orch.registry.capture_definition.call_args[0][1]
    assert payload.step.prompt_file
    assert Path(payload.step.prompt_file).exists()


def test_human_challenge_role_not_found_falls_back_to_temp_prompt():
    """If the CoS role cannot be resolved at all (e.g. a minimal roles.yaml
    with no chief_of_staff/cos entry -- the `cos_role is None` branch),
    human_challenge() must still work: it synthesizes a temp prompt file
    instead of handing the Claude runner a hard-coded empty prompt_file."""
    with (
        patch("hivepilot.orchestrator.state_service") as mock_ss,
        patch("hivepilot.orchestrator.log_challenge_interaction"),
        patch("hivepilot.roles.get_role", side_effect=KeyError("no such role")),
    ):
        mock_ss.get_approval.return_value = _APPROVAL_ROW
        orch = _make_orchestrator()

        result = orch.human_challenge(1, "why?", "alice")

    assert result == "ok"
    payload = orch.registry.capture_definition.call_args[0][1]
    assert payload.step.prompt_file, "prompt_file must not be empty even with no CoS role"
    assert Path(payload.step.prompt_file).exists()


def test_human_challenge_role_with_missing_prompt_file_falls_back_to_temp_prompt(tmp_path):
    """A role that resolves but whose declared prompt_file is absent from
    disk (stale roles.yaml override, moved file, etc.) must not crash the
    Claude runner with an empty prompt_file either -- same temp-prompt
    fallback as the role-not-found case."""
    from hivepilot.roles import Role

    fake_role = Role(
        name="chief_of_staff",
        title="Chief of Staff",
        prompt_file=tmp_path / "does_not_exist.md",
        model_profile="automation",
        inputs=[],
        outputs=[],
        can_block=False,
        order=2,
        runner="claude",
    )

    with (
        patch("hivepilot.orchestrator.state_service") as mock_ss,
        patch("hivepilot.orchestrator.log_challenge_interaction"),
        patch("hivepilot.roles.get_role", return_value=fake_role),
    ):
        mock_ss.get_approval.return_value = _APPROVAL_ROW
        orch = _make_orchestrator()

        orch.human_challenge(1, "why?", "alice")

    payload = orch.registry.capture_definition.call_args[0][1]
    assert payload.step.prompt_file
    assert payload.step.prompt_file != str(fake_role.prompt_file)
    assert Path(payload.step.prompt_file).exists()
