"""HP-75 — inbound mail watcher policy: allowlist, dedup, bounded admission,
restricted dispatch, and a read-only IMAP client. `poll_once` is exercised with
an injected client + dispatch (no live mailbox); the DB is isolated per test."""

from __future__ import annotations

import yaml

from hivepilot.services import mail_watcher, state_service
from hivepilot.services.mail_watcher import (
    InboundMessage,
    MailWatcher,
    load_mail_watchers,
    poll_once,
    sender_allowed,
)


class FakeClient:
    def __init__(self, messages: list[InboundMessage]) -> None:
        self._messages = messages

    def fetch_unseen(self) -> list[InboundMessage]:
        return list(self._messages)


def _watcher(
    *,
    enabled: bool = True,
    allow_senders: list[str] | None = None,
    max_admission_failures: int = 3,
) -> MailWatcher:
    return MailWatcher(
        name="support",
        host="imap.example.com",
        username="bot@example.com",
        password="${env:IMAP_PW}",
        task="triage",
        projects=["support"],
        allow_senders=["@example.com"] if allow_senders is None else allow_senders,
        max_admission_failures=max_admission_failures,
        enabled=enabled,
    )


def _msg(mid="<m1>", sender="alice@example.com", subject="hi", body="hello") -> InboundMessage:
    return InboundMessage(message_id=mid, sender=sender, subject=subject, body=body)


class TestSenderAllowlist:
    def test_empty_allowlist_admits_nothing(self):
        assert sender_allowed("a@x.com", []) is False

    def test_exact_and_domain_rules(self):
        assert sender_allowed("boss@acme.com", ["boss@acme.com"]) is True
        assert sender_allowed("anyone@example.com", ["@example.com"]) is True
        assert sender_allowed("evil@other.com", ["@example.com"]) is False

    def test_name_and_angle_brackets_are_parsed(self):
        assert sender_allowed("Alice <alice@example.com>", ["@example.com"]) is True


class TestPollOnce:
    def test_disabled_watcher_does_nothing(self):
        calls: list = []
        result = poll_once(
            _watcher(enabled=False), FakeClient([_msg()]), dispatch=lambda c, m: calls.append(m)
        )
        assert result.disabled is True
        assert calls == []

    def test_allowlisted_message_is_dispatched_and_recorded(self):
        calls: list = []
        result = poll_once(
            _watcher(), FakeClient([_msg()]), dispatch=lambda c, m: calls.append(m.message_id)
        )
        assert result.dispatched == 1 and calls == ["<m1>"]
        assert state_service.get_mail_processed("support", "<m1>")["status"] == "dispatched"

    def test_non_allowlisted_sender_is_rejected_never_dispatched(self):
        calls: list = []
        result = poll_once(
            _watcher(),
            FakeClient([_msg(sender="stranger@evil.com")]),
            dispatch=lambda c, m: calls.append(m),
        )
        assert result.rejected == 1 and calls == []
        assert state_service.get_mail_processed("support", "<m1>")["status"] == "skipped"

    def test_a_processed_message_is_not_dispatched_again(self):
        count = {"n": 0}

        def _d(c, m):
            count["n"] += 1

        client = FakeClient([_msg()])
        poll_once(_watcher(), client, dispatch=_d)
        result2 = poll_once(_watcher(), client, dispatch=_d)  # same message id
        assert count["n"] == 1  # dispatched once only
        assert result2.duplicates == 1

    def test_missing_message_id_gets_a_stable_synthetic_id(self):
        """Two different no-id messages must not collapse onto one empty key."""
        calls: list = []
        a = _msg(mid="", sender="alice@example.com", subject="one", body="aaa")
        b = _msg(mid="", sender="alice@example.com", subject="two", body="bbb")
        result = poll_once(
            _watcher(),
            FakeClient([a, b]),
            dispatch=lambda c, m: calls.append(m.message_id),
        )
        assert result.dispatched == 2
        assert all(mid.startswith("<synth-") for mid in calls)
        assert calls[0] != calls[1]
        # same content again → same synthetic id → duplicate
        result2 = poll_once(
            _watcher(), FakeClient([a]), dispatch=lambda c, m: calls.append(m.message_id)
        )
        assert result2.duplicates == 1

    def test_dispatch_failure_is_bounded_then_skipped(self):
        def _boom(c, m):
            raise RuntimeError("downstream down")

        w = _watcher(max_admission_failures=2)
        r1 = poll_once(w, FakeClient([_msg()]), dispatch=_boom)
        assert r1.failed == 1  # attempt 1 < 2 → pending
        assert state_service.get_mail_processed("support", "<m1>")["status"] == "pending"
        r2 = poll_once(w, FakeClient([_msg()]), dispatch=_boom)
        assert r2.skipped == 1  # attempt 2 >= 2 → skipped (given up)
        assert state_service.get_mail_processed("support", "<m1>")["status"] == "skipped"


class TestLoadMailWatchers:
    def test_parses_entries_with_defaults(self, tmp_path, monkeypatch):
        path = tmp_path / "mail_watchers.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "mail_watchers": {
                        "support": {
                            "host": "imap.example.com",
                            "username": "bot@example.com",
                            "password": "${env:IMAP_PW}",
                            "task": "triage",
                            "projects": ["support"],
                            "allow_senders": ["@example.com"],
                            "enabled": True,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(mail_watcher.settings, "mail_watchers_file", path)
        watchers = load_mail_watchers()
        w = watchers["support"]
        assert w.enabled is True and w.port == 993 and w.mailbox == "INBOX"
        assert w.allow_senders == ["@example.com"] and w.max_admission_failures == 3


class TestPollAll:
    def test_no_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mail_watcher.settings, "mail_watchers_file", tmp_path / "missing.yaml")
        assert mail_watcher.poll_all() == {}

    def test_disabled_watcher_is_reported_not_connected(self, tmp_path, monkeypatch):
        path = tmp_path / "mail_watchers.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "mail_watchers": {
                        "support": {
                            "host": "imap.example.com",
                            "username": "bot@example.com",
                            "task": "triage",
                            "enabled": False,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(mail_watcher.settings, "mail_watchers_file", path)
        results = mail_watcher.poll_all()
        assert results["support"].disabled is True


class TestResolveSecret:
    def test_env_placeholder_and_literal(self, monkeypatch):
        monkeypatch.setenv("IMAP_PW", "s3cret")
        assert mail_watcher._resolve_secret("${env:IMAP_PW}") == "s3cret"
        assert mail_watcher._resolve_secret("literal") == "literal"


class TestImapClientIsReadOnly:
    def test_select_is_readonly_and_message_is_parsed(self, monkeypatch):
        raw = b"Message-ID: <abc>\r\nFrom: Alice <a@example.com>\r\nSubject: Hi\r\n\r\nbody text"
        calls: list = []

        class FakeIMAP:
            def __init__(self, host, port):
                calls.append(("connect", host, port))

            def login(self, u, p):
                calls.append(("login", u))

            def select(self, mailbox, readonly=False):
                calls.append(("select", mailbox, readonly))
                return ("OK", [b""])

            def search(self, charset, *criteria):
                return ("OK", [b"1"])

            def fetch(self, num, spec):
                return ("OK", [(b"1 (RFC822)", raw)])

            def logout(self):
                calls.append(("logout",))

        import imaplib

        monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
        client = mail_watcher.ImapMailClient(_watcher())
        messages = client.fetch_unseen()

        assert ("select", "INBOX", True) in calls  # read-only select, never sets \Seen
        assert messages[0].message_id == "<abc>"
        assert messages[0].sender == "Alice <a@example.com>"
        assert messages[0].body == "body text"
