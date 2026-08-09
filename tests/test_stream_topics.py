"""Tests for Telegram forum topic routing (per-agent message threads).

Includes the cwd-independence regression: `_TOPICS_REGISTRY_PATH` used to be
a relative `.hivepilot/stream_topics.json`, so an OpenRC service (cwd=/) and
a CLI run from a login shell (cwd=$HOME) each maintained their OWN registry
-- the second context never saw a topic the first had already created, so
`createForumTopic` ran again and duplicated the forum topic in Telegram.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from hivepilot.services.notification_service import (
    _ensure_topic_thread,
    _load_topics,
    _resolve_agent_key,
    _save_topics,
    _send_telegram,
    _topics_registry_path,
    stream_agent_turn,
)

# ---------------------------------------------------------------------------
# _resolve_agent_key
# ---------------------------------------------------------------------------


class TestResolveAgentKey:
    def test_cto_by_display_name(self):
        assert _resolve_agent_key("Blaise (CTO)") == "cto"

    def test_developer_by_display_name(self):
        assert _resolve_agent_key("Gustave (Developer)") == "developer"

    def test_ceo_accent(self):
        assert _resolve_agent_key("Aliénor (CEO)") == "ceo"

    def test_chief_of_staff(self):
        key = _resolve_agent_key("Jules (Chief of Staff)")
        assert key == "chief_of_staff"

    def test_an_unknown_actor_gets_no_key(self):
        """Deliberately inverted. This used to assert a non-empty slug, and
        that slug was the defect: it became a permanent Telegram topic, so
        every unmatched actor minted one. `Orchestrator._agent_name` falls
        back to `stage.name` for a roleless task, which meant pipeline stages
        grew topics -- `refresh` and `pentest` were found in the live
        registry. None sends to the General topic instead, which is
        recoverable; a stray topic has to be deleted by hand.

        See TestOnlyDeclaredNonRoleStreamsGetATopic in
        test_topic_naming_convention.py for the full behaviour.
        """
        assert _resolve_agent_key("SomethingUnknown (Foo)") is None

    def test_an_empty_actor_gets_no_key(self):
        """Was `general` — a literal topic named after the absence of an
        actor, which is a topic nobody ever asked for."""
        assert _resolve_agent_key("") is None


# ---------------------------------------------------------------------------
# _load_topics / _save_topics
# ---------------------------------------------------------------------------


class TestTopicsRegistry:
    def test_round_trip(self, tmp_path: Path, monkeypatch):
        registry_file = tmp_path / "stream_topics.json"
        monkeypatch.setattr(
            "hivepilot.services.notification_service._topics_registry_path",
            lambda: registry_file,
        )
        data = {"cto": 42, "developer": 99}
        _save_topics(data)
        loaded = _load_topics()
        assert loaded == data

    def test_load_missing_file_returns_empty(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            "hivepilot.services.notification_service._topics_registry_path",
            lambda: tmp_path / "nonexistent.json",
        )
        # No legacy file at cwd either -> empty.
        monkeypatch.chdir(tmp_path)
        assert _load_topics() == {}

    def test_save_creates_parent_dir(self, tmp_path: Path, monkeypatch):
        nested = tmp_path / "a" / "b" / "topics.json"
        monkeypatch.setattr(
            "hivepilot.services.notification_service._topics_registry_path",
            lambda: nested,
        )
        _save_topics({"ceo": 1})
        assert nested.exists()
        assert json.loads(nested.read_text()) == {"ceo": 1}


# ---------------------------------------------------------------------------
# _topics_registry_path — absolute, cwd-independent resolution
# ---------------------------------------------------------------------------


class TestTopicsRegistryPathIsAbsoluteAndCwdIndependent:
    def test_default_path_is_absolute(self, monkeypatch):
        monkeypatch.setattr(
            "hivepilot.services.notification_service.settings.stream_topics_registry_path",
            None,
            raising=False,
        )
        assert _topics_registry_path().is_absolute()

    def test_same_resolved_path_across_different_cwds(self, tmp_path, monkeypatch):
        """The core regression: two different process cwds must resolve to
        the SAME absolute registry path, so one context sees the topics the
        other one created (no duplicate `createForumTopic`)."""
        cwd_a = tmp_path / "a"
        cwd_b = tmp_path / "root"
        cwd_a.mkdir()
        cwd_b.mkdir()

        monkeypatch.setattr(
            "hivepilot.services.notification_service.settings.stream_topics_registry_path",
            None,
            raising=False,
        )

        monkeypatch.chdir(cwd_a)
        path_from_a = _topics_registry_path()

        monkeypatch.chdir(cwd_b)
        path_from_b = _topics_registry_path()

        assert path_from_a == path_from_b
        assert path_from_a.is_absolute()

    def test_topic_saved_in_one_cwd_is_found_in_another(self, tmp_path, monkeypatch):
        """End-to-end regression: save from cwd A, load from cwd B -> same
        entries (no second registry, no duplicate topic creation)."""
        xdg_data_home = tmp_path / "xdg-data"
        cwd_a = tmp_path / "openrc-root"
        cwd_b = tmp_path / "login-shell-home"
        cwd_a.mkdir()
        cwd_b.mkdir()

        monkeypatch.setattr(
            "hivepilot.services.notification_service.settings.stream_topics_registry_path",
            None,
            raising=False,
        )
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data_home))

        monkeypatch.chdir(cwd_a)
        _save_topics({"cto": 175})

        monkeypatch.chdir(cwd_b)
        assert _load_topics() == {"cto": 175}

    def test_explicit_override_is_resolved_absolute(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "hivepilot.services.notification_service.settings.stream_topics_registry_path",
            tmp_path / "custom" / "topics.json",
            raising=False,
        )
        assert _topics_registry_path() == tmp_path / "custom" / "topics.json"


# ---------------------------------------------------------------------------
# Legacy migration — cwd-relative `.hivepilot/stream_topics.json` -> new path
# ---------------------------------------------------------------------------


class TestLegacyTopicsMigration:
    def test_migrates_legacy_relative_registry(self, tmp_path, monkeypatch):
        new_path = tmp_path / "new" / "stream_topics.json"
        legacy_cwd = tmp_path / "legacy-cwd"
        legacy_dir = legacy_cwd / ".hivepilot"
        legacy_dir.mkdir(parents=True)
        legacy_file = legacy_dir / "stream_topics.json"
        legacy_file.write_text(json.dumps({"cto": 175, "developer": 176}), encoding="utf-8")

        monkeypatch.setattr(
            "hivepilot.services.notification_service._topics_registry_path",
            lambda: new_path,
        )
        monkeypatch.chdir(legacy_cwd)

        loaded = _load_topics()

        assert loaded == {"cto": 175, "developer": 176}
        # The migration is persisted at the NEW path so it only happens once.
        assert new_path.exists()
        assert json.loads(new_path.read_text(encoding="utf-8")) == {
            "cto": 175,
            "developer": 176,
        }

    def test_new_path_wins_over_legacy(self, tmp_path, monkeypatch):
        new_path = tmp_path / "new" / "stream_topics.json"
        new_path.parent.mkdir(parents=True)
        new_path.write_text(json.dumps({"cto": 999}), encoding="utf-8")

        legacy_cwd = tmp_path / "legacy-cwd"
        legacy_dir = legacy_cwd / ".hivepilot"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "stream_topics.json").write_text(json.dumps({"cto": 175}), encoding="utf-8")

        monkeypatch.setattr(
            "hivepilot.services.notification_service._topics_registry_path",
            lambda: new_path,
        )
        monkeypatch.chdir(legacy_cwd)

        assert _load_topics() == {"cto": 999}

    def test_no_legacy_no_new_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "hivepilot.services.notification_service._topics_registry_path",
            lambda: tmp_path / "new" / "stream_topics.json",
        )
        monkeypatch.chdir(tmp_path)  # no .hivepilot/ dir here
        assert _load_topics() == {}


# ---------------------------------------------------------------------------
# _ensure_topic_thread — reuse cached thread id vs create + persist
# ---------------------------------------------------------------------------


class TestEnsureTopicThread:
    def _make_settings(self):
        s = MagicMock()
        s.telegram_bot_token = "tok"
        s.telegram_stream_chat_id = 999
        return s

    def test_reuses_cached_thread_id_without_creating(self, tmp_path, monkeypatch):
        registry_file = tmp_path / "stream_topics.json"
        registry_file.write_text(json.dumps({"cto": 175}), encoding="utf-8")
        monkeypatch.setattr(
            "hivepilot.services.notification_service._topics_registry_path",
            lambda: registry_file,
        )

        create_calls = {"count": 0}

        def fake_post(*args, **kwargs):
            create_calls["count"] += 1
            raise AssertionError("createForumTopic must not be called for a cached agent_key")

        with (
            patch("hivepilot.services.notification_service.settings", self._make_settings()),
            patch("hivepilot.services.notification_service.requests.post", fake_post),
        ):
            thread_id = _ensure_topic_thread("cto", "Blaise (CTO)")

        assert thread_id == 175
        assert create_calls["count"] == 0

    def test_creates_and_persists_when_absent(self, tmp_path, monkeypatch):
        registry_file = tmp_path / "stream_topics.json"
        monkeypatch.setattr(
            "hivepilot.services.notification_service._topics_registry_path",
            lambda: registry_file,
        )

        def fake_post(url, json=None, timeout=None):
            r = MagicMock()
            r.json.return_value = {"ok": True, "result": {"message_thread_id": 208}}
            return r

        with (
            patch("hivepilot.services.notification_service.settings", self._make_settings()),
            patch("hivepilot.services.notification_service.requests.post", fake_post),
        ):
            thread_id = _ensure_topic_thread("cto", "Blaise (CTO)")

        assert thread_id == 208
        assert json.loads(registry_file.read_text(encoding="utf-8")) == {"cto": 208}


# ---------------------------------------------------------------------------
# Best-effort: an unwritable registry path never crashes streaming
# ---------------------------------------------------------------------------


class TestUnwritableRegistryIsBestEffort:
    def test_save_to_unwritable_path_does_not_raise(self, tmp_path, monkeypatch):
        unwritable_dir = tmp_path / "readonly"
        unwritable_dir.mkdir()
        os.chmod(unwritable_dir, 0o400)
        target = unwritable_dir / "nested" / "stream_topics.json"

        monkeypatch.setattr(
            "hivepilot.services.notification_service._topics_registry_path",
            lambda: target,
        )
        try:
            _save_topics({"cto": 1})  # must not raise
        finally:
            os.chmod(unwritable_dir, 0o700)

    def test_ensure_topic_thread_still_returns_id_when_save_fails(self, tmp_path, monkeypatch):
        unwritable_dir = tmp_path / "readonly"
        unwritable_dir.mkdir()
        os.chmod(unwritable_dir, 0o400)
        target = unwritable_dir / "nested" / "stream_topics.json"

        monkeypatch.setattr(
            "hivepilot.services.notification_service._topics_registry_path",
            lambda: target,
        )

        def fake_post(url, json=None, timeout=None):
            r = MagicMock()
            r.json.return_value = {"ok": True, "result": {"message_thread_id": 42}}
            return r

        settings_mock = MagicMock()
        settings_mock.telegram_bot_token = "tok"
        settings_mock.telegram_stream_chat_id = 999

        try:
            with (
                patch("hivepilot.services.notification_service.settings", settings_mock),
                patch("hivepilot.services.notification_service.requests.post", fake_post),
            ):
                thread_id = _ensure_topic_thread("cto", "Blaise (CTO)")
        finally:
            os.chmod(unwritable_dir, 0o700)

        assert thread_id == 42  # best-effort: create still succeeds even if persist fails


# ---------------------------------------------------------------------------
# _send_telegram — message_thread_id in payload
# ---------------------------------------------------------------------------


class TestSendTelegramThreadId:
    def _make_settings(self, **kw):
        s = MagicMock()
        s.telegram_bot_token = "tok"
        s.telegram_notification_chat_id = 123
        s.telegram_allowed_chat_ids = []
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def test_includes_thread_id_when_provided(self):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["payload"] = json
            r = MagicMock()
            r.raise_for_status = lambda: None
            return r

        with (
            patch("hivepilot.services.notification_service.requests.post", fake_post),
            patch("hivepilot.services.notification_service.settings", self._make_settings()),
        ):
            _send_telegram("hello", chat_id=123, message_thread_id=777)

        assert captured["payload"]["message_thread_id"] == 777

    def test_omits_thread_id_when_none(self):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["payload"] = json
            r = MagicMock()
            r.raise_for_status = lambda: None
            return r

        with (
            patch("hivepilot.services.notification_service.requests.post", fake_post),
            patch("hivepilot.services.notification_service.settings", self._make_settings()),
        ):
            _send_telegram("hello", chat_id=123, message_thread_id=None)

        assert "message_thread_id" not in captured["payload"]


# ---------------------------------------------------------------------------
# stream_agent_turn — topics enabled vs disabled
# ---------------------------------------------------------------------------


class TestStreamAgentTurnTopics:
    def _make_settings(self, topics_enabled: bool):
        s = MagicMock()
        s.telegram_stream_live = True
        s.telegram_stream_topics = topics_enabled
        s.telegram_stream_chat_id = 999
        s.telegram_bot_token = "tok"
        s.telegram_notification_chat_id = None
        s.telegram_allowed_chat_ids = []
        return s

    def test_topics_enabled_calls_ensure_and_passes_thread_id(self):
        fake_thread_id = 42
        sent_kwargs = {}

        def fake_send(message, chat_id=None, message_thread_id=None, parse_mode=None):
            sent_kwargs["message_thread_id"] = message_thread_id

        with (
            patch("hivepilot.services.notification_service.settings", self._make_settings(True)),
            patch(
                "hivepilot.services.notification_service._ensure_topic_thread",
                return_value=fake_thread_id,
            ) as mock_ensure,
            patch("hivepilot.services.notification_service._send_telegram", fake_send),
        ):
            stream_agent_turn(actor="Blaise (CTO)", stage="planning")

        mock_ensure.assert_called_once()
        assert sent_kwargs["message_thread_id"] == fake_thread_id

    def test_topics_disabled_passes_none_thread_id(self):
        sent_kwargs = {}

        def fake_send(message, chat_id=None, message_thread_id=None, parse_mode=None):
            sent_kwargs["message_thread_id"] = message_thread_id

        with (
            patch("hivepilot.services.notification_service.settings", self._make_settings(False)),
            patch("hivepilot.services.notification_service._send_telegram", fake_send),
        ):
            stream_agent_turn(actor="Blaise (CTO)", stage="planning")

        assert sent_kwargs["message_thread_id"] is None
