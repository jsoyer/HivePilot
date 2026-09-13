"""P0 Telegram stop-bleed (HP-93).

Forum topics duplicate because the Bot API cannot list or dedupe names.
These tests pin the five stop-bleed invariants:

1. No createForumTopic for ``run:{id}``.
2. Persistent doors are not reminted when stale — Inbox or DM instead.
3. ``ensure_pollen_doors`` is a no-op when any door already exists.
4. ``bootstrap`` mints only when the set is empty.
5. ``_invalidate_topic`` drops the SQLite mirror; create+register is locked.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.services import notification_service as ns
from hivepilot.services import topics_admin


def _telegram_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ns.settings, "telegram_bot_token", "tok", raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", -100999, raising=False)
    monkeypatch.setattr(ns.settings, "telegram_stream_topics", True, raising=False)
    ns._reset_topic_creation_budget()


def _registry(tmp_path, monkeypatch: pytest.MonkeyPatch, mapping: dict[str, int] | None = None):
    path = tmp_path / "stream_topics.json"
    if mapping is not None:
        path.write_text(json.dumps(mapping), encoding="utf-8")
    monkeypatch.setattr(ns, "_topics_registry_path", lambda: path)
    monkeypatch.setattr(ns, "_mirror_reconciled", False)
    return path


def _recording_create(thread_id: int = 900):
    calls: list[str] = []

    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        resp = MagicMock()
        resp.json.return_value = {"ok": True, "result": {"message_thread_id": thread_id}}
        return resp

    return calls, fake_post


class TestNoRunTopicCreate:
    def test_ensure_refuses_run_key(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        _telegram_ready(monkeypatch)
        _registry(tmp_path, monkeypatch, {})
        calls, fake_post = _recording_create()

        with patch.object(ns.requests, "post", fake_post):
            assert ns._ensure_topic_thread("run:42", "🛠️ acme") is None

        assert calls == []
        assert "createForumTopic" not in "".join(calls)

    def test_stream_turn_with_run_id_stays_on_runs_door(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _telegram_ready(monkeypatch)
        monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
        monkeypatch.setattr(ns.settings, "telegram_stream_rich", False, raising=False)
        _registry(tmp_path, monkeypatch, {"runs": 88, "inbox": 11})
        ensure_keys: list[str] = []

        def spy_ensure(agent_key: str, title: str, *, allow_create: bool = False):
            ensure_keys.append(agent_key)
            raise AssertionError("createForumTopic path must not run for a run-indexed turn")

        sent: list[dict] = []

        def fake_send(message, chat_id=None, message_thread_id=None, parse_mode=None):
            sent.append({"thread": message_thread_id, "msg": message})

        monkeypatch.setattr(ns, "_ensure_topic_thread", spy_ensure)
        monkeypatch.setattr(ns, "_send_telegram", fake_send)

        ns.stream_agent_turn(
            actor="Blaise (CTO)",
            stage="planning",
            run_id=42,
            run_slug="acme-api",
        )

        assert ensure_keys == []
        assert sent and sent[0]["thread"] == 88


class TestEnsurePollenDoorsNoRemint:
    def test_noop_when_any_door_present(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        _telegram_ready(monkeypatch)
        _registry(tmp_path, monkeypatch, {"inbox": 1})
        calls, fake_post = _recording_create()

        with patch.object(ns.requests, "post", fake_post):
            landed = ns.ensure_pollen_doors()

        assert landed == {"inbox": 1}
        assert calls == []

    def test_empty_registry_does_not_mint(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        _telegram_ready(monkeypatch)
        _registry(tmp_path, monkeypatch, {})
        calls, fake_post = _recording_create()

        with patch.object(ns.requests, "post", fake_post):
            landed = ns.ensure_pollen_doors()

        assert landed == {}
        assert calls == []


class TestBootstrapMintsOnlyWhenEmpty:
    def test_dry_run_does_not_create(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        _telegram_ready(monkeypatch)
        _registry(tmp_path, monkeypatch, {})
        calls, fake_post = _recording_create()

        with patch.object(ns.requests, "post", fake_post):
            result = topics_admin.bootstrap(confirm=False)

        assert result.dry_run is True
        assert result.minted == {}
        assert calls == []

    def test_mints_four_doors_when_empty(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        _telegram_ready(monkeypatch)
        path = _registry(tmp_path, monkeypatch, {})
        ids = {"Inbox": 1, "Approvals": 2, "Runs": 3, "Alerts": 4}

        def fake_post(url, json=None, timeout=None):
            if url.endswith("createForumTopic"):
                name = (json or {}).get("name")
                resp = MagicMock()
                resp.json.return_value = {
                    "ok": True,
                    "result": {"message_thread_id": ids[name]},
                }
                return resp
            resp = MagicMock()
            resp.json.return_value = {"ok": True, "result": {"message_id": 99}}
            return resp

        with patch.object(ns.requests, "post", fake_post):
            result = topics_admin.bootstrap(confirm=True)

        assert result.skipped is False
        assert result.minted == {"inbox": 1, "approvals": 2, "runs": 3, "alerts": 4}
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["inbox"] == 1
        assert on_disk["alerts"] == 4

    def test_no_op_when_any_door_exists(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        _telegram_ready(monkeypatch)
        _registry(tmp_path, monkeypatch, {"inbox": 1, "runs": 3})
        calls, fake_post = _recording_create()

        with patch.object(ns.requests, "post", fake_post):
            result = topics_admin.bootstrap(confirm=True)

        assert result.skipped is True
        assert result.existing == {"inbox": 1, "runs": 3}
        assert result.minted == {}
        assert calls == []


class TestWipeSyncRegistry:
    def test_clears_json_and_sqlite_mirror(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _registry(tmp_path, monkeypatch)
        ns._register_topic("inbox", 1)
        ns._register_topic("runs", 3)
        ns._register_topic("developer", 330)
        assert ns._read_topic_mirror()["runs"] == 3

        cleared = ns.wipe_topic_registry()

        assert cleared == {"inbox": 1, "runs": 3, "developer": 330}
        assert json.loads(path.read_text(encoding="utf-8")) == {}
        assert ns._read_topic_mirror() == {}
        assert ns._load_topics() == {}

    def test_dry_run_leaves_both_stores(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        _registry(tmp_path, monkeypatch)
        ns._register_topic("inbox", 1)
        result = topics_admin.wipe_sync(confirm=False)
        assert result.dry_run is True
        assert result.cleared == {"inbox": 1}
        assert ns._load_topics() == {"inbox": 1}
        assert ns._read_topic_mirror()["inbox"] == 1

    def test_after_wipe_bootstrap_can_mint(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Operator wipe leaves leftover ids; wipe-sync then bootstrap remints."""
        _telegram_ready(monkeypatch)
        _registry(tmp_path, monkeypatch)
        ns._register_topic("inbox", 99)
        ns.wipe_topic_registry()

        ids = {"Inbox": 1, "Approvals": 2, "Runs": 3, "Alerts": 4}

        def fake_post(url, json=None, timeout=None):
            if url.endswith("createForumTopic"):
                name = (json or {}).get("name")
                resp = MagicMock()
                resp.json.return_value = {
                    "ok": True,
                    "result": {"message_thread_id": ids[name]},
                }
                return resp
            resp = MagicMock()
            resp.json.return_value = {"ok": True, "result": {"message_id": 99}}
            return resp

        with patch.object(ns.requests, "post", fake_post):
            result = topics_admin.bootstrap(confirm=True)

        assert result.skipped is False
        assert result.minted["inbox"] == 1


class TestInvalidateDropsSqliteMirror:
    def test_invalidated_key_cannot_resurrect_from_mirror(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _registry(tmp_path, monkeypatch)
        ns._register_topic("runs", 208)
        ns._register_topic("inbox", 11)
        assert ns._read_topic_mirror()["runs"] == 208

        dead = ns._invalidate_topic("runs")
        assert dead == 208
        assert "runs" not in ns._read_topic_mirror()

        path.write_text("{}", encoding="utf-8")
        restored = ns._load_topics()
        assert "runs" not in restored
        assert restored.get("inbox") == 11


class TestCreateRegisterLock:
    def test_reread_under_lock_skips_create(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The race: process A registers while B waits on the lock. B must
        reuse A's id instead of calling createForumTopic again."""
        _telegram_ready(monkeypatch)
        _registry(tmp_path, monkeypatch, {})
        calls, fake_post = _recording_create()

        @contextmanager
        def inject_peer_write():
            ns._register_topic("developer", 330)
            yield

        monkeypatch.setattr(ns, "_topic_create_lock", inject_peer_write)

        with patch.object(ns.requests, "post", fake_post):
            thread_id = ns._ensure_topic_thread("developer", "Gustave (Developer)")

        assert thread_id == 330
        assert calls == []

    def test_create_path_takes_the_lock(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        _telegram_ready(monkeypatch)
        _registry(tmp_path, monkeypatch, {})
        lock_entered = {"n": 0}

        @contextmanager
        def counting_lock():
            lock_entered["n"] += 1
            yield

        monkeypatch.setattr(ns, "_topic_create_lock", counting_lock)
        calls, fake_post = _recording_create(42)

        with patch.object(ns.requests, "post", fake_post):
            assert ns._ensure_topic_thread("developer", "Gustave (Developer)") == 42

        assert lock_entered["n"] == 1
        assert any("createForumTopic" in url for url in calls)


class TestStaleDoorNoRemint:
    def test_stale_runs_door_falls_back_to_inbox(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _telegram_ready(monkeypatch)
        monkeypatch.setattr(ns.settings, "telegram_stream_live", True, raising=False)
        monkeypatch.setattr(ns.settings, "telegram_stream_rich", False, raising=False)
        _registry(tmp_path, monkeypatch, {"runs": 208, "inbox": 11})
        calls, fake_post = _recording_create()

        sent: list[dict] = []

        def fake_send(message, chat_id=None, message_thread_id=None, parse_mode=None):
            sent.append({"thread": message_thread_id, "msg": message})
            if message_thread_id == 208:
                raise ns.TelegramSendError(
                    status_code=400,
                    description="Bad Request: message thread not found",
                    chat_id=chat_id,
                    message_thread_id=message_thread_id,
                )

        monkeypatch.setattr(ns, "_send_telegram", fake_send)

        with patch.object(ns.requests, "post", fake_post):
            ns.stream_agent_turn(actor="Gustave (Developer)", summary="deploy finished")

        assert sent[0]["thread"] == 208
        assert sent[1]["thread"] == 11
        assert sent[0]["msg"] == sent[1]["msg"]
        assert calls == []
        assert "runs" not in ns._load_topics()
