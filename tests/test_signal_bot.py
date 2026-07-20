"""Tests for hivepilot/services/signal_bot.py — dual-mode (cli/rest) Signal bot.

Signal has no cloud bot API and no inbound webhook (E2E P2P) — the transport is
pull-only. `cli` mode drives the external `signal-cli` binary via `subprocess`;
`rest` mode polls a `signal-cli-rest-api` HTTP wrapper via `requests`. Neither
requires an SDK, so — unlike test_slack_bot.py/test_discord_bot.py (which fake
`sys.modules["slack_bolt"]`/`sys.modules["discord"]`) — these tests mock
`subprocess.run`/`shutil.which` (cli) and `requests.get`/`requests.post` (rest)
directly.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import hivepilot.services.signal_bot as signal_bot

ALLOWED_NUMBER = "+15550001111"
DENIED_NUMBER = "+15559998888"
BOT_NUMBER = "+15551234567"


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(signal_bot.settings, "signal_number", BOT_NUMBER)
    monkeypatch.setattr(signal_bot.settings, "signal_allowed_numbers", [ALLOWED_NUMBER])
    monkeypatch.setattr(signal_bot.settings, "signal_cli_path", "signal-cli")
    monkeypatch.setattr(signal_bot.settings, "signal_rest_url", "http://localhost:8080")
    monkeypatch.setattr(signal_bot.settings, "signal_notification_number", ALLOWED_NUMBER)
    monkeypatch.setattr(signal_bot.settings, "signal_receive_mode", "cli")


# ---------------------------------------------------------------------------
# _is_allowed — E.164 whitelist
# ---------------------------------------------------------------------------


class TestIsAllowed:
    def test_allowed_number(self) -> None:
        assert signal_bot._is_allowed(ALLOWED_NUMBER) is True

    def test_denied_number(self) -> None:
        assert signal_bot._is_allowed(DENIED_NUMBER) is False

    def test_open_when_list_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.settings, "signal_allowed_numbers", [])
        assert signal_bot._is_allowed(DENIED_NUMBER) is True


# ---------------------------------------------------------------------------
# parse_envelope
# ---------------------------------------------------------------------------


class TestParseEnvelope:
    def test_parses_wrapped_envelope(self) -> None:
        raw = {
            "envelope": {
                "sourceNumber": ALLOWED_NUMBER,
                "dataMessage": {"message": "/status"},
            }
        }
        assert signal_bot.parse_envelope(raw) == (ALLOWED_NUMBER, "/status")

    def test_falls_back_to_source_field(self) -> None:
        raw = {"envelope": {"source": ALLOWED_NUMBER, "dataMessage": {"message": "hi"}}}
        assert signal_bot.parse_envelope(raw) == (ALLOWED_NUMBER, "hi")

    def test_receipt_without_data_message_returns_none(self) -> None:
        raw = {"envelope": {"sourceNumber": ALLOWED_NUMBER, "receiptMessage": {}}}
        assert signal_bot.parse_envelope(raw) is None

    def test_missing_sender_returns_none(self) -> None:
        raw = {"envelope": {"dataMessage": {"message": "hi"}}}
        assert signal_bot.parse_envelope(raw) is None

    def test_non_dict_returns_none(self) -> None:
        assert signal_bot.parse_envelope("not a dict") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _dispatch_text — help handled locally, everything else reused via chatops
# ---------------------------------------------------------------------------


class TestDispatchText:
    def test_help_handled_locally_without_chatops(self) -> None:
        with patch("hivepilot.services.chatops_service.handle_signal") as handle:
            result = signal_bot._dispatch_text("/help")
        handle.assert_not_called()
        assert "run" in result.lower()

    def test_delegates_to_chatops_service(self) -> None:
        with patch("hivepilot.services.chatops_service.handle_signal", return_value="ok") as handle:
            result = signal_bot._dispatch_text("/status")
        handle.assert_called_once_with({"text": "/status"})
        assert result == "ok"

    def test_bare_approve_form_reaches_orchestrator(self) -> None:
        """No leading slash — Signal's natural reply style must still route."""
        orch = MagicMock()
        orch.run_approved.return_value = MagicMock(success=True)
        with (
            patch("hivepilot.services.chatops_service._verify", lambda required: None),
            patch("hivepilot.services.chatops_service._get_orchestrator", return_value=orch),
        ):
            result = signal_bot._dispatch_text("approve 42")
        orch.run_approved.assert_called_once_with(
            run_id=42, approve=True, approver="signal", reason=None
        )
        assert "42" in result

    def test_chatops_error_caught_and_returned_as_text(self) -> None:
        """A RuntimeError from chatops_service (e.g. missing chatops token) must
        never crash the receive loop — it becomes a reply message instead."""
        with patch(
            "hivepilot.services.chatops_service.handle_signal",
            side_effect=RuntimeError("boom"),
        ):
            result = signal_bot._dispatch_text("/run acme deploy")
        assert "boom" in result

    def test_empty_text(self) -> None:
        assert signal_bot._dispatch_text("   ") == "Unknown command"


# ---------------------------------------------------------------------------
# CLI transport — receive
# ---------------------------------------------------------------------------


class TestReceiveCli:
    def test_missing_binary_returns_empty_no_subprocess_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(signal_bot.shutil, "which", lambda _: None)
        with patch.object(signal_bot.subprocess, "run") as run:
            result = signal_bot._receive_cli()
        assert result == []
        run.assert_not_called()

    def test_parses_json_lines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.shutil, "which", lambda _: "/usr/bin/signal-cli")
        stdout = (
            '{"envelope": {"sourceNumber": "+1555", "dataMessage": {"message": "/status"}}}\n'
            '{"envelope": {"sourceNumber": "+1555", "receiptMessage": {}}}\n'
        )
        fake_proc = MagicMock(stdout=stdout, returncode=0)
        with patch.object(signal_bot.subprocess, "run", return_value=fake_proc) as run:
            result = signal_bot._receive_cli()
        assert len(result) == 2
        argv = run.call_args.args[0]
        assert argv[0] == "/usr/bin/signal-cli"
        assert "-a" in argv and BOT_NUMBER in argv
        assert "receive" in argv

    def test_timeout_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.shutil, "which", lambda _: "/usr/bin/signal-cli")
        with patch.object(
            signal_bot.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="signal-cli", timeout=30),
        ):
            assert signal_bot._receive_cli() == []

    def test_malformed_json_line_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.shutil, "which", lambda _: "/usr/bin/signal-cli")
        fake_proc = MagicMock(stdout="not json\n", returncode=0)
        with patch.object(signal_bot.subprocess, "run", return_value=fake_proc):
            assert signal_bot._receive_cli() == []


# ---------------------------------------------------------------------------
# CLI transport — send
# ---------------------------------------------------------------------------


class TestSendCli:
    def test_missing_binary_raises_no_subprocess_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(signal_bot.shutil, "which", lambda _: None)
        with patch.object(signal_bot.subprocess, "run") as run:
            with pytest.raises(RuntimeError, match="signal-cli not found"):
                signal_bot._send_cli(ALLOWED_NUMBER, "hello")
        run.assert_not_called()

    def test_sends_via_subprocess_argv_no_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.shutil, "which", lambda _: "/usr/bin/signal-cli")
        with patch.object(signal_bot.subprocess, "run") as run:
            signal_bot._send_cli(ALLOWED_NUMBER, "hello world")
        argv = run.call_args.args[0]
        assert argv == [
            "/usr/bin/signal-cli",
            "-a",
            BOT_NUMBER,
            "send",
            "-m",
            "hello world",
            ALLOWED_NUMBER,
        ]
        # No credential/token-shaped argument anywhere in argv — signal-cli's
        # registration state is managed out-of-band, never passed as a CLI arg.
        assert not any("token" in str(a).lower() or "password" in str(a).lower() for a in argv)


# ---------------------------------------------------------------------------
# REST transport
# ---------------------------------------------------------------------------


class TestReceiveRest:
    def test_parses_envelope_list(self) -> None:
        fake_resp = MagicMock()
        fake_resp.json.return_value = [
            {"envelope": {"sourceNumber": "+1555", "dataMessage": {"message": "/status"}}}
        ]
        fake_resp.raise_for_status.return_value = None
        with patch("requests.get", return_value=fake_resp) as get:
            result = signal_bot._receive_rest()
        assert len(result) == 1
        assert "receive" in get.call_args.args[0]
        assert BOT_NUMBER in get.call_args.args[0]

    def test_unreachable_returns_empty(self) -> None:
        with patch("requests.get", side_effect=ConnectionError("refused")):
            assert signal_bot._receive_rest() == []


class TestSendRest:
    def test_posts_expected_payload(self) -> None:
        fake_resp = MagicMock()
        fake_resp.raise_for_status.return_value = None
        with patch("requests.post", return_value=fake_resp) as post:
            signal_bot._send_rest(ALLOWED_NUMBER, "hello")
        url, kwargs = post.call_args.args[0], post.call_args.kwargs
        assert url.endswith("/v2/send")
        assert kwargs["json"] == {
            "message": "hello",
            "number": BOT_NUMBER,
            "recipients": [ALLOWED_NUMBER],
        }


# ---------------------------------------------------------------------------
# SignalBot — poll loop, whitelist enforcement, graceful shutdown
# ---------------------------------------------------------------------------


class TestSignalBotPollOnce:
    def test_allowed_sender_dispatches_and_replies(self) -> None:
        bot = signal_bot.SignalBot(mode="cli")
        envelopes = [
            {"envelope": {"sourceNumber": ALLOWED_NUMBER, "dataMessage": {"message": "/help"}}}
        ]
        with (
            patch.object(signal_bot, "_receive_cli", return_value=envelopes),
            patch.object(bot, "send") as send,
        ):
            bot._poll_once()
        send.assert_called_once()
        assert send.call_args.args[0] == ALLOWED_NUMBER

    def test_denied_sender_not_dispatched_no_reply(self) -> None:
        bot = signal_bot.SignalBot(mode="cli")
        envelopes = [
            {"envelope": {"sourceNumber": DENIED_NUMBER, "dataMessage": {"message": "/run x y"}}}
        ]
        with (
            patch.object(signal_bot, "_receive_cli", return_value=envelopes),
            patch.object(signal_bot, "_dispatch_text") as dispatch,
            patch.object(bot, "send") as send,
        ):
            bot._poll_once()
        dispatch.assert_not_called()
        send.assert_not_called()

    def test_rest_mode_uses_receive_rest(self) -> None:
        bot = signal_bot.SignalBot(mode="rest")
        with (
            patch.object(signal_bot, "_receive_rest", return_value=[]) as receive_rest,
            patch.object(signal_bot, "_receive_cli") as receive_cli,
        ):
            bot._poll_once()
        receive_rest.assert_called_once()
        receive_cli.assert_not_called()


class TestSignalBotRun:
    def test_unknown_mode_rejected(self) -> None:
        with pytest.raises(RuntimeError, match="Unknown Signal receive mode"):
            signal_bot.SignalBot(mode="carrier-pigeon")

    def test_missing_binary_raises_before_looping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.shutil, "which", lambda _: None)
        bot = signal_bot.SignalBot(mode="cli")
        with patch.object(bot, "_poll_once") as poll:
            with pytest.raises(RuntimeError, match="signal-cli not found"):
                bot.run()
        poll.assert_not_called()

    def test_graceful_shutdown_stops_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.shutil, "which", lambda _: "/usr/bin/signal-cli")
        bot = signal_bot.SignalBot(mode="cli", poll_interval=0.01)
        calls = {"n": 0}

        def _fake_poll() -> None:
            calls["n"] += 1
            if calls["n"] >= 2:
                bot.stop()

        with (
            patch.object(bot, "_poll_once", side_effect=_fake_poll),
            patch.object(signal_bot.signal_module, "signal"),
        ):
            bot.run()
        assert calls["n"] == 2


# ---------------------------------------------------------------------------
# notify / notify_approval_required
# ---------------------------------------------------------------------------


class TestNotify:
    def test_sends_to_notification_number(self) -> None:
        with patch.object(signal_bot, "_send_cli") as send_cli:
            signal_bot.notify("hello")
        send_cli.assert_called_once_with(ALLOWED_NUMBER, "hello")

    def test_unconfigured_notification_number_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.settings, "signal_notification_number", None)
        with pytest.raises(RuntimeError, match="notification number"):
            signal_bot.notify("hello")

    def test_rest_mode_routes_to_send_rest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.settings, "signal_receive_mode", "rest")
        with patch.object(signal_bot, "_send_rest") as send_rest:
            signal_bot.notify("hello")
        send_rest.assert_called_once_with(ALLOWED_NUMBER, "hello")


class TestNotifyApprovalRequired:
    def test_sends_text_only_instructions(self) -> None:
        with patch.object(signal_bot, "_send_cli") as send_cli:
            signal_bot.notify_approval_required(run_id=42, project="acme", task="deploy")
        recipient, message = send_cli.call_args.args
        assert recipient == ALLOWED_NUMBER
        assert "42" in message and "acme" in message and "deploy" in message
        assert "approve 42" in message and "deny 42" in message

    def test_unconfigured_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.settings, "signal_notification_number", None)
        with pytest.raises(RuntimeError):
            signal_bot.notify_approval_required(run_id=1, project="acme", task="deploy")


# ---------------------------------------------------------------------------
# register / link / info
# ---------------------------------------------------------------------------


class TestRegister:
    def test_missing_binary_raises_no_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.shutil, "which", lambda _: None)
        with patch.object(signal_bot.subprocess, "run") as run:
            with pytest.raises(RuntimeError, match="signal-cli not found"):
                signal_bot.register(BOT_NUMBER)
        run.assert_not_called()

    def test_default_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.shutil, "which", lambda _: "/usr/bin/signal-cli")
        with patch.object(signal_bot.subprocess, "run") as run:
            signal_bot.register(BOT_NUMBER)
        argv = run.call_args.args[0]
        assert argv == ["/usr/bin/signal-cli", "-a", BOT_NUMBER, "register"]

    def test_voice_and_captcha_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.shutil, "which", lambda _: "/usr/bin/signal-cli")
        with patch.object(signal_bot.subprocess, "run") as run:
            signal_bot.register(BOT_NUMBER, voice=True, captcha="captcha-token")
        argv = run.call_args.args[0]
        assert "--voice" in argv
        assert "--captcha" in argv and "captcha-token" in argv


class TestLink:
    def test_missing_binary_raises_no_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.shutil, "which", lambda _: None)
        with patch.object(signal_bot.subprocess, "run") as run:
            with pytest.raises(RuntimeError, match="signal-cli not found"):
                signal_bot.link()
        run.assert_not_called()

    def test_default_device_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.shutil, "which", lambda _: "/usr/bin/signal-cli")
        with patch.object(signal_bot.subprocess, "run") as run:
            signal_bot.link()
        argv = run.call_args.args[0]
        assert argv == ["/usr/bin/signal-cli", "link", "-n", "hivepilot"]


class TestInfo:
    def test_reports_configuration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.shutil, "which", lambda _: "/usr/bin/signal-cli")
        result = signal_bot.info()
        assert result["mode"] == "cli"
        assert result["number"] == BOT_NUMBER
        assert result["cli_available"] is True
        assert result["allowed_numbers"] == [ALLOWED_NUMBER]

    def test_reports_missing_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(signal_bot.shutil, "which", lambda _: None)
        assert signal_bot.info()["cli_available"] is False


# ---------------------------------------------------------------------------
# send_approval_keyboard routes to Signal alongside Telegram/Slack/Discord
# ---------------------------------------------------------------------------


class TestSendApprovalKeyboardRoutesToSignal:
    def test_signal_branch_invoked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """conftest stubs send_approval_keyboard to a no-op for all other tests
        (to suppress real notifications). Restore the real implementation the
        same way tests/test_rich_approval.py does, then verify the Signal
        branch is reached alongside Telegram/Slack/Discord."""
        import importlib.util
        import types

        import hivepilot.services.notification_service as ns_mod
        import hivepilot.services.telegram_bot as tgb

        spec = importlib.util.find_spec("hivepilot.services.notification_service")
        assert spec is not None
        source = spec.loader.get_source(  # type: ignore[attr-defined]
            "hivepilot.services.notification_service"
        )
        tmp_mod = types.ModuleType("_tmp_ns_signal")
        tmp_mod.__spec__ = spec  # type: ignore[assignment]
        exec(compile(source, spec.origin, "exec"), tmp_mod.__dict__)  # noqa: S102
        real_send = tmp_mod.send_approval_keyboard

        monkeypatch.setattr(ns_mod, "send_approval_keyboard", real_send)

        slack_stub = MagicMock()
        slack_stub.notify_approval_required = lambda **kw: None
        discord_stub = MagicMock()
        discord_stub.notify_approval_required = lambda **kw: None
        signal_recorded: list[dict[str, Any]] = []
        signal_stub = MagicMock()
        signal_stub.notify_approval_required = lambda **kw: signal_recorded.append(kw)

        with (
            patch.object(tgb, "notify_approval_required", side_effect=lambda **kw: None),
            patch.dict(
                "sys.modules",
                {
                    "hivepilot.services.slack_bot": slack_stub,
                    "hivepilot.services.discord_bot": discord_stub,
                    "hivepilot.services.signal_bot": signal_stub,
                },
            ),
        ):
            ns_mod.send_approval_keyboard(run_id=7, project="acme", task="deploy")

        assert signal_recorded == [{"run_id": 7, "project": "acme", "task": "deploy"}]

    def test_signal_failure_does_not_break_other_channels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Signal notify failure (e.g. unconfigured) must be swallowed —
        mirrors the existing Slack/Discord try/except-pass branches."""
        import importlib.util
        import types

        import hivepilot.services.notification_service as ns_mod
        import hivepilot.services.telegram_bot as tgb

        spec = importlib.util.find_spec("hivepilot.services.notification_service")
        assert spec is not None
        source = spec.loader.get_source(  # type: ignore[attr-defined]
            "hivepilot.services.notification_service"
        )
        tmp_mod = types.ModuleType("_tmp_ns_signal2")
        tmp_mod.__spec__ = spec  # type: ignore[assignment]
        exec(compile(source, spec.origin, "exec"), tmp_mod.__dict__)  # noqa: S102
        real_send = tmp_mod.send_approval_keyboard
        monkeypatch.setattr(ns_mod, "send_approval_keyboard", real_send)

        slack_stub = MagicMock()
        slack_stub.notify_approval_required = lambda **kw: None
        discord_stub = MagicMock()
        discord_stub.notify_approval_required = lambda **kw: None
        signal_stub = MagicMock()

        def _raise(**kw: Any) -> None:
            raise RuntimeError("no notification number configured")

        signal_stub.notify_approval_required = _raise

        with (
            patch.object(tgb, "notify_approval_required", side_effect=lambda **kw: None),
            patch.dict(
                "sys.modules",
                {
                    "hivepilot.services.slack_bot": slack_stub,
                    "hivepilot.services.discord_bot": discord_stub,
                    "hivepilot.services.signal_bot": signal_stub,
                },
            ),
        ):
            # Must not raise.
            ns_mod.send_approval_keyboard(run_id=7, project="acme", task="deploy")


# ---------------------------------------------------------------------------
# CLI wiring — hivepilot signal start|notify|register|link|info
# ---------------------------------------------------------------------------


class TestCli:
    def test_start_missing_binary_exits_1_with_friendly_error(self) -> None:
        from hivepilot.cli import app

        runner = CliRunner()
        with patch(
            "hivepilot.services.signal_bot.SignalBot.run",
            side_effect=RuntimeError("signal-cli not found on PATH"),
        ):
            result = runner.invoke(app, ["signal", "start", "--mode", "cli"])
        assert result.exit_code == 1
        assert "signal-cli not found" in result.output

    def test_start_unknown_mode_exits_1(self) -> None:
        from hivepilot.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["signal", "start", "--mode", "carrier-pigeon"])
        assert result.exit_code == 1

    def test_notify_sends_message(self) -> None:
        from hivepilot.cli import app

        runner = CliRunner()
        with patch("hivepilot.services.signal_bot.notify") as notify:
            result = runner.invoke(app, ["signal", "notify", "hello"])
        assert result.exit_code == 0
        notify.assert_called_once_with("hello")

    def test_notify_error_exits_1(self) -> None:
        from hivepilot.cli import app

        runner = CliRunner()
        with patch(
            "hivepilot.services.signal_bot.notify",
            side_effect=RuntimeError("No Signal notification number configured"),
        ):
            result = runner.invoke(app, ["signal", "notify", "hello"])
        assert result.exit_code == 1

    def test_register_missing_binary_exits_1(self) -> None:
        from hivepilot.cli import app

        runner = CliRunner()
        with patch(
            "hivepilot.services.signal_bot.register",
            side_effect=RuntimeError("signal-cli not found on PATH"),
        ):
            result = runner.invoke(app, ["signal", "register", BOT_NUMBER])
        assert result.exit_code == 1

    def test_register_prints_verify_hint(self) -> None:
        from hivepilot.cli import app

        runner = CliRunner()
        with patch("hivepilot.services.signal_bot.register") as register:
            result = runner.invoke(app, ["signal", "register", BOT_NUMBER, "--voice"])
        assert result.exit_code == 0
        register.assert_called_once_with(BOT_NUMBER, voice=True, captcha=None)
        assert "verify" in result.output.lower()

    def test_link_missing_binary_exits_1(self) -> None:
        from hivepilot.cli import app

        runner = CliRunner()
        with patch(
            "hivepilot.services.signal_bot.link",
            side_effect=RuntimeError("signal-cli not found on PATH"),
        ):
            result = runner.invoke(app, ["signal", "link"])
        assert result.exit_code == 1

    def test_info_prints_configuration(self) -> None:
        from hivepilot.cli import app

        runner = CliRunner()
        with patch(
            "hivepilot.services.signal_bot.info",
            return_value={"mode": "cli", "number": BOT_NUMBER, "cli_available": True},
        ):
            result = runner.invoke(app, ["signal", "info"])
        assert result.exit_code == 0
        assert "cli" in result.output
        assert BOT_NUMBER in result.output
