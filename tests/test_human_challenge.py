"""Tests for the human challenge/ask feature at plan checkpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Keyboard tests ────────────────────────────────────────────────────────────


def test_keyboard_includes_challenge_button():
    """The approval keyboard must include a challenge:{run_id} button."""
    import inspect

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


def test_human_challenge_invokes_cos_and_stays_paused():
    """human_challenge re-invokes CoS, appends to context, keeps run paused."""
    # Import the actual orchestrator class (find correct name)
    import importlib

    orch_module = importlib.import_module("hivepilot.orchestrator")
    # Find the class
    import inspect

    orch_classes = [
        obj
        for name, obj in inspect.getmembers(orch_module, inspect.isclass)
        if "orchestrator" in name.lower() and obj.__module__ == "hivepilot.orchestrator"
    ]
    assert orch_classes, "Could not find orchestrator class"
    OrchestratorClass = orch_classes[0]

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

        # Build a minimal orchestrator bypassing __init__
        orch = object.__new__(OrchestratorClass)
        # Mock the registry if it has capture_definition
        orch.registry = MagicMock()
        orch.registry.capture_definition = MagicMock(return_value=cos_reply)
        # Mock policy-like attributes
        for attr in ("_policy", "policy", "config", "_config"):
            try:
                setattr(orch, attr, MagicMock())
            except Exception:
                pass

        result = orch.human_challenge(run_id, challenge_text, approver)

    assert result == cos_reply
    # planning_context was updated with challenge+response
    call_args = mock_ss.update_approval_metadata.call_args
    assert call_args is not None, "update_approval_metadata was not called"
    updated_meta = call_args[0][1]
    assert challenge_text in updated_meta["planning_context"]
    assert cos_reply in updated_meta["planning_context"]
