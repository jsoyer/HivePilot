"""Regression tests for delivering a run's result back to Telegram.

Both tests here reproduce a real incident: run 267 (`noxys-ciso`) succeeded and
produced a 13,961-character threat assessment, Telegram rejected the reply with
``BadRequest("Message is too long")``, and the global error handler logged it as
a transient network hiccup that "will retry". Nothing retried. The operator saw
no output and no error — the report only survived as an on-disk artifact.

Drives the async handlers with asyncio.run() — no pytest-asyncio needed, and the
telegram library is NOT installed in the test environment, so `telegram.error`
is stubbed with PTB's real exception hierarchy (`BadRequest` subclasses
`NetworkError` — that inheritance is the whole point of the second test).
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import hivepilot.services.telegram_bot as telegram_bot

# Telegram's hard cap; the splitter targets 3900 to leave entity/marker headroom.
TELEGRAM_HARD_CAP = 4096


def _result(detail: str, *, success: bool = True):
    return SimpleNamespace(success=success, project="noxys", target="noxys", detail=detail)


# ---------------------------------------------------------------------------
# _reply_results — never lose a long report to the message-length cap
# ---------------------------------------------------------------------------


def test_long_report_is_split_across_several_messages_instead_of_being_dropped():
    """A CISO-sized report must arrive, in order, across multiple messages."""
    # Same order of magnitude as the report lost in run 267.
    detail = "\n\n".join(f"Paragraph {i}. " + ("lorem ipsum " * 40) for i in range(30))
    assert len(detail) > 13_000, "fixture must exceed Telegram's cap to be meaningful"

    message = MagicMock()
    message.reply_text = AsyncMock()

    asyncio.run(telegram_bot._reply_results(message, [_result(detail)]))

    sent = [c.args[0] for c in message.reply_text.await_args_list]
    assert len(sent) > 1, "a 13k-char report must be split, not sent as one message"
    for chunk in sent:
        assert len(chunk) <= TELEGRAM_HARD_CAP, f"chunk of {len(chunk)} chars exceeds the cap"

    # The substance must survive the split — check landmarks from both ends.
    joined = "".join(sent)
    assert "Paragraph 0." in joined
    assert "Paragraph 29." in joined


def test_short_result_still_arrives_as_a_single_message():
    """The common case must not regress into pointless message spam."""
    message = MagicMock()
    message.reply_text = AsyncMock()

    asyncio.run(telegram_bot._reply_results(message, [_result("clearance granted")]))

    assert message.reply_text.await_count == 1
    assert "clearance granted" in message.reply_text.await_args.args[0]


def test_agent_output_with_angle_brackets_is_not_mangled_as_markup():
    """`List<int>` in agent output must survive — the splitter is not HTML-aware here."""
    message = MagicMock()
    message.reply_text = AsyncMock()

    asyncio.run(telegram_bot._reply_results(message, [_result("returns List<int> from 2 > 1")]))

    joined = "".join(c.args[0] for c in message.reply_text.await_args_list)
    assert "List<int>" in joined
    assert "2 > 1" in joined


# ---------------------------------------------------------------------------
# _on_error — a rejected payload is not a network hiccup
# ---------------------------------------------------------------------------


@pytest.fixture
def ptb_errors(monkeypatch):
    """Stub `telegram.error` mirroring PTB's real hierarchy.

    In python-telegram-bot: ``BadRequest -> NetworkError -> TelegramError``.
    A handler that tests NetworkError first therefore swallows BadRequest.
    """

    class TelegramError(Exception): ...

    class NetworkError(TelegramError): ...

    class BadRequest(NetworkError): ...

    class TimedOut(NetworkError): ...

    class Conflict(TelegramError): ...

    module = types.ModuleType("telegram.error")
    module.TelegramError = TelegramError
    module.NetworkError = NetworkError
    module.BadRequest = BadRequest
    module.TimedOut = TimedOut
    module.Conflict = Conflict

    parent = sys.modules.get("telegram") or types.ModuleType("telegram")
    parent.error = module
    monkeypatch.setitem(sys.modules, "telegram", parent)
    monkeypatch.setitem(sys.modules, "telegram.error", module)
    return module


def test_message_too_long_is_reported_as_an_error_not_a_retryable_hiccup(ptb_errors):
    """The exact failure from run 267 must surface loudly, with no retry promise."""
    context = MagicMock()
    context.error = ptb_errors.BadRequest("Message is too long")

    with patch.object(telegram_bot, "logger") as logger:
        asyncio.run(telegram_bot._on_error(MagicMock(), context))

    assert not logger.warning.called, "a rejected payload must not be logged as a warning"
    logger.error.assert_called_once()
    assert logger.error.call_args.args[0] == "telegram.bad_request"
    # The old line claimed a retry that does not exist — never say that again.
    assert "will retry" not in logger.error.call_args.kwargs.get("detail", "")


def test_a_genuine_network_error_is_still_treated_as_retryable(ptb_errors):
    """Guard the fix from over-reaching: real transport failures do retry."""
    context = MagicMock()
    context.error = ptb_errors.NetworkError("connection reset")

    with patch.object(telegram_bot, "logger") as logger:
        asyncio.run(telegram_bot._on_error(MagicMock(), context))

    assert not logger.error.called
    logger.warning.assert_called_once()
    assert logger.warning.call_args.args[0] == "telegram.network_error"


def test_polling_conflict_classification_is_unchanged(ptb_errors):
    """Conflict is matched before everything else and must stay that way."""
    context = MagicMock()
    context.error = ptb_errors.Conflict("terminated by other getUpdates request")

    with patch.object(telegram_bot, "logger") as logger:
        asyncio.run(telegram_bot._on_error(MagicMock(), context))

    logger.warning.assert_called_once()
    assert logger.warning.call_args.args[0] == "telegram.polling_conflict"
