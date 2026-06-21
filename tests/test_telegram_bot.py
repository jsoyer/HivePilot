"""Tests for _cmd_interactions handler in telegram_bot.py.

Drives the async handler with asyncio.run() — no pytest-asyncio needed since the
telegram library is NOT installed in the test environment.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

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
    update, ctx = _make_update(), _make_context(["noxys", "company", "simulate"])
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
    update, ctx = _make_update(), _make_context(["noxys", "adopt", "X"])
    orch = MagicMock()
    orch.run_debate.return_value = {"path": "ADR.md", "dry_run": True}
    with (
        patch.object(telegram_bot, "_require_allowed", return_value=True),
        patch.object(telegram_bot, "_get_orch", return_value=orch),
    ):
        asyncio.run(telegram_bot._cmd_debate(update, ctx))
    assert orch.run_debate.call_args.kwargs["topic"] == "adopt X"
    assert "ADR.md" in update.message.reply_text.call_args.args[0]


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
