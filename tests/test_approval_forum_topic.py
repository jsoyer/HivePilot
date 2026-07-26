"""Tests for approvals routed into a dedicated "Approvals" Telegram forum
topic (not the operator's DM), covering:

- `_approval_chat_id` resolution order (approval_chat_id -> stream group ->
  DM, the last being today's regression-tested behaviour)
- `_approval_message_thread_id` reusing `notification_service._ensure_topic_thread`
  with the dedicated "approvals" key, only when the resolved chat IS the
  forum stream group
- the approval message carries the inline keyboard + resolved
  `message_thread_id`, with callback_data unchanged
- topic-creation failure degrades to a threadless send (never lost)
- total send failure to the resolved chat falls back once to the DM
- the approve/deny callback works when pressed from the GROUP chat id, not
  just the DM/notification chat
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hivepilot.services import telegram_bot

# ---------------------------------------------------------------------------
# _approval_chat_id — resolution order
# ---------------------------------------------------------------------------


class TestApprovalChatIdResolution:
    def test_explicit_approval_chat_id_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(telegram_bot.settings, "telegram_approval_chat_id", -100999)
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_topics", True)
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_chat_id", -100111)
        monkeypatch.setattr(telegram_bot.settings, "telegram_notification_chat_id", 555)
        monkeypatch.setattr(telegram_bot.settings, "telegram_allowed_chat_ids", [])

        assert telegram_bot._approval_chat_id() == -100999

    def test_unset_falls_back_to_stream_group_when_topics_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(telegram_bot.settings, "telegram_approval_chat_id", None)
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_topics", True)
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_chat_id", -100111)
        monkeypatch.setattr(telegram_bot.settings, "telegram_notification_chat_id", 555)
        monkeypatch.setattr(telegram_bot.settings, "telegram_allowed_chat_ids", [])

        assert telegram_bot._approval_chat_id() == -100111

    def test_neither_set_falls_back_to_dm_today_behaviour(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: with neither new knob configured, resolution is
        identical to `_notification_chat_id()` — today's DM behaviour."""
        monkeypatch.setattr(telegram_bot.settings, "telegram_approval_chat_id", None)
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_topics", False)
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_chat_id", None)
        monkeypatch.setattr(telegram_bot.settings, "telegram_notification_chat_id", 555)
        monkeypatch.setattr(telegram_bot.settings, "telegram_allowed_chat_ids", [])

        assert telegram_bot._approval_chat_id() == 555 == telegram_bot._notification_chat_id()

    def test_stream_chat_id_set_but_topics_off_falls_back_to_dm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stream group configured without topics enabled must NOT be used
        for approvals — only `telegram_stream_topics=True` opts in."""
        monkeypatch.setattr(telegram_bot.settings, "telegram_approval_chat_id", None)
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_topics", False)
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_chat_id", -100111)
        monkeypatch.setattr(telegram_bot.settings, "telegram_notification_chat_id", 555)
        monkeypatch.setattr(telegram_bot.settings, "telegram_allowed_chat_ids", [])

        assert telegram_bot._approval_chat_id() == 555


# ---------------------------------------------------------------------------
# _approval_message_thread_id — dedicated "Approvals" topic reuse
# ---------------------------------------------------------------------------


class TestApprovalMessageThreadId:
    def test_resolved_group_chat_creates_approvals_topic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_topics", True)
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_chat_id", -100111)

        with patch(
            "hivepilot.services.notification_service._ensure_topic_thread",
            return_value=777,
        ) as mock_ensure:
            thread_id = telegram_bot._approval_message_thread_id(-100111)

        assert thread_id == 777
        mock_ensure.assert_called_once_with("approvals", "⛔ Approvals")

    def test_explicit_approval_chat_different_from_stream_group_no_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit `telegram_approval_chat_id` that differs from the
        stream group is not assumed to be a forum — no synthetic thread."""
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_topics", True)
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_chat_id", -100111)

        with patch("hivepilot.services.notification_service._ensure_topic_thread") as mock_ensure:
            thread_id = telegram_bot._approval_message_thread_id(-100999)

        assert thread_id is None
        mock_ensure.assert_not_called()

    def test_topics_disabled_no_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_topics", False)
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_chat_id", -100111)

        thread_id = telegram_bot._approval_message_thread_id(-100111)

        assert thread_id is None

    def test_dm_chat_id_never_gets_a_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_topics", True)
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_chat_id", -100111)

        thread_id = telegram_bot._approval_message_thread_id(555)

        assert thread_id is None

    def test_topic_creation_failure_degrades_to_no_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_ensure_topic_thread` is best-effort and returns None on any
        failure (not a forum, missing rights, 429) — never raises."""
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_topics", True)
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_chat_id", -100111)

        with patch(
            "hivepilot.services.notification_service._ensure_topic_thread",
            return_value=None,
        ):
            thread_id = telegram_bot._approval_message_thread_id(-100111)

        assert thread_id is None


# ---------------------------------------------------------------------------
# _send_approval_keyboard_message — message_thread_id plumbing
# ---------------------------------------------------------------------------


def _make_bot(send_side_effect=None):
    bot = MagicMock()
    if send_side_effect is not None:
        bot.send_message = AsyncMock(side_effect=send_side_effect)
    else:
        bot.send_message = AsyncMock()
    return bot


def _extract_callback_data(keyboard) -> list[str]:
    return [btn.callback_data for row in keyboard.inline_keyboard for btn in row]


class TestSendApprovalKeyboardMessageThreadId:
    async def test_thread_id_forwarded_and_keyboard_attached(self) -> None:
        bot = _make_bot()

        await telegram_bot._send_approval_keyboard_message(
            bot,
            chat_id=-100111,
            run_id=42,
            project="acme",
            task="deploy",
            details="ship it",
            message_thread_id=777,
        )

        bot.send_message.assert_awaited_once()
        kwargs = bot.send_message.call_args.kwargs
        assert kwargs["message_thread_id"] == 777
        assert kwargs.get("parse_mode") is None
        assert _extract_callback_data(kwargs["reply_markup"]) == [
            "approve:42",
            "deny:42",
            "challenge:42",
        ]

    async def test_no_thread_id_omits_kwarg(self) -> None:
        bot = _make_bot()

        await telegram_bot._send_approval_keyboard_message(
            bot, chat_id=555, run_id=42, project="acme", task="deploy"
        )

        kwargs = bot.send_message.call_args.kwargs
        assert "message_thread_id" not in kwargs

    async def test_fallback_retry_preserves_thread_id(self) -> None:
        """The defense-in-depth plain-fallback retry (existing 400-safety
        net) must also carry the thread id through — a topic-routed
        approval must not silently drop back to General on retry."""
        bot = _make_bot(send_side_effect=[Exception("boom"), None])

        await telegram_bot._send_approval_keyboard_message(
            bot,
            chat_id=-100111,
            run_id=5,
            project="acme",
            task="deploy",
            details="anything",
            message_thread_id=777,
        )

        assert bot.send_message.await_count == 2
        second_kwargs = bot.send_message.call_args.kwargs
        assert second_kwargs["message_thread_id"] == 777
        assert _extract_callback_data(second_kwargs["reply_markup"]) == [
            "approve:5",
            "deny:5",
            "challenge:5",
        ]


# ---------------------------------------------------------------------------
# notify_approval_required — end-to-end resolution + fallback chain
# ---------------------------------------------------------------------------


class TestNotifyApprovalRequiredRouting:
    def _patch_settings(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        approval_chat_id=None,
        stream_topics=False,
        stream_chat_id=None,
        notification_chat_id=555,
    ) -> None:
        monkeypatch.setattr(telegram_bot.settings, "telegram_bot_token", "tok")
        monkeypatch.setattr(telegram_bot.settings, "telegram_approval_chat_id", approval_chat_id)
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_topics", stream_topics)
        monkeypatch.setattr(telegram_bot.settings, "telegram_stream_chat_id", stream_chat_id)
        monkeypatch.setattr(
            telegram_bot.settings, "telegram_notification_chat_id", notification_chat_id
        )
        monkeypatch.setattr(telegram_bot.settings, "telegram_allowed_chat_ids", [])

    def test_routes_to_forum_group_with_approvals_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_settings(
            monkeypatch, stream_topics=True, stream_chat_id=-100111, notification_chat_id=555
        )
        sent: dict = {}

        async def fake_send_keyboard(
            bot, *, chat_id, run_id, project, task, details=None, message_thread_id=None
        ):
            sent["chat_id"] = chat_id
            sent["message_thread_id"] = message_thread_id

        with (
            patch.object(telegram_bot, "_send_approval_keyboard_message", fake_send_keyboard),
            patch(
                "hivepilot.services.notification_service._ensure_topic_thread",
                return_value=777,
            ),
            patch("telegram.Bot") as mock_bot_cls,
        ):
            mock_bot_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_bot_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            telegram_bot.notify_approval_required(run_id=1, project="acme", task="deploy")

        assert sent["chat_id"] == -100111
        assert sent["message_thread_id"] == 777

    def test_neither_configured_routes_to_dm_regression(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_settings(monkeypatch, notification_chat_id=555)
        sent: dict = {}

        async def fake_send_keyboard(
            bot, *, chat_id, run_id, project, task, details=None, message_thread_id=None
        ):
            sent["chat_id"] = chat_id
            sent["message_thread_id"] = message_thread_id

        with (
            patch.object(telegram_bot, "_send_approval_keyboard_message", fake_send_keyboard),
            patch("telegram.Bot") as mock_bot_cls,
        ):
            mock_bot_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_bot_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            telegram_bot.notify_approval_required(run_id=2, project="acme", task="deploy")

        assert sent["chat_id"] == 555
        assert sent["message_thread_id"] is None

    def test_group_send_failure_falls_back_to_dm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An approval must never be lost: if the send to the resolved
        (group) chat fails outright, retry once against the DM chat."""
        self._patch_settings(
            monkeypatch, stream_topics=True, stream_chat_id=-100111, notification_chat_id=555
        )
        calls: list[dict] = []

        async def fake_send_keyboard(
            bot, *, chat_id, run_id, project, task, details=None, message_thread_id=None
        ):
            calls.append({"chat_id": chat_id, "message_thread_id": message_thread_id})
            if chat_id == -100111:
                raise RuntimeError("bot not a member of that chat")

        with (
            patch.object(telegram_bot, "_send_approval_keyboard_message", fake_send_keyboard),
            patch(
                "hivepilot.services.notification_service._ensure_topic_thread",
                return_value=777,
            ),
            patch("telegram.Bot") as mock_bot_cls,
        ):
            mock_bot_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_bot_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            telegram_bot.notify_approval_required(run_id=3, project="acme", task="deploy")

        assert len(calls) == 2
        assert calls[0]["chat_id"] == -100111
        assert calls[1]["chat_id"] == 555
        # The DM fallback never carries the group's thread id.
        assert calls[1]["message_thread_id"] is None

    def test_no_configured_chat_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_settings(monkeypatch, notification_chat_id=None)

        with pytest.raises(RuntimeError):
            telegram_bot.notify_approval_required(run_id=4, project="acme", task="deploy")

    def test_stale_topic_self_heals_retries_same_chat_not_dm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dead "Approvals" topic (message thread not found) must NOT
        immediately fall back to the DM — it self-heals: invalidate the
        registry entry, recreate the topic, and retry in the SAME group
        chat with the fresh thread id."""
        self._patch_settings(
            monkeypatch, stream_topics=True, stream_chat_id=-100111, notification_chat_id=555
        )
        calls: list[dict] = []

        async def fake_send_keyboard(
            bot, *, chat_id, run_id, project, task, details=None, message_thread_id=None
        ):
            calls.append({"chat_id": chat_id, "message_thread_id": message_thread_id})
            if message_thread_id == 208:
                raise RuntimeError("Bad Request: message thread not found")

        ensure_calls: list[str] = []

        def fake_ensure(agent_key: str, title: str):
            ensure_calls.append(agent_key)
            return 208 if len(ensure_calls) == 1 else 999

        with (
            patch.object(telegram_bot, "_send_approval_keyboard_message", fake_send_keyboard),
            patch(
                "hivepilot.services.notification_service._ensure_topic_thread",
                side_effect=fake_ensure,
            ),
            patch(
                "hivepilot.services.notification_service._invalidate_topic",
                return_value=208,
            ) as mock_invalidate,
            patch("telegram.Bot") as mock_bot_cls,
        ):
            mock_bot_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_bot_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            telegram_bot.notify_approval_required(run_id=6, project="acme", task="deploy")

        mock_invalidate.assert_called_once_with("approvals")
        assert len(calls) == 2
        assert calls[0]["chat_id"] == -100111
        assert calls[0]["message_thread_id"] == 208
        assert calls[1]["chat_id"] == -100111  # SAME group, NOT the DM
        assert calls[1]["message_thread_id"] == 999  # fresh, recreated topic

    def test_closed_topic_falls_back_to_general_not_dm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A CLOSED (not deleted) "Approvals" topic must NOT be recreated —
        it routes to the group's General topic (no thread id), still the
        SAME chat, not the DM."""
        self._patch_settings(
            monkeypatch, stream_topics=True, stream_chat_id=-100111, notification_chat_id=555
        )
        calls: list[dict] = []

        async def fake_send_keyboard(
            bot, *, chat_id, run_id, project, task, details=None, message_thread_id=None
        ):
            calls.append({"chat_id": chat_id, "message_thread_id": message_thread_id})
            if message_thread_id == 208:
                raise RuntimeError("Bad Request: TOPIC_CLOSED")

        with (
            patch.object(telegram_bot, "_send_approval_keyboard_message", fake_send_keyboard),
            patch(
                "hivepilot.services.notification_service._ensure_topic_thread",
                return_value=208,
            ) as mock_ensure,
            patch("telegram.Bot") as mock_bot_cls,
        ):
            mock_bot_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_bot_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            telegram_bot.notify_approval_required(run_id=7, project="acme", task="deploy")

        # Only the initial resolve call -- no recreate attempt for a closed topic.
        mock_ensure.assert_called_once_with("approvals", "⛔ Approvals")
        assert len(calls) == 2
        assert calls[0]["chat_id"] == -100111
        assert calls[1]["chat_id"] == -100111  # SAME group, NOT the DM
        assert calls[1]["message_thread_id"] is None  # General


# ---------------------------------------------------------------------------
# _callback_approval — approve/deny button pressed from the GROUP chat
# ---------------------------------------------------------------------------


class TestCallbackApprovalFromGroupChat:
    def _make_update(self, chat_id: int, data: str) -> MagicMock:
        update = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.message.chat.id = chat_id
        update.callback_query.message.reply_text = AsyncMock()
        update.callback_query.data = data
        update.callback_query.from_user.username = "operator"
        return update

    async def test_approve_from_group_chat_resolves_correct_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A group chat id (distinct from the DM/notification chat) that IS
        in `telegram_allowed_chat_ids` must be authorized, and the
        approve/deny path must resolve the run purely from callback_data —
        never from which chat the button was pressed in."""
        group_chat_id = -100111
        monkeypatch.setattr(
            telegram_bot.settings, "telegram_allowed_chat_ids", [group_chat_id, 555]
        )
        update = self._make_update(group_chat_id, "approve:42")
        context = MagicMock()

        with patch.object(telegram_bot, "_dispatch_approval") as mock_dispatch:
            mock_dispatch.return_value = MagicMock(success=True)
            await telegram_bot._callback_approval(update, context)

        mock_dispatch.assert_called_once_with(
            42, approve=True, approver="telegram:operator", reason=None
        )
        update.callback_query.edit_message_text.assert_awaited_once()
        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "42" in text
        assert "succeeded" in text

    async def test_deny_from_group_chat_when_whitelist_open(self) -> None:
        """Empty `telegram_allowed_chat_ids` (open) authorizes any chat,
        including a forum group — no DM/private-chat assumption anywhere in
        the authorization path."""
        group_chat_id = -100222
        update = self._make_update(group_chat_id, "deny:99")
        context = MagicMock()

        with (
            patch.object(telegram_bot.settings, "telegram_allowed_chat_ids", []),
            patch.object(telegram_bot, "_dispatch_approval") as mock_dispatch,
        ):
            await telegram_bot._callback_approval(update, context)

        mock_dispatch.assert_called_once_with(
            99, approve=False, approver="telegram:operator", reason="Denied via Telegram button"
        )

    async def test_group_chat_not_in_whitelist_is_unauthorized(self) -> None:
        """A group NOT on the whitelist must still be rejected — proves the
        fix isn't "accept every chat", only that a legitimately-whitelisted
        group is no longer assumed to be a DM."""
        update = self._make_update(-100333, "approve:1")
        context = MagicMock()

        with (
            patch.object(telegram_bot.settings, "telegram_allowed_chat_ids", [555]),
            patch.object(telegram_bot, "_dispatch_approval") as mock_dispatch,
        ):
            await telegram_bot._callback_approval(update, context)

        mock_dispatch.assert_not_called()
        update.callback_query.edit_message_text.assert_awaited_once_with("Unauthorized.")
