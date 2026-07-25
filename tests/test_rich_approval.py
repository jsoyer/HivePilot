"""Tests for the rich plan-checkpoint approval message feature.

Covers:
- notification_service.send_approval_keyboard forwards `details` to notify_approval_required
- orchestrator._build_checkpoint_details produces the expected structured string
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# notification_service.send_approval_keyboard → forwards details
# ---------------------------------------------------------------------------


def test_send_approval_keyboard_forwards_details(monkeypatch) -> None:
    """send_approval_keyboard must pass `details` through to notify_approval_required.

    conftest replaces send_approval_keyboard with a no-op for all tests (to suppress
    real notifications).  This test restores the real implementation, then patches
    telegram_bot.notify_approval_required at the module level so the lazy
    ``from hivepilot.services.telegram_bot import notify_approval_required`` inside
    send_approval_keyboard resolves to our recorder.  Slack/discord are stubbed via
    sys.modules so no network or config is needed.
    """
    # conftest's autouse fixture replaces send_approval_keyboard with a no-op for all
    # tests.  We need to restore the real implementation for this specific test.
    # Strategy: exec() the source into a fresh namespace to retrieve the original
    # function without polluting sys.modules, then monkeypatch it back onto ns_mod.
    import importlib.util
    import types

    import hivepilot.services.notification_service as ns_mod
    import hivepilot.services.telegram_bot as tgb

    spec = importlib.util.find_spec("hivepilot.services.notification_service")
    assert spec is not None
    source = spec.loader.get_source("hivepilot.services.notification_service")  # type: ignore[attr-defined]
    tmp_mod = types.ModuleType("_tmp_ns")
    tmp_mod.__spec__ = spec  # type: ignore[assignment]
    exec(compile(source, spec.origin, "exec"), tmp_mod.__dict__)  # noqa: S102
    real_send = tmp_mod.send_approval_keyboard

    monkeypatch.setattr(ns_mod, "send_approval_keyboard", real_send)

    recorded: list[dict] = []

    def _fake_notify(*, run_id: int, project: str, task: str, details: str | None = None) -> None:
        recorded.append({"run_id": run_id, "project": project, "task": task, "details": details})

    slack_stub = MagicMock()
    slack_stub.notify_approval_required = lambda **kw: None
    discord_stub = MagicMock()
    discord_stub.notify_approval_required = lambda **kw: None

    with (
        patch.object(tgb, "notify_approval_required", side_effect=_fake_notify),
        patch.dict(
            "sys.modules",
            {
                "hivepilot.services.slack_bot": slack_stub,
                "hivepilot.services.discord_bot": discord_stub,
            },
        ),
    ):
        ns_mod.send_approval_keyboard(
            run_id=42, project="acme", task="plan → build", details="hello details"
        )

    assert recorded, "notify_approval_required was not called"
    assert recorded[0]["details"] == "hello details"
    assert recorded[0]["run_id"] == 42


def test_send_approval_keyboard_redacts_registered_secret_in_details(monkeypatch) -> None:
    """A resolved ${secret:NAME} value embedded in `details` (built from prior
    stage output via _build_checkpoint_details) must never reach the Telegram
    approval DM — same leak class as send_notification/stream_agent_turn.

    Same restore-the-real-implementation strategy as
    test_send_approval_keyboard_forwards_details above (conftest stubs
    send_approval_keyboard to a no-op for all other tests).
    """
    import importlib.util
    import types

    # NOTE: submodule-form import (not `from hivepilot.services import
    # config_provenance`) is deliberate. The sibling test above wraps its call
    # in `patch.dict("sys.modules", {...})`, which restores the ENTIRE
    # sys.modules dict to its pre-`with` snapshot on exit — silently evicting
    # any module that got lazily imported for the FIRST time inside that
    # block (here: config_provenance, imported lazily inside
    # send_approval_keyboard) while leaving the stale `hivepilot.services`
    # package attribute pointing at the evicted module object. The
    # `from package import submodule` form would then resolve to that stale,
    # emptied module instead of triggering a fresh, sys.modules-consistent
    # import — silently registering the marker into a module distinct from
    # the one `send_approval_keyboard`'s own lazy import resolves to. The
    # `import package.submodule` form always re-syncs sys.modules first.
    import hivepilot.services.config_provenance as config_provenance
    import hivepilot.services.notification_service as ns_mod
    import hivepilot.services.telegram_bot as tgb

    config_provenance.clear_secret_values()
    marker = "APPROVAL-MARKER-do-not-leak"
    config_provenance.register_secret_value(marker)

    spec = importlib.util.find_spec("hivepilot.services.notification_service")
    assert spec is not None
    source = spec.loader.get_source("hivepilot.services.notification_service")  # type: ignore[attr-defined]
    tmp_mod = types.ModuleType("_tmp_ns2")
    tmp_mod.__spec__ = spec  # type: ignore[assignment]
    exec(compile(source, spec.origin, "exec"), tmp_mod.__dict__)  # noqa: S102
    real_send = tmp_mod.send_approval_keyboard

    monkeypatch.setattr(ns_mod, "send_approval_keyboard", real_send)

    recorded: list[dict] = []

    def _fake_notify(*, run_id: int, project: str, task: str, details: str | None = None) -> None:
        recorded.append({"details": details})

    slack_stub = MagicMock()
    slack_stub.notify_approval_required = lambda **kw: None
    discord_stub = MagicMock()
    discord_stub.notify_approval_required = lambda **kw: None

    try:
        with (
            patch.object(tgb, "notify_approval_required", side_effect=_fake_notify),
            patch.dict(
                "sys.modules",
                {
                    "hivepilot.services.slack_bot": slack_stub,
                    "hivepilot.services.discord_bot": discord_stub,
                },
            ),
        ):
            ns_mod.send_approval_keyboard(
                run_id=42,
                project="acme",
                task="plan → build",
                details=f"plan echoed {marker}",
            )
        assert recorded, "notify_approval_required was not called"
        assert marker not in (recorded[0]["details"] or "")
        assert config_provenance.REDACTED in (recorded[0]["details"] or "")
    finally:
        config_provenance.clear_secret_values()


# ---------------------------------------------------------------------------
# _build_checkpoint_details — pure function unit tests
# ---------------------------------------------------------------------------


def test_build_checkpoint_details_with_structured_report() -> None:
    """Helper includes components, completed, next stage, and parsed summary bullets."""
    from hivepilot.orchestrator import _build_checkpoint_details

    # Simulate a Jules synthesis chunk with a structured summary
    jules_chunk = (
        "## status\nok\n"
        "## summary\n"
        "- Ship the auth service first\n"
        "- Update the DB schema with migration\n"
        "- Run smoke tests on staging\n"
    )

    result = _build_checkpoint_details(
        prior_chunks=["earlier chunk", jules_chunk],
        completed=["plan", "research"],
        next_stage="build",
        components=["acme-auth", "acme-api"],
        group_mode=True,
    )

    assert "acme-auth" in result
    assert "acme-api" in result
    assert "plan" in result and "research" in result
    assert "build" in result
    assert "Ship the auth service first" in result
    assert "Update the DB schema" in result
    assert "Run smoke tests" in result
    assert "Obsidian vault" in result


def test_build_checkpoint_details_fallback_when_no_bullets() -> None:
    """Falls back to a plain text excerpt when parse_agent_report finds no bullets."""
    from hivepilot.orchestrator import _build_checkpoint_details

    unstructured_chunk = "The agent concluded that everything looks great. Deploy when ready."

    result = _build_checkpoint_details(
        prior_chunks=[unstructured_chunk],
        completed=["plan"],
        next_stage="deploy",
        components=[],
        group_mode=False,
    )

    assert "deploy" in result
    assert "plan" in result
    # Fallback excerpt should appear
    assert "concluded" in result or "Plan excerpt" in result
    assert "Obsidian vault" in result


def test_build_checkpoint_details_no_components_in_non_group_mode() -> None:
    """Components line must NOT appear when group_mode=False."""
    from hivepilot.orchestrator import _build_checkpoint_details

    result = _build_checkpoint_details(
        prior_chunks=[],
        completed=["plan"],
        next_stage="build",
        components=["some-component"],
        group_mode=False,
    )

    assert "some-component" not in result
    assert "build" in result


def test_build_checkpoint_details_no_prior_chunks() -> None:
    """Works gracefully with empty prior_chunks."""
    from hivepilot.orchestrator import _build_checkpoint_details

    result = _build_checkpoint_details(
        prior_chunks=[],
        completed=[],
        next_stage="build",
        components=[],
        group_mode=False,
    )

    assert "build" in result
    assert "Obsidian vault" in result


def test_build_checkpoint_details_limits_bullets() -> None:
    """No more than 6 bullets appear regardless of how many the report contains."""
    from hivepilot.orchestrator import _build_checkpoint_details

    many_bullets = "\n".join(f"- Step {i}" for i in range(20))
    chunk = f"## summary\n{many_bullets}"

    result = _build_checkpoint_details(
        prior_chunks=[chunk],
        completed=["plan"],
        next_stage="build",
        components=[],
        group_mode=False,
    )

    bullet_count = result.count("• Step")
    assert bullet_count <= 6


# ---------------------------------------------------------------------------
# telegram_bot._truncate_md
# ---------------------------------------------------------------------------


def test_truncate_md_short_text() -> None:
    from hivepilot.services.telegram_bot import _truncate_md

    text = "hello world"
    assert _truncate_md(text, max_len=100) == text


def test_truncate_md_long_text() -> None:
    from hivepilot.services.telegram_bot import _truncate_md

    text = "line one\nline two\nline three\n" * 200
    result = _truncate_md(text, max_len=50)
    assert len(result) <= 52  # small tolerance for the trailing ellipsis
    assert result.endswith("…")


# ---------------------------------------------------------------------------
# telegram_bot._send_approval_keyboard_message — parse-safe against arbitrary
# plan/details text (regression for the Telegram 400
# "Can't parse entities: can't find end of the entity ..." bug)
# ---------------------------------------------------------------------------


def _make_bot(send_side_effect=None):
    """A MagicMock bot with an AsyncMock send_message."""
    bot = MagicMock()
    if send_side_effect is not None:
        bot.send_message = AsyncMock(side_effect=send_side_effect)
    else:
        bot.send_message = AsyncMock()
    return bot


def _extract_callback_data(keyboard) -> list[str]:
    return [btn.callback_data for row in keyboard.inline_keyboard for btn in row]


async def test_send_approval_keyboard_message_unbalanced_markdown_sends_and_keeps_buttons() -> None:
    """Dynamic `details` text with unbalanced markdown entities (lone `_`, `*`,
    unclosed backtick, unclosed `[link`) must not raise and must still deliver
    the Approve/Deny/Challenge keyboard — this is the exact shape of content
    that used to trigger Telegram's 400 "Can't parse entities" and silently
    drop the checkpoint's inline keyboard.
    """
    from hivepilot.services import telegram_bot

    bot = _make_bot()
    malicious_details = "plan with a lone _ and * and unclosed `code and a [link without close"

    await telegram_bot._send_approval_keyboard_message(
        bot, chat_id=123, run_id=42, project="acme", task="deploy", details=malicious_details
    )

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.call_args.kwargs
    # No parse_mode at all — plain text — is what makes this content safe:
    # Telegram never attempts to parse it as markup, so it can never 400.
    assert kwargs.get("parse_mode") is None
    assert malicious_details in kwargs["text"]
    keyboard = kwargs["reply_markup"]
    assert _extract_callback_data(keyboard) == ["approve:42", "deny:42", "challenge:42"]


async def test_send_approval_keyboard_message_over_length_truncates_and_sends() -> None:
    """A very long dynamic details string gets truncated (not dropped) and the
    message still sends with the keyboard attached."""
    from hivepilot.services import telegram_bot

    bot = _make_bot()
    long_details = "x" * 10_000

    await telegram_bot._send_approval_keyboard_message(
        bot, chat_id=123, run_id=7, project="acme", task="deploy", details=long_details
    )

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.call_args.kwargs
    assert len(kwargs["text"]) < len(long_details)
    assert kwargs.get("reply_markup") is not None
    assert _extract_callback_data(kwargs["reply_markup"]) == ["approve:7", "deny:7", "challenge:7"]


async def test_send_approval_keyboard_message_normal_plan_unchanged_callbacks() -> None:
    """Regression: a normal plan sends as before and callback_data tokens the
    callback handler expects (`approve:<id>` / `deny:<id>` / `challenge:<id>`)
    are unchanged."""
    from hivepilot.services import telegram_bot

    bot = _make_bot()
    await telegram_bot._send_approval_keyboard_message(
        bot,
        chat_id=123,
        run_id=99,
        project="acme",
        task="plan → build",
        details="Ship the auth service first. Update the DB schema.",
    )

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.call_args.kwargs
    assert "acme" in kwargs["text"]
    assert "plan → build" in kwargs["text"]
    assert "Ship the auth service first" in kwargs["text"]
    assert _extract_callback_data(kwargs["reply_markup"]) == [
        "approve:99",
        "deny:99",
        "challenge:99",
    ]


async def test_send_approval_keyboard_message_falls_back_to_plain_on_send_failure() -> None:
    """If the first send still fails for any reason (defense-in-depth), retry
    once with a minimal fallback body — the keyboard must survive even that."""
    from hivepilot.services import telegram_bot

    bot = _make_bot(send_side_effect=[Exception("boom"), None])

    await telegram_bot._send_approval_keyboard_message(
        bot, chat_id=123, run_id=5, project="acme", task="deploy", details="anything"
    )

    assert bot.send_message.await_count == 2
    second_kwargs = bot.send_message.call_args.kwargs
    assert second_kwargs.get("reply_markup") is not None
    assert _extract_callback_data(second_kwargs["reply_markup"]) == [
        "approve:5",
        "deny:5",
        "challenge:5",
    ]
