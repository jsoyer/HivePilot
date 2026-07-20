"""Tests for _cmd_interactions handler in telegram_bot.py.

Drives the async handler with asyncio.run() — no pytest-asyncio needed since the
telegram library is NOT installed in the test environment.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import hivepilot.services.telegram_bot as telegram_bot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_update(chat_id: int = 123) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    return update


def _make_context(args: list[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


SAMPLE_ROW = {
    "id": 1,
    "run_id": 42,
    "actor": "planner",
    "action": "propose",
    "target": "executor",
    "summary": "Proposed refactor of auth module",
    "metadata": None,
    "timestamp": "2026-06-19T10:00:00",
}

SAMPLE_ROW_NO_TARGET = {
    "id": 2,
    "run_id": None,
    "actor": "observer",
    "action": "note",
    "target": None,
    "summary": "Noted an anomaly",
    "metadata": None,
    "timestamp": "2026-06-19T10:01:00",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCmdInteractionsEmpty:
    """When the store returns [] the handler replies with the empty message."""

    def test_no_interactions_reply(self):
        update = _make_update()
        context = _make_context()

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch(
                "hivepilot.services.state_service.list_recent_interactions",
                return_value=[],
            ),
        ):
            asyncio.run(telegram_bot._cmd_interactions(update, context))

        update.message.reply_text.assert_awaited_once()
        call_text = update.message.reply_text.call_args[0][0]
        assert call_text == "No interactions logged yet."


class TestCmdInteractionsFormatting:
    """Reply text includes actor, action, target and summary from the row."""

    def test_row_formatted_correctly(self):
        update = _make_update()
        context = _make_context()

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch(
                "hivepilot.services.state_service.list_recent_interactions",
                return_value=[SAMPLE_ROW],
            ),
        ):
            asyncio.run(telegram_bot._cmd_interactions(update, context))

        call_text = update.message.reply_text.call_args[0][0]
        assert "planner" in call_text
        assert "propose" in call_text
        assert "executor" in call_text
        assert "Proposed refactor of auth module" in call_text

    def test_none_run_id_formatted_as_dash(self):
        """When run_id is None, the line shows '-' instead of a number."""
        update = _make_update()
        context = _make_context()

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch(
                "hivepilot.services.state_service.list_recent_interactions",
                return_value=[SAMPLE_ROW_NO_TARGET],
            ),
        ):
            asyncio.run(telegram_bot._cmd_interactions(update, context))

        call_text = update.message.reply_text.call_args[0][0]
        assert "[#-]" in call_text

    def test_none_target_formatted_as_all(self):
        """When target is None, the line shows 'all'."""
        update = _make_update()
        context = _make_context()

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch(
                "hivepilot.services.state_service.list_recent_interactions",
                return_value=[SAMPLE_ROW_NO_TARGET],
            ),
        ):
            asyncio.run(telegram_bot._cmd_interactions(update, context))

        call_text = update.message.reply_text.call_args[0][0]
        assert "all" in call_text


class TestCmdInteractionsLimitArg:
    """Numeric first arg is forwarded as limit to the store."""

    def test_numeric_arg_sets_limit(self):
        update = _make_update()
        context = _make_context(args=["3"])

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch(
                "hivepilot.services.state_service.list_recent_interactions",
                return_value=[SAMPLE_ROW],
            ) as mock_list,
        ):
            asyncio.run(telegram_bot._cmd_interactions(update, context))

        mock_list.assert_called_once_with(limit=3, run_id=None)

    def test_no_args_uses_default_limit_10(self):
        update = _make_update()
        context = _make_context(args=[])

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch(
                "hivepilot.services.state_service.list_recent_interactions",
                return_value=[],
            ) as mock_list,
        ):
            asyncio.run(telegram_bot._cmd_interactions(update, context))

        mock_list.assert_called_once_with(limit=10, run_id=None)

    def test_non_numeric_arg_uses_default_limit(self):
        """Non-digit first arg is ignored and default limit is used."""
        update = _make_update()
        context = _make_context(args=["abc"])

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch(
                "hivepilot.services.state_service.list_recent_interactions",
                return_value=[],
            ) as mock_list,
        ):
            asyncio.run(telegram_bot._cmd_interactions(update, context))

        mock_list.assert_called_once_with(limit=10, run_id=None)


class TestCmdInteractionsErrorPath:
    """When the store raises, the reply starts with 'Error:'."""

    def test_store_exception_returns_error_message(self):
        update = _make_update()
        context = _make_context()

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch(
                "hivepilot.services.state_service.list_recent_interactions",
                side_effect=RuntimeError("DB is locked"),
            ),
        ):
            asyncio.run(telegram_bot._cmd_interactions(update, context))

        call_text = update.message.reply_text.call_args[0][0]
        assert call_text.startswith("Error:")


class TestCmdInteractionsUnauthorized:
    """When _require_allowed returns False, the handler returns without replying."""

    def test_unauthorized_no_reply(self):
        update = _make_update(chat_id=999)
        context = _make_context()

        with patch.object(telegram_bot, "_require_allowed", return_value=False):
            asyncio.run(telegram_bot._cmd_interactions(update, context))

        update.message.reply_text.assert_not_awaited()


class TestBuildApplicationRegistration:
    """_build_application source must reference the interactions handler."""

    def test_handler_function_exists(self):
        assert hasattr(telegram_bot, "_cmd_interactions"), (
            "_cmd_interactions not defined in telegram_bot module"
        )
        assert asyncio.iscoroutinefunction(telegram_bot._cmd_interactions)

    def test_handler_registered_in_build_application(self):
        src = inspect.getsource(telegram_bot._build_application)
        assert "interactions" in src, (
            "'interactions' not found in _build_application source — "
            "CommandHandler('interactions', ...) was not registered"
        )


class TestHelpUpdated:
    """_cmd_help source must mention /interactions."""

    def test_help_contains_interactions(self):
        src = inspect.getsource(telegram_bot._cmd_help)
        assert "interactions" in src, (
            "/interactions line not found in _cmd_help — help text not updated"
        )


# ---------------------------------------------------------------------------
# Remote command + control commands (run-pipeline / debate / steps / discovery)
# ---------------------------------------------------------------------------

import types  # noqa: E402


def _orch_mock(**attrs) -> MagicMock:
    orch = MagicMock()
    for k, v in attrs.items():
        setattr(orch, k, v)
    return orch


def test_cmd_pipelines_lists_pipelines() -> None:
    update, ctx = _make_update(), _make_context()
    orch = MagicMock()
    orch.pipelines.pipelines = {"company": types.SimpleNamespace(description="Full company")}
    with (
        patch.object(telegram_bot, "_require_allowed", return_value=True),
        patch.object(telegram_bot, "_get_orch", return_value=orch),
    ):
        asyncio.run(telegram_bot._cmd_pipelines(update, ctx))
    out = update.message.reply_text.call_args.args[0]
    assert "company" in out


def test_cmd_run_pipeline_usage_error() -> None:
    update, ctx = _make_update(), _make_context(["onlyproject"])
    with patch.object(telegram_bot, "_require_allowed", return_value=True):
        asyncio.run(telegram_bot._cmd_run_pipeline(update, ctx))
    assert "Usage:" in update.message.reply_text.call_args.args[0]


def test_cmd_run_pipeline_passes_simulate() -> None:
    update, ctx = _make_update(), _make_context(["acme", "company", "simulate"])
    orch = MagicMock()
    orch.run_pipeline.return_value = []
    with (
        patch.object(telegram_bot, "_require_allowed", return_value=True),
        patch.object(telegram_bot, "_get_orch", return_value=orch),
    ):
        asyncio.run(telegram_bot._cmd_run_pipeline(update, ctx))
    assert orch.run_pipeline.call_args.kwargs["simulate"] is True
    assert orch.run_pipeline.call_args.kwargs["pipeline_name"] == "company"


def test_cmd_debate_calls_run_debate() -> None:
    update, ctx = _make_update(), _make_context(["acme", "adopt", "X"])
    orch = MagicMock()
    orch.run_debate.return_value = {"path": "ADR.md", "dry_run": True}
    with (
        patch.object(telegram_bot, "_require_allowed", return_value=True),
        patch.object(telegram_bot, "_get_orch", return_value=orch),
    ):
        asyncio.run(telegram_bot._cmd_debate(update, ctx))
    assert orch.run_debate.call_args.kwargs["topic"] == "adopt X"
    assert "ADR.md" in update.message.reply_text.call_args.args[0]


def test_cmd_debate_degrades_when_ceo_role_absent() -> None:
    update, ctx = _make_update(), _make_context(["acme", "adopt", "X"])
    orch = MagicMock()
    with (
        patch.object(telegram_bot, "_require_allowed", return_value=True),
        patch.object(telegram_bot, "_get_orch", return_value=orch),
        patch("hivepilot.roles.ROLES", {}),
    ):
        asyncio.run(telegram_bot._cmd_debate(update, ctx))
    orch.run_debate.assert_not_called()
    out = update.message.reply_text.call_args.args[0]
    assert "not configured" in out
    assert "examples/roles.yaml" in out


def test_cmd_steps_queries_state() -> None:
    update, ctx = _make_update(), _make_context(["7"])
    rows = [{"status": "success", "step": "ceo intake", "timestamp": "t", "detail": "ok"}]
    with (
        patch.object(telegram_bot, "_require_allowed", return_value=True),
        patch("hivepilot.services.state_service.get_steps_for_run", return_value=rows),
    ):
        asyncio.run(telegram_bot._cmd_steps(update, ctx))
    out = update.message.reply_text.call_args.args[0]
    assert "ceo intake" in out and "success" in out


def test_cmd_steps_usage_error() -> None:
    update, ctx = _make_update(), _make_context([])
    with patch.object(telegram_bot, "_require_allowed", return_value=True):
        asyncio.run(telegram_bot._cmd_steps(update, ctx))
    assert "Usage:" in update.message.reply_text.call_args.args[0]


def test_new_commands_registered_in_source() -> None:
    src = inspect.getsource(telegram_bot._build_application)
    for cmd in ("runpipeline", "debate", "steps", "pipelines", "projects", "tasks"):
        assert cmd in src, f"{cmd} not registered"


# ---------------------------------------------------------------------------
# Python 3.14 "no current event loop" regression (run_polling / run_webhook)
# ---------------------------------------------------------------------------


def _get_current_loop_or_none():
    """Best-effort snapshot of the current event loop for save/restore in
    tests — tolerates the Python 3.14 'no current event loop' RuntimeError
    (which can already be the ambient state before a test even runs)."""
    try:
        return asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        return None


class TestEnsureEventLoop:
    """_ensure_event_loop() must give the main thread a usable loop on 3.14
    (where asyncio.get_event_loop() raises instead of auto-creating one),
    without disturbing an already-running loop."""

    def test_sets_new_loop_when_none_current(self) -> None:
        old_loop = _get_current_loop_or_none()
        try:
            # Simulate the Python 3.14 "no current event loop" state.
            asyncio.set_event_loop(None)

            telegram_bot._ensure_event_loop()

            # A loop must now be retrievable without raising.
            loop = asyncio.get_event_loop()
            assert loop is not None
            assert not loop.is_running()
        finally:
            asyncio.set_event_loop(old_loop)

    def test_noop_when_loop_already_running(self) -> None:
        observed: dict[str, Any] = {}

        async def _inner():
            running_before = asyncio.get_running_loop()
            telegram_bot._ensure_event_loop()
            running_after = asyncio.get_running_loop()
            observed["before"] = running_before
            observed["after"] = running_after

        asyncio.run(_inner())
        # The running loop must be untouched — same object before and after.
        assert observed["before"] is observed["after"]

    def test_noop_when_loop_already_set_but_not_running(self) -> None:
        old_loop = _get_current_loop_or_none()
        existing = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(existing)
            telegram_bot._ensure_event_loop()
            assert asyncio.get_event_loop() is existing
        finally:
            asyncio.set_event_loop(old_loop)
            existing.close()


class TestRunPollingNoCurrentLoop:
    """Regression: run_polling() must not raise 'no current event loop' on
    the Python 3.14-style no-loop main thread, and must delegate to PTB."""

    def test_run_polling_survives_no_current_loop_and_calls_ptb(self) -> None:
        fake_app = MagicMock()
        old_loop = _get_current_loop_or_none()
        try:
            asyncio.set_event_loop(None)  # simulate 3.14 no-loop main thread
            with (
                patch.object(telegram_bot, "_token", return_value="123:ABC"),
                patch.object(telegram_bot, "_build_application", return_value=fake_app),
            ):
                telegram_bot.run_polling()
        finally:
            asyncio.set_event_loop(old_loop)

        fake_app.run_polling.assert_called_once_with(drop_pending_updates=True)

    def test_run_polling_calls_ensure_event_loop_before_ptb(self) -> None:
        fake_app = MagicMock()
        call_order: list[str] = []
        fake_app.run_polling.side_effect = lambda **_: call_order.append("run_polling")
        with (
            patch.object(telegram_bot, "_token", return_value="123:ABC"),
            patch.object(telegram_bot, "_build_application", return_value=fake_app),
            patch.object(
                telegram_bot,
                "_ensure_event_loop",
                side_effect=lambda: call_order.append("ensure_event_loop"),
            ),
        ):
            telegram_bot.run_polling()

        assert call_order == ["ensure_event_loop", "run_polling"]


class TestRunWebhookNoCurrentLoop:
    """Same 3.14 loop-guarantee, for the built-in-server webhook path."""

    def test_run_webhook_survives_no_current_loop_and_calls_ptb(self) -> None:
        fake_app = MagicMock()
        old_loop = _get_current_loop_or_none()
        try:
            asyncio.set_event_loop(None)  # simulate 3.14 no-loop main thread
            with (
                patch.object(telegram_bot, "_token", return_value="123456:ABC"),
                patch.object(telegram_bot, "_build_application", return_value=fake_app),
            ):
                telegram_bot.run_webhook("https://example.com")
        finally:
            asyncio.set_event_loop(old_loop)

        fake_app.run_webhook.assert_called_once()

    def test_run_webhook_calls_ensure_event_loop_before_ptb(self) -> None:
        fake_app = MagicMock()
        call_order: list[str] = []
        fake_app.run_webhook.side_effect = lambda **_: call_order.append("run_webhook")
        with (
            patch.object(telegram_bot, "_token", return_value="123456:ABC"),
            patch.object(telegram_bot, "_build_application", return_value=fake_app),
            patch.object(
                telegram_bot,
                "_ensure_event_loop",
                side_effect=lambda: call_order.append("ensure_event_loop"),
            ),
        ):
            telegram_bot.run_webhook("https://example.com")

        assert call_order == ["ensure_event_loop", "run_webhook"]


class TestProcessUpdateUnaffectedByLoopFix:
    """The FastAPI-integrated process_update path runs inside uvicorn's already
    -running loop; it must not call _ensure_event_loop (get_running_loop()
    early-return already covers it — nothing to wire in here)."""

    def test_process_update_source_does_not_reference_ensure_event_loop(self) -> None:
        src = inspect.getsource(telegram_bot.process_update)
        assert "_ensure_event_loop" not in src


def test_fetch_recent_chats_dedupes(monkeypatch) -> None:
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "result": [
                    {"message": {"chat": {"id": 42, "first_name": "Jo", "type": "private"}}},
                    {"message": {"chat": {"id": 42, "first_name": "Jo"}}},
                    {"message": {"chat": {"id": -100, "title": "Team", "type": "group"}}},
                ]
            }

    monkeypatch.setattr(telegram_bot.settings, "telegram_bot_token", "T")
    monkeypatch.setattr("requests.get", lambda *a, **k: FakeResp())
    chats = telegram_bot.fetch_recent_chats()
    assert {c["id"] for c in chats} == {42, -100}
    assert any(c["name"] == "Team" for c in chats)


# ---------------------------------------------------------------------------
# Natural-language concierge (opt-in) — plain-text @mention hook
# ---------------------------------------------------------------------------

from hivepilot.services.concierge_service import ConciergeDecision  # noqa: E402


def _make_mention_update(chat_id: int = 555, text: str = "hello there") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_mention_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


class TestConciergeOffByteIdentical:
    """`chatops_concierge_enabled=False` (default) — a plain-text message
    still hits the old silent `return`; concierge_service.route is never
    called and no reply is sent."""

    def test_concierge_not_called_and_silent_when_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(telegram_bot.settings, "chatops_concierge_enabled", False)
        update = _make_mention_update(text="hello there")
        context = _make_mention_context()
        telegram_bot._pending_challenges.clear()

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch("hivepilot.services.concierge_service.route") as mock_route,
        ):
            asyncio.run(telegram_bot._cmd_mention(update, context))

        mock_route.assert_not_called()
        update.message.reply_text.assert_not_awaited()


class TestConciergeOnAnswer:
    def test_answer_decision_replies_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(telegram_bot.settings, "chatops_concierge_enabled", True)
        update = _make_mention_update(text="what's running?")
        context = _make_mention_context()
        telegram_bot._pending_challenges.clear()
        decision = ConciergeDecision(kind="answer", answer_text="Nothing is running right now.")

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch(
                "hivepilot.services.concierge_service.route", return_value=decision
            ) as mock_route,
        ):
            asyncio.run(telegram_bot._cmd_mention(update, context))

        mock_route.assert_called_once()
        update.message.reply_text.assert_awaited_once_with("Nothing is running right now.")


class TestConciergeOnDestructive:
    def test_destructive_route_sends_keyboard_and_stores_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(telegram_bot.settings, "chatops_concierge_enabled", True)
        update = _make_mention_update(chat_id=777, text="ask gustave to fix the bug")
        context = _make_mention_context()
        telegram_bot._pending_challenges.clear()
        telegram_bot._pending_concierge.clear()
        decision = ConciergeDecision(
            kind="route",
            role_key="developer",
            target="acme",
            order="fix the bug",
            destructive=True,
        )

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch("hivepilot.services.concierge_service.route", return_value=decision),
        ):
            asyncio.run(telegram_bot._cmd_mention(update, context))

        assert 777 in telegram_bot._pending_concierge
        assert telegram_bot._pending_concierge[777] == decision
        context.bot.send_message.assert_awaited_once()
        call_kwargs = context.bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 777
        assert "reply_markup" in call_kwargs

    def teardown_method(self, method) -> None:
        telegram_bot._pending_concierge.clear()


class TestConciergeCallback:
    """`concierge:yes:<token>` / `concierge:no:<token>` inline-keyboard callback."""

    def _make_callback_update(self, chat_id: int, data: str) -> MagicMock:
        update = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.message.chat.id = chat_id
        update.callback_query.message.reply_text = AsyncMock()
        update.callback_query.message.delete = AsyncMock()
        update.callback_query.data = data
        return update

    def _make_callback_context(self) -> MagicMock:
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        return ctx

    def test_no_cancels_and_drops_pending(self) -> None:
        telegram_bot._pending_concierge[888] = ConciergeDecision(
            kind="action", action="run", destructive=True
        )
        update = self._make_callback_update(888, "concierge:no:tok123")
        context = self._make_callback_context()

        with patch.object(telegram_bot, "_require_allowed", return_value=True):
            asyncio.run(telegram_bot._concierge_callback(update, context))

        assert 888 not in telegram_bot._pending_concierge
        update.callback_query.edit_message_text.assert_awaited()

    def test_yes_executes_route_decision(self) -> None:
        decision = ConciergeDecision(
            kind="route", role_key="developer", target="acme", order="do it", destructive=True
        )
        telegram_bot._pending_concierge[999] = decision
        update = self._make_callback_update(999, "concierge:yes:tok123")
        context = self._make_callback_context()

        orch = MagicMock()
        orch.run_task.return_value = []
        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch.object(telegram_bot, "_get_orch", return_value=orch),
        ):
            asyncio.run(telegram_bot._concierge_callback(update, context))

        assert 999 not in telegram_bot._pending_concierge
        orch.run_task.assert_called_once()
        assert orch.run_task.call_args.kwargs["project_names"] == ["acme"]

    def test_yes_with_no_pending_reports_expired(self) -> None:
        telegram_bot._pending_concierge.pop(111, None)
        update = self._make_callback_update(111, "concierge:yes:tok123")
        context = self._make_callback_context()

        with patch.object(telegram_bot, "_require_allowed", return_value=True):
            asyncio.run(telegram_bot._concierge_callback(update, context))

        update.callback_query.edit_message_text.assert_awaited()

    def test_unauthorized_chat_never_executes(self) -> None:
        decision = ConciergeDecision(kind="action", action="run", destructive=True)
        telegram_bot._pending_concierge[222] = decision
        update = self._make_callback_update(222, "concierge:yes:tok123")
        context = self._make_callback_context()

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=False),
            patch.object(telegram_bot, "_get_orch") as mock_get_orch,
        ):
            asyncio.run(telegram_bot._concierge_callback(update, context))

        mock_get_orch.assert_not_called()
        # Pending entry is untouched by an unauthorized attempt.
        assert 222 in telegram_bot._pending_concierge

    def teardown_method(self, method) -> None:
        telegram_bot._pending_concierge.clear()


class TestConciergeHandlerRegistered:
    def test_callback_handler_registered_in_build_application(self) -> None:
        src = inspect.getsource(telegram_bot._build_application)
        assert "_concierge_callback" in src
        assert "concierge" in src


# ---------------------------------------------------------------------------
# Graceful PTB error handler (Conflict / network errors logged concisely,
# unexpected errors keep their traceback)
# ---------------------------------------------------------------------------


class TestOnError:
    def test_conflict_logs_warning_no_raise(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging as stdlib_logging

        from telegram.error import Conflict

        context = MagicMock()
        context.error = Conflict("terminated by other getUpdates request")

        with caplog.at_level(stdlib_logging.WARNING):
            asyncio.run(telegram_bot._on_error(None, context))

        assert any(rec.levelname == "WARNING" for rec in caplog.records)

    def test_network_error_logs_warning_no_raise(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging as stdlib_logging

        from telegram.error import NetworkError

        context = MagicMock()
        context.error = NetworkError("connection reset")

        with caplog.at_level(stdlib_logging.WARNING):
            asyncio.run(telegram_bot._on_error(None, context))

        assert any(rec.levelname == "WARNING" for rec in caplog.records)

    def test_unexpected_error_logs_error_with_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging as stdlib_logging

        context = MagicMock()
        context.error = RuntimeError("something genuinely unexpected")

        with caplog.at_level(stdlib_logging.WARNING):
            asyncio.run(telegram_bot._on_error(None, context))

        assert any(rec.levelname == "ERROR" for rec in caplog.records)

    def test_registered_in_build_application(self) -> None:
        src = inspect.getsource(telegram_bot._build_application)
        assert "add_error_handler" in src
        assert "_on_error" in src
