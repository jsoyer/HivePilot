"""HP-130e cutover / cleanup guards.

Cutover is operator-gated: wipe the local topic registry only when
multi-token door bots are active. Never delete Telegram topics on boot,
never mint ``run:{id}`` topics, never require BotFather tokens in-repo.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from hivepilot.cli import app
from hivepilot.services import notification_service as ns
from hivepilot.services import telegram_bot, topics_admin
from hivepilot.services.telegram_doors import telegram_multi_token_mode


def _registry(tmp_path, monkeypatch: pytest.MonkeyPatch, mapping: dict[str, int] | None = None):
    path = tmp_path / "stream_topics.json"
    if mapping is not None:
        path.write_text(json.dumps(mapping), encoding="utf-8")
    monkeypatch.setattr(ns, "_topics_registry_path", lambda: path)
    monkeypatch.setattr(ns, "_mirror_reconciled", False)
    return path


def _legacy_doors(tmp_path, monkeypatch: pytest.MonkeyPatch):
    mapping = {
        "inbox": 2118,
        "approvals": 2119,
        "runs": 2120,
        "alerts": 2121,
    }
    return _registry(tmp_path, monkeypatch, mapping), mapping


class TestCutoverWipeGuards:
    def test_refuses_when_legacy_stream_topics_still_owns_registry(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path, mapping = _legacy_doors(tmp_path, monkeypatch)
        monkeypatch.setattr(topics_admin, "telegram_multi_token_mode", lambda: False)
        ns._register_topic("inbox", 2118)

        result = topics_admin.cutover_wipe(confirm=True)

        assert result.refused is True
        assert result.multi_token is False
        assert "STREAM_TOPICS" in (result.reason or "")
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["inbox"] == 2118
        assert on_disk["alerts"] == 2121

    def test_dry_run_in_multi_token_leaves_json_and_sqlite(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _registry(tmp_path, monkeypatch)
        ns._register_topic("inbox", 2118)
        ns._register_topic("runs", 2120)
        monkeypatch.setattr(topics_admin, "telegram_multi_token_mode", lambda: True)

        result = topics_admin.cutover_wipe(confirm=False)

        assert result.dry_run is True
        assert result.refused is False
        assert result.wiped["inbox"] == 2118
        assert ns._load_topics()["runs"] == 2120
        assert ns._read_topic_mirror()["inbox"] == 2118

    def test_confirm_in_multi_token_clears_json_and_sqlite(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path, _mapping = _legacy_doors(tmp_path, monkeypatch)
        ns._register_topic("inbox", 2118)
        ns._register_topic("approvals", 2119)
        ns._register_topic("runs", 2120)
        ns._register_topic("alerts", 2121)
        monkeypatch.setattr(topics_admin, "telegram_multi_token_mode", lambda: True)

        result = topics_admin.cutover_wipe(confirm=True)

        assert result.refused is False
        assert result.dry_run is False
        assert result.wiped["inbox"] == 2118
        assert json.loads(path.read_text(encoding="utf-8")) == {}
        assert ns._read_topic_mirror() == {}
        assert ns._load_topics() == {}

    def test_cutover_never_calls_telegram(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        _legacy_doors(tmp_path, monkeypatch)
        monkeypatch.setattr(topics_admin, "telegram_multi_token_mode", lambda: True)
        posts: list[str] = []

        def boom(url, json=None, timeout=None):
            posts.append(url)
            raise AssertionError(f"Telegram must not be called: {url}")

        with patch("requests.post", boom):
            topics_admin.cutover_plan()
            topics_admin.cutover_wipe(confirm=False)
            topics_admin.cutover_wipe(confirm=True)

        assert posts == []


class TestBootstrapSkippedInMultiToken:
    def test_empty_registry_does_not_mint(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        _registry(tmp_path, monkeypatch, {})
        monkeypatch.setattr(ns.settings, "telegram_bot_token", "tok", raising=False)
        monkeypatch.setattr(ns.settings, "telegram_stream_chat_id", -100999, raising=False)
        monkeypatch.setattr(ns.settings, "telegram_stream_topics", True, raising=False)
        monkeypatch.setattr(topics_admin, "telegram_multi_token_mode", lambda: True)
        monkeypatch.setattr(ns, "telegram_multi_token_mode", lambda *a, **k: True)
        calls: list[str] = []

        def fake_post(url, json=None, timeout=None):
            calls.append(url)
            resp = MagicMock()
            resp.json.return_value = {"ok": True, "result": {"message_thread_id": 1}}
            return resp

        with patch.object(ns.requests, "post", fake_post):
            result = topics_admin.bootstrap(confirm=True)

        assert result.skipped is True
        assert result.skipped_multi_token is True
        assert result.minted == {}
        assert calls == []
        assert "createForumTopic" not in "".join(calls)


class TestPruneAfterCutoverWipe:
    def test_legacy_door_ids_protected_until_wipe(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _legacy_doors(tmp_path, monkeypatch)
        deleted: list[int] = []

        result = topics_admin.prune(
            list(topics_admin.LEGACY_FORUM_DOOR_THREAD_IDS), confirm=True, delete=deleted.append
        )

        assert deleted == []
        assert set(result.protected) == set(topics_admin.LEGACY_FORUM_DOOR_THREAD_IDS)

    def test_legacy_door_ids_deletable_after_wipe(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _legacy_doors(tmp_path, monkeypatch)
        monkeypatch.setattr(topics_admin, "telegram_multi_token_mode", lambda: True)
        topics_admin.cutover_wipe(confirm=True)
        deleted: list[int] = []

        result = topics_admin.prune(
            list(topics_admin.LEGACY_FORUM_DOOR_THREAD_IDS),
            confirm=True,
            delete=deleted.append,
        )

        assert deleted == list(topics_admin.LEGACY_FORUM_DOOR_THREAD_IDS)
        assert result.protected == []
        assert result.deleted == list(topics_admin.LEGACY_FORUM_DOOR_THREAD_IDS)


class TestStartupNeverWipesOrDeletes:
    def test_build_application_has_no_cutover_side_effects(self) -> None:
        src = inspect.getsource(telegram_bot._build_application)
        assert "wipe_topic_registry" not in src
        assert "wipe_sync" not in src
        assert "cutover_wipe" not in src
        assert "deleteForumTopic" not in src
        assert "createForumTopic" not in src

    def test_run_polling_has_no_cutover_side_effects(self) -> None:
        src = inspect.getsource(telegram_bot.run_polling)
        assert "wipe_topic_registry" not in src
        assert "cutover_wipe" not in src
        assert "deleteForumTopic" not in src


class TestCutoverCli:
    def test_yes_refused_on_legacy_path(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        _legacy_doors(tmp_path, monkeypatch)
        monkeypatch.setattr(topics_admin, "telegram_multi_token_mode", lambda: False)
        runner = CliRunner()

        result = runner.invoke(app, ["topics", "cutover", "--yes"])

        assert result.exit_code == 1
        assert "REFUSED" in result.output
        assert ns._load_topics()["inbox"] == 2118

    def test_yes_wipes_in_multi_token(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        _legacy_doors(tmp_path, monkeypatch)
        monkeypatch.setattr(topics_admin, "telegram_multi_token_mode", lambda: True)
        runner = CliRunner()

        result = runner.invoke(app, ["topics", "cutover", "--yes"])

        assert result.exit_code == 0, result.output
        assert "cleared" in result.output
        assert "2118" in result.output
        assert "Do not run `topics bootstrap`" in result.output
        assert ns._load_topics() == {}

    def test_bootstrap_cli_refuses_in_multi_token(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _registry(tmp_path, monkeypatch, {})
        monkeypatch.setattr(topics_admin, "telegram_multi_token_mode", lambda: True)
        runner = CliRunner()

        result = runner.invoke(app, ["topics", "bootstrap", "--yes"])

        assert result.exit_code == 0, result.output
        assert "Refusing to mint" in result.output
        assert "topics cutover" in result.output
        assert topics_admin.list_topics() == {}

    def test_wipe_sync_followup_skips_bootstrap_in_multi_token(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _legacy_doors(tmp_path, monkeypatch)
        monkeypatch.setattr(topics_admin, "telegram_multi_token_mode", lambda: True)
        runner = CliRunner()

        result = runner.invoke(app, ["topics", "wipe-sync", "--yes"])

        assert result.exit_code == 0, result.output
        assert "Do not run `topics bootstrap`" in result.output
        assert "Mint doors with" not in result.output


def test_legacy_forum_door_ids_are_the_documented_orphans() -> None:
    assert topics_admin.LEGACY_FORUM_DOOR_THREAD_IDS == (2118, 2119, 2120, 2121)


def test_multi_token_mode_still_requires_two_distinct_tokens() -> None:
    """Cutover must not trip on a single shared token."""
    from hivepilot.config import Settings

    shared = Settings(_env_file=None, telegram_bot_token="shared-token")  # type: ignore[call-arg]
    assert telegram_multi_token_mode(shared) is False
