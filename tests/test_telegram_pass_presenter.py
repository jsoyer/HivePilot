"""HP-102: Telegram Approvals door calls the shared decide_approval()."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hivepilot.pass_store import APPROVED, PENDING, create_pending, get
from hivepilot.presenters import present, reset_pending, telegram_keyboard
from hivepilot.services import telegram_bot
from hivepilot.services.telegram_doors import APPROVALS, INBOX


@pytest.fixture(autouse=True)
def _reset_presenter() -> Iterator[None]:
    reset_pending()
    yield
    reset_pending()


def _query(*, data: str, user_id: int = 7, chat_id: int = 99) -> SimpleNamespace:
    query = SimpleNamespace()
    query.data = data
    query.from_user = SimpleNamespace(id=user_id, username="jerome")
    query.message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        message_thread_id=21,
        reply_text=AsyncMock(),
    )
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def test_pass_keyboard_is_approvals_door_only() -> None:
    proposal = create_pending(kind="tool", payload={"token": "Bash"})
    card = present(proposal, owner_id="7")
    assert telegram_keyboard(card, door=APPROVALS) is not None
    assert telegram_keyboard(card, door=INBOX) is None


def test_callback_pass_approval_uses_shared_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telegram_bot, "_require_allowed", lambda _chat: True)
    proposal = create_pending(kind="partition", project="example-api", task="docs")
    present(proposal, owner_id="7")
    keyboard = telegram_keyboard(present(proposal, owner_id="7"), door=APPROVALS)
    assert keyboard is not None
    data = keyboard.buttons[0][0]["callback_data"]
    update = SimpleNamespace(callback_query=_query(data=data, user_id=7))
    asyncio.run(telegram_bot._callback_pass_approval(update, MagicMock()))
    update.callback_query.edit_message_text.assert_called()
    assert get(proposal.id).status == APPROVED


def test_callback_pass_approval_owner_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telegram_bot, "_require_allowed", lambda _chat: True)
    proposal = create_pending(kind="partition", project="example-api", task="docs")
    present(proposal, owner_id="7")
    data = f"pass:approve:{proposal.id}"
    update = SimpleNamespace(callback_query=_query(data=data, user_id=99))
    asyncio.run(telegram_bot._callback_pass_approval(update, MagicMock()))
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "Error" in text
    assert get(proposal.id).status == PENDING


def test_send_pass_keyboard_noop_without_card(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    card = SimpleNamespace(
        approval_id="x", kind="tool", title="t", summary="s", project="", task=""
    )
    with patch("hivepilot.services.telegram_bot.telegram_keyboard", return_value=None):
        asyncio.run(
            telegram_bot._send_pass_keyboard_message(bot, chat_id=1, card=card, message_thread_id=2)
        )
    bot.send_message.assert_not_called()
