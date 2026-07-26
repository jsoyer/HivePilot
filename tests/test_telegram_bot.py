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
        stored_token, stored_decision = telegram_bot._pending_concierge[777]
        assert stored_decision == decision
        assert stored_token  # non-empty
        context.bot.send_message.assert_awaited_once()
        call_kwargs = context.bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 777
        assert "reply_markup" in call_kwargs
        # The button's callback_data must carry the SAME token that was stored
        # — otherwise a correct "yes" reply could never validate.
        keyboard = call_kwargs["reply_markup"]
        yes_button = keyboard.inline_keyboard[0][0]
        assert yes_button.callback_data == f"concierge:yes:{stored_token}"

    def test_confirmation_sent_as_plain_text_not_markdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A model-controlled `order` containing backticks/underscores/
        asterisks must not be interpreted as Markdown — sent with no
        parse_mode (plain text)."""
        monkeypatch.setattr(telegram_bot.settings, "chatops_concierge_enabled", True)
        update = _make_mention_update(chat_id=778, text="ask gustave to do `rm -rf *_data`")
        context = _make_mention_context()
        telegram_bot._pending_challenges.clear()
        telegram_bot._pending_concierge.clear()
        malicious_order = "fix `unclosed backtick and *unclosed bold and _unclosed italic"
        decision = ConciergeDecision(
            kind="route",
            role_key="developer",
            target="acme",
            order=malicious_order,
            destructive=True,
        )

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch("hivepilot.services.concierge_service.route", return_value=decision),
        ):
            asyncio.run(telegram_bot._cmd_mention(update, context))

        context.bot.send_message.assert_awaited_once()
        call_kwargs = context.bot.send_message.call_args.kwargs
        assert call_kwargs.get("parse_mode") is None
        assert malicious_order in call_kwargs["text"]

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
        telegram_bot._pending_concierge[888] = (
            "tok123",
            ConciergeDecision(kind="action", action="run", destructive=True),
        )
        update = self._make_callback_update(888, "concierge:no:tok123")
        context = self._make_callback_context()

        with patch.object(telegram_bot, "_require_allowed", return_value=True):
            asyncio.run(telegram_bot._concierge_callback(update, context))

        assert 888 not in telegram_bot._pending_concierge
        update.callback_query.edit_message_text.assert_awaited()

    def test_yes_with_correct_token_executes_route_decision(self) -> None:
        decision = ConciergeDecision(
            kind="route", role_key="developer", target="acme", order="do it", destructive=True
        )
        telegram_bot._pending_concierge[999] = ("realtoken", decision)
        update = self._make_callback_update(999, "concierge:yes:realtoken")
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

    def test_stale_token_does_not_execute_and_leaves_pending_untouched(self) -> None:
        """A keyboard whose token no longer matches the currently-stored
        token (e.g. because a newer destructive message overwrote it) must
        NOT execute anything, must show an expired message, and must leave
        the CURRENT pending decision in place (a wrong token must never
        clear a still-valid pending confirmation)."""
        current_decision = ConciergeDecision(
            kind="route", role_key="developer", target="acme", order="current", destructive=True
        )
        telegram_bot._pending_concierge[333] = ("currenttoken", current_decision)
        update = self._make_callback_update(333, "concierge:yes:staletoken")
        context = self._make_callback_context()

        orch = MagicMock()
        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch.object(telegram_bot, "_get_orch", return_value=orch),
        ):
            asyncio.run(telegram_bot._concierge_callback(update, context))

        orch.run_task.assert_not_called()
        orch.run_pipeline.assert_not_called()
        # Current pending decision is untouched by the stale-token attempt.
        assert telegram_bot._pending_concierge[333] == ("currenttoken", current_decision)
        update.callback_query.edit_message_text.assert_awaited_once()
        text = update.callback_query.edit_message_text.call_args.args[0]
        assert "expired" in text.lower()

    def test_overwrite_scenario_stale_button_never_executes_new_decision(self) -> None:
        """The exact review scenario: decision A is pending with token A;
        before the user presses A's Yes button, decision B overwrites the
        pending entry (different token, different content). Pressing A's
        stale button must execute NOTHING — never A, and never B."""
        decision_a = ConciergeDecision(
            kind="route", role_key="developer", target="acme", order="A's order", destructive=True
        )
        decision_b = ConciergeDecision(
            kind="action",
            action="run_pipeline",
            target="acme-api",
            params={"pipeline": "company"},
            destructive=True,
        )
        telegram_bot._pending_concierge[444] = ("token_a", decision_a)
        # A newer destructive message overwrites the pending entry before
        # the user acts on A's keyboard.
        telegram_bot._pending_concierge[444] = ("token_b", decision_b)

        update = self._make_callback_update(444, "concierge:yes:token_a")  # A's stale button
        context = self._make_callback_context()

        orch = MagicMock()
        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch.object(telegram_bot, "_get_orch", return_value=orch),
        ):
            asyncio.run(telegram_bot._concierge_callback(update, context))

        orch.run_task.assert_not_called()
        orch.run_pipeline.assert_not_called()
        orch.run_approved.assert_not_called()
        # B is still pending, untouched, and can still be confirmed correctly later.
        assert telegram_bot._pending_concierge[444] == ("token_b", decision_b)

    def test_unauthorized_chat_never_executes(self) -> None:
        decision = ConciergeDecision(kind="action", action="run", destructive=True)
        telegram_bot._pending_concierge[222] = ("tok123", decision)
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
# Multi-agent dispatch (kind="multi_route") — one confirmation gates ALL
# orders in the batch; declining runs none; each dispatch reuses the same
# _run_agent_order path as a single `route` decision.
# ---------------------------------------------------------------------------

from hivepilot.services.concierge_service import DispatchOrder  # noqa: E402


class TestConciergeMultiDispatch:
    def teardown_method(self, method) -> None:
        telegram_bot._pending_concierge.clear()

    def _multi_decision(self) -> ConciergeDecision:
        return ConciergeDecision(
            kind="multi_route",
            dispatches=[
                DispatchOrder(role_key="cto", target="acme", order="sketch the architecture"),
                DispatchOrder(role_key="ciso", target="acme", order="define the security review"),
                DispatchOrder(role_key="developer", target="acme", order="prep the rollout"),
            ],
            destructive=True,
        )

    def test_multi_dispatch_sends_one_confirmation_listing_all_orders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(telegram_bot.settings, "chatops_concierge_enabled", True)
        update = _make_mention_update(chat_id=1001, text="donne leur les ordres")
        context = _make_mention_context()
        telegram_bot._pending_challenges.clear()
        telegram_bot._pending_concierge.clear()
        decision = self._multi_decision()

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch("hivepilot.services.concierge_service.route", return_value=decision),
        ):
            asyncio.run(telegram_bot._cmd_mention(update, context))

        assert 1001 in telegram_bot._pending_concierge
        stored_token, stored_decision = telegram_bot._pending_concierge[1001]
        assert stored_decision == decision
        context.bot.send_message.assert_awaited_once()
        call_kwargs = context.bot.send_message.call_args.kwargs
        assert call_kwargs.get("parse_mode") is None
        text = call_kwargs["text"]
        # All three orders must be listed in the single confirmation prompt.
        assert "cto" in text
        assert "ciso" in text
        assert "developer" in text
        keyboard = call_kwargs["reply_markup"]
        yes_button = keyboard.inline_keyboard[0][0]
        assert yes_button.callback_data == f"concierge:yes:{stored_token}"

    def test_confirm_yes_dispatches_all_three_orders(self) -> None:
        decision = self._multi_decision()
        telegram_bot._pending_concierge[2002] = ("tok-multi", decision)
        update = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.message.chat.id = 2002
        update.callback_query.message.reply_text = AsyncMock()
        update.callback_query.message.delete = AsyncMock()
        update.callback_query.data = "concierge:yes:tok-multi"
        context = MagicMock()

        orch = MagicMock()
        orch.run_task.return_value = []
        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch.object(telegram_bot, "_get_orch", return_value=orch),
        ):
            asyncio.run(telegram_bot._concierge_callback(update, context))

        assert 2002 not in telegram_bot._pending_concierge
        assert orch.run_task.call_count == 3
        dispatched_roles = {c.kwargs.get("task_name") for c in orch.run_task.call_args_list}
        # task_name comes from each role's registered command_task — just
        # assert three distinct calls happened (one per dispatch), not a
        # single collapsed call.
        assert len(dispatched_roles) >= 1
        assert orch.run_task.call_count == len(decision.dispatches or [])

    def test_confirm_no_dispatches_nothing(self) -> None:
        decision = self._multi_decision()
        telegram_bot._pending_concierge[3003] = ("tok-multi-2", decision)
        update = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.message.chat.id = 3003
        update.callback_query.data = "concierge:no:tok-multi-2"
        context = MagicMock()

        orch = MagicMock()
        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch.object(telegram_bot, "_get_orch", return_value=orch),
        ):
            asyncio.run(telegram_bot._concierge_callback(update, context))

        assert 3003 not in telegram_bot._pending_concierge
        orch.run_task.assert_not_called()

    def test_multi_dispatch_skips_unconfigured_role_but_runs_the_rest(self) -> None:
        """Defense in depth: even though `_clamp` already validates roles,
        `_execute_concierge_decision` independently re-checks `_AGENT_REGISTRY`
        (mirrors the single-`route` behaviour) — an entry that somehow isn't
        addressable is skipped, not fatal to the rest of the batch."""
        decision = ConciergeDecision(
            kind="multi_route",
            dispatches=[
                DispatchOrder(role_key="cto", target="acme", order="sketch the architecture"),
                DispatchOrder(role_key="not-a-real-role", target="acme", order="ghost order"),
            ],
            destructive=True,
        )
        telegram_bot._pending_concierge[4004] = ("tok-multi-3", decision)
        update = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.message.chat.id = 4004
        update.callback_query.message.reply_text = AsyncMock()
        update.callback_query.message.delete = AsyncMock()
        update.callback_query.data = "concierge:yes:tok-multi-3"
        context = MagicMock()

        orch = MagicMock()
        orch.run_task.return_value = []
        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch.object(telegram_bot, "_get_orch", return_value=orch),
        ):
            asyncio.run(telegram_bot._concierge_callback(update, context))

        assert orch.run_task.call_count == 1

    def test_empty_dispatches_replies_nothing_to_do(self) -> None:
        decision = ConciergeDecision(kind="multi_route", dispatches=[], destructive=True)
        telegram_bot._pending_concierge[5005] = ("tok-multi-4", decision)
        update = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.message.chat.id = 5005
        update.callback_query.message.reply_text = AsyncMock()
        update.callback_query.message.delete = AsyncMock()
        update.callback_query.data = "concierge:yes:tok-multi-4"
        context = MagicMock()

        orch = MagicMock()
        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch.object(telegram_bot, "_get_orch", return_value=orch),
        ):
            asyncio.run(telegram_bot._concierge_callback(update, context))

        orch.run_task.assert_not_called()
        update.callback_query.message.reply_text.assert_awaited_once_with("Nothing to do.")


class TestConciergeChatIdThreading:
    """`_handle_concierge_mention` must thread the chat id into
    `concierge_service.route` so conversation memory is chat-scoped."""

    def test_route_called_with_chat_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(telegram_bot.settings, "chatops_concierge_enabled", True)
        update = _make_mention_update(chat_id=6006, text="hello")
        context = _make_mention_context()
        telegram_bot._pending_challenges.clear()
        decision = ConciergeDecision(kind="answer", answer_text="hi")

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch(
                "hivepilot.services.concierge_service.route", return_value=decision
            ) as mock_route,
        ):
            asyncio.run(telegram_bot._cmd_mention(update, context))

        mock_route.assert_called_once()
        assert mock_route.call_args.kwargs.get("chat_id") == 6006


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


# ---------------------------------------------------------------------------
# `_dispatch_approval` -- now delegates entirely to the shared
# `Orchestrator.approve_run` helper (also used by `api_service.handle_approval`
# for Mirador's "Approve" button), instead of re-implementing the
# pipeline-checkpoint-vs-task discriminator itself. Regression: the Telegram
# path's behavior must stay byte-identical (unit-tested directly on the
# orchestrator in tests/test_pipeline_checkpoint.py::TestApproveRunRouting).
# ---------------------------------------------------------------------------


class TestDispatchApprovalDelegatesToSharedHelper:
    def test_delegates_to_orchestrator_approve_run(self) -> None:
        orch = MagicMock()
        with patch.object(telegram_bot, "_get_orch", return_value=orch):
            telegram_bot._dispatch_approval(42, approve=True, approver="telegram", reason=None)

        orch.approve_run.assert_called_once_with(
            run_id=42, approve=True, approver="telegram", reason=None
        )
        orch.run_approved.assert_not_called()
        orch.resume_pipeline.assert_not_called()

    def test_deny_delegates_to_orchestrator_approve_run(self) -> None:
        orch = MagicMock()
        with patch.object(telegram_bot, "_get_orch", return_value=orch):
            telegram_bot._dispatch_approval(7, approve=False, approver="telegram", reason="no good")

        orch.approve_run.assert_called_once_with(
            run_id=7, approve=False, approver="telegram", reason="no good"
        )
        orch.run_approved.assert_not_called()
        orch.resume_pipeline.assert_not_called()

    def test_no_direct_run_approved_call_in_telegram_bot_source(self) -> None:
        """Static guard: the routing decision must live in ONE place
        (`Orchestrator.approve_run`) -- `telegram_bot.py` must never call
        `run_approved`/`resume_pipeline` directly again for the
        approve/deny routing decision."""
        from pathlib import Path

        source = Path(telegram_bot.__file__).read_text()
        assert ".run_approved(" not in source
        assert ".resume_pipeline(" not in source
        assert "_get_orch().approve_run(" in source


# ---------------------------------------------------------------------------
# Explicit-failure-logs sprint, Part A.2 (centralized): the route/run_id/
# project-or-pipeline/approver dispatch log, and the specific failure reason
# on rejection, now live in `Orchestrator.approve_run` itself (see
# `hivepilot/orchestrator.py`) rather than in `_dispatch_approval` -- so
# Telegram gets them "for free" simply by delegating to `approve_run`. These
# tests prove exactly that: driving the REAL `approve_run` (bound to a fake
# `resume_pipeline`/`run_approved`) through `telegram_bot._dispatch_approval`
# and asserting the centralized `approval.dispatch`/`approval.dispatch_failed`
# log lines fire, not a telegram-local reimplementation of them.
# ---------------------------------------------------------------------------


class _FakeApprovalOrchestrator:
    """Real `Orchestrator.approve_run` bound to fake `resume_pipeline`/
    `run_approved` -- exercises the ACTUAL routing+logging method through
    `_dispatch_approval`, not a re-implementation of it."""

    def __init__(self) -> None:
        self.resume_pipeline_calls: list[dict] = []
        self.run_approved_calls: list[dict] = []

    def resume_pipeline(self, **kwargs):
        self.resume_pipeline_calls.append(kwargs)
        return "ok"

    def run_approved(self, **kwargs):
        self.run_approved_calls.append(kwargs)
        return "ok"


from hivepilot.orchestrator import Orchestrator as _Orchestrator  # noqa: E402

_FakeApprovalOrchestrator.approve_run = _Orchestrator.approve_run  # type: ignore[attr-defined]


class TestDispatchApprovalLogging:
    """`_dispatch_approval` logs the route/run_id/project-or-pipeline/approver
    BEFORE dispatching, and the failure reason (not a bare exception) when
    the orchestrator rejects it -- via the centralized `approve_run` logs."""

    def test_logs_task_route_before_dispatch(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging as stdlib_logging

        fake_orch = _FakeApprovalOrchestrator()
        approval = {
            "status": "pending",
            "project": "proj-a",
            "task": "task-a",
            "metadata": "{}",
        }
        with (
            patch("hivepilot.orchestrator.state_service.get_approval", return_value=approval),
            patch.object(telegram_bot, "_get_orch", return_value=fake_orch),
            caplog.at_level(stdlib_logging.INFO),
        ):
            result = telegram_bot._dispatch_approval(42, True, "alice")

        assert result == "ok"
        assert fake_orch.run_approved_calls == [
            {"run_id": 42, "approve": True, "approver": "alice", "reason": None}
        ]
        assert fake_orch.resume_pipeline_calls == []
        dispatch_records = [
            r for r in caplog.records if r.levelname == "INFO" and "approval.dispatch" in r.message
        ]
        assert dispatch_records
        assert '"route": "task"' in dispatch_records[0].message
        assert '"run_id": 42' in dispatch_records[0].message

    def test_logs_pipeline_checkpoint_route_before_dispatch(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import json
        import logging as stdlib_logging

        fake_orch = _FakeApprovalOrchestrator()
        meta = json.dumps({"kind": "pipeline_checkpoint", "pipeline": "review"})
        approval = {"status": "pending", "project": "proj-a", "task": None, "metadata": meta}
        with (
            patch("hivepilot.orchestrator.state_service.get_approval", return_value=approval),
            patch.object(telegram_bot, "_get_orch", return_value=fake_orch),
            caplog.at_level(stdlib_logging.INFO),
        ):
            result = telegram_bot._dispatch_approval(43, True, "bob")

        assert result == "ok"
        assert fake_orch.resume_pipeline_calls == [
            {"run_id": 43, "approve": True, "approver": "bob"}
        ]
        assert fake_orch.run_approved_calls == []
        dispatch_records = [
            r for r in caplog.records if r.levelname == "INFO" and "approval.dispatch" in r.message
        ]
        assert dispatch_records
        assert '"route": "pipeline_checkpoint"' in dispatch_records[0].message
        assert '"pipeline": "review"' in dispatch_records[0].message

    def test_logs_specific_reason_on_dispatch_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging as stdlib_logging

        with (
            patch("hivepilot.orchestrator.state_service.get_approval", return_value=None),
            patch.object(telegram_bot, "_get_orch", return_value=_FakeApprovalOrchestrator()),
            caplog.at_level(stdlib_logging.INFO),
            pytest.raises(ValueError, match="not pending approval"),
        ):
            telegram_bot._dispatch_approval(99, True, "alice")

        rejected_records = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and "approval.dispatch_rejected" in r.message
        ]
        assert rejected_records
        assert '"run_id": 99' in rejected_records[0].message
        assert '"reason": "unknown_run"' in rejected_records[0].message


# ---------------------------------------------------------------------------
# Bug 1 (live): the 🗣 Challenge / Ask button pressed in a Telegram FORUM
# group topic appeared to "do nothing" -- the follow-up prompt didn't
# reliably carry the topic's `message_thread_id` (landing in General
# instead), `_pending_challenges` was keyed by bare chat_id (two topics
# could clobber each other), and the whole path logged nothing.
# ---------------------------------------------------------------------------


class TestChallengeButtonForumTopic:
    def _make_challenge_callback_update(
        self, *, chat_id: int, thread_id: int | None, run_id: int = 42
    ) -> MagicMock:
        update = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.data = f"challenge:{run_id}"
        update.callback_query.from_user.username = "alice"
        update.callback_query.message.chat.id = chat_id
        update.callback_query.message.message_thread_id = thread_id
        update.callback_query.message.reply_text = AsyncMock()
        return update

    def teardown_method(self, method) -> None:
        telegram_bot._pending_challenges.clear()

    def test_challenge_reply_carries_the_topic_thread_id(self) -> None:
        update = self._make_challenge_callback_update(chat_id=100, thread_id=456)
        context = MagicMock()

        with patch.object(telegram_bot, "_require_allowed", return_value=True):
            asyncio.run(telegram_bot._callback_approval(update, context))

        update.callback_query.message.reply_text.assert_awaited_once()
        call_kwargs = update.callback_query.message.reply_text.call_args.kwargs
        assert call_kwargs.get("message_thread_id") == 456

    def test_no_thread_id_omits_the_kwarg_for_non_forum_chats(self) -> None:
        update = self._make_challenge_callback_update(chat_id=101, thread_id=None)
        context = MagicMock()

        with patch.object(telegram_bot, "_require_allowed", return_value=True):
            asyncio.run(telegram_bot._callback_approval(update, context))

        call_kwargs = update.callback_query.message.reply_text.call_args.kwargs
        assert "message_thread_id" not in call_kwargs

    def test_pending_challenge_keyed_by_chat_and_thread(self) -> None:
        update = self._make_challenge_callback_update(chat_id=200, thread_id=10, run_id=7)
        context = MagicMock()

        with patch.object(telegram_bot, "_require_allowed", return_value=True):
            asyncio.run(telegram_bot._callback_approval(update, context))

        assert (200, 10) in telegram_bot._pending_challenges
        assert 200 not in telegram_bot._pending_challenges

    def test_followup_in_a_different_topic_does_not_consume_the_challenge(self) -> None:
        callback_update = self._make_challenge_callback_update(chat_id=300, thread_id=1, run_id=9)
        callback_context = MagicMock()
        with patch.object(telegram_bot, "_require_allowed", return_value=True):
            asyncio.run(telegram_bot._callback_approval(callback_update, callback_context))
        assert (300, 1) in telegram_bot._pending_challenges

        # Operator replies in a DIFFERENT topic of the same group.
        mention_update = _make_mention_update(chat_id=300, text="not my answer")
        mention_update.message.message_thread_id = 2
        mention_context = _make_mention_context()

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch.object(telegram_bot, "_get_orch") as mock_get_orch,
        ):
            asyncio.run(telegram_bot._cmd_mention(mention_update, mention_context))

        mock_get_orch.assert_not_called()
        # Still pending, untouched, for the CORRECT topic.
        assert (300, 1) in telegram_bot._pending_challenges

    def test_followup_in_the_same_topic_consumes_the_challenge(self) -> None:
        callback_update = self._make_challenge_callback_update(chat_id=400, thread_id=5, run_id=11)
        callback_context = MagicMock()
        with patch.object(telegram_bot, "_require_allowed", return_value=True):
            asyncio.run(telegram_bot._callback_approval(callback_update, callback_context))

        mention_update = _make_mention_update(chat_id=400, text="here is my challenge")
        mention_update.message.message_thread_id = 5
        mention_context = _make_mention_context()

        orch = MagicMock()
        orch.human_challenge.return_value = "CoS response"
        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch.object(telegram_bot, "_get_orch", return_value=orch),
            patch("hivepilot.services.state_service.get_approval", return_value=None),
        ):
            asyncio.run(telegram_bot._cmd_mention(mention_update, mention_context))

        orch.human_challenge.assert_called_once_with(11, "here is my challenge", "telegram:alice")
        assert (400, 5) not in telegram_bot._pending_challenges

    def test_backward_compat_bare_chat_id_entry_is_still_consumed(self) -> None:
        """An entry stored under the bare chat_id (pre-fix state) must still
        be consumed even when the follow-up message happens to carry a
        thread_id — defensive fallback, never a hard requirement."""
        telegram_bot._pending_challenges[500] = (13, "telegram:bob")

        mention_update = _make_mention_update(chat_id=500, text="my answer")
        mention_update.message.message_thread_id = 999
        mention_context = _make_mention_context()

        orch = MagicMock()
        orch.human_challenge.return_value = "CoS response"
        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch.object(telegram_bot, "_get_orch", return_value=orch),
            patch("hivepilot.services.state_service.get_approval", return_value=None),
        ):
            asyncio.run(telegram_bot._cmd_mention(mention_update, mention_context))

        orch.human_challenge.assert_called_once_with(13, "my answer", "telegram:bob")

    def test_challenge_path_emits_structured_log_events_on_request(self) -> None:
        update = self._make_challenge_callback_update(chat_id=600, thread_id=1, run_id=21)
        context = MagicMock()

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch.object(telegram_bot, "logger") as mock_logger,
        ):
            asyncio.run(telegram_bot._callback_approval(update, context))

        events = [call.args[0] for call in mock_logger.info.call_args_list]
        assert "telegram.challenge.requested" in events
        assert "telegram.challenge.prompt_sent" in events

    def test_challenge_receive_and_dispatch_events_logged(self) -> None:
        telegram_bot._pending_challenges[(700, 3)] = (30, "telegram:carol")
        mention_update = _make_mention_update(chat_id=700, text="my question")
        mention_update.message.message_thread_id = 3
        mention_context = _make_mention_context()

        orch = MagicMock()
        orch.human_challenge.return_value = "response"
        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch.object(telegram_bot, "_get_orch", return_value=orch),
            patch("hivepilot.services.state_service.get_approval", return_value=None),
            patch.object(telegram_bot, "logger") as mock_logger,
        ):
            asyncio.run(telegram_bot._cmd_mention(mention_update, mention_context))

        events = [call.args[0] for call in mock_logger.info.call_args_list]
        assert "telegram.challenge.received" in events
        assert "telegram.challenge.dispatched" in events

    def test_challenge_dispatch_failure_logs_failed_event(self) -> None:
        telegram_bot._pending_challenges[(750, 4)] = (31, "telegram:dave")
        mention_update = _make_mention_update(chat_id=750, text="my question")
        mention_update.message.message_thread_id = 4
        mention_context = _make_mention_context()

        orch = MagicMock()
        orch.human_challenge.side_effect = RuntimeError("orchestrator boom")
        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch.object(telegram_bot, "_get_orch", return_value=orch),
            patch.object(telegram_bot, "logger") as mock_logger,
        ):
            asyncio.run(telegram_bot._cmd_mention(mention_update, mention_context))

        error_events = [call.args[0] for call in mock_logger.error.call_args_list]
        assert "telegram.challenge.failed" in error_events

    def test_prompt_send_failure_logs_prompt_failed_and_does_not_raise(self) -> None:
        update = self._make_challenge_callback_update(chat_id=800, thread_id=2, run_id=40)
        update.callback_query.message.reply_text = AsyncMock(side_effect=RuntimeError("boom"))
        context = MagicMock()

        with (
            patch.object(telegram_bot, "_require_allowed", return_value=True),
            patch.object(telegram_bot, "logger") as mock_logger,
        ):
            asyncio.run(telegram_bot._callback_approval(update, context))  # must not raise

        error_events = [call.args[0] for call in mock_logger.error.call_args_list]
        assert "telegram.challenge.prompt_failed" in error_events


class TestCallbackApprovalNoMessage:
    """Telegram can omit `message` on a callback query -- live log showed a
    bare `AttributeError: 'NoneType' object has no attribute 'chat'` here."""

    def test_no_message_does_not_raise_and_shows_a_toast(self) -> None:
        update = MagicMock()
        update.callback_query.message = None
        update.callback_query.data = "approve:1"
        update.callback_query.answer = AsyncMock()
        context = MagicMock()

        asyncio.run(telegram_bot._callback_approval(update, context))  # must not raise

        update.callback_query.answer.assert_awaited_once()
        call_args = update.callback_query.answer.call_args
        assert call_args.args and "no longer be used" in call_args.args[0].lower()
        assert call_args.kwargs.get("show_alert") is True
