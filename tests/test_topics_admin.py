"""Tests for `hivepilot topics list` / `topics prune`.

Why the tool takes explicit ids instead of discovering them: the Telegram Bot
API has **no endpoint that lists a forum's topics**, and no safe existence
probe either. `deleteForumTopic` destroys, `editForumTopic` and
`closeForumTopic` mutate, and `unpinAllForumTopicMessages` would drop the
operator's pins. A tool that guessed an id and guessed right would destroy a
live topic.

So the design is: the operator supplies the ids, and the tool's job is to
refuse the dangerous ones.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "stream_topics.json"
    path.write_text(json.dumps({"developer": 330, "ciso": 331, "qa": 332}))
    from hivepilot.services import notification_service

    monkeypatch.setattr(notification_service, "_topics_registry_path", lambda: path)
    return path


class TestPruneRefusesTheDangerousOnes:
    def test_never_deletes_an_id_the_registry_points_at(self, registry):
        """The core invariant. A live topic is one the registry still uses."""
        from hivepilot.services import topics_admin

        plan = topics_admin.plan_prune([330, 999])

        assert plan.protected == [330]
        assert plan.deletable == [999]

    def test_dry_run_by_default(self, registry):
        """Nothing is destroyed until someone says so in as many words."""
        from hivepilot.services import topics_admin

        deleted: list[int] = []
        result = topics_admin.prune([999], confirm=False, delete=deleted.append)

        assert deleted == []
        assert result.deleted == []
        assert result.would_delete == [999]

    def test_confirm_actually_deletes(self, registry):
        from hivepilot.services import topics_admin

        deleted: list[int] = []
        result = topics_admin.prune([999], confirm=True, delete=deleted.append)

        assert deleted == [999]
        assert result.deleted == [999]

    def test_a_protected_id_is_never_deleted_even_with_confirm(self, registry):
        from hivepilot.services import topics_admin

        deleted: list[int] = []
        result = topics_admin.prune([330, 999], confirm=True, delete=deleted.append)

        assert 330 not in deleted
        assert result.protected == [330]

    def test_a_failed_delete_is_reported_not_swallowed(self, registry):
        """A topic that could not be removed must not read as removed."""
        from hivepilot.services import topics_admin

        def boom(_id: int) -> None:
            raise RuntimeError("topic not found")

        result = topics_admin.prune([999], confirm=True, delete=boom)

        assert result.deleted == []
        assert result.failed and result.failed[0][0] == 999

    def test_duplicate_ids_are_collapsed(self, registry):
        from hivepilot.services import topics_admin

        deleted: list[int] = []
        topics_admin.prune([999, 999], confirm=True, delete=deleted.append)

        assert deleted == [999]


class TestListShowsWhatIsProtected:
    def test_lists_the_registry(self, registry):
        """The operator compares this against what Telegram shows; anything in
        the group and not in this list is a candidate."""
        from hivepilot.services import topics_admin

        entries = topics_admin.list_topics()

        assert {"developer": 330, "ciso": 331, "qa": 332} == entries
