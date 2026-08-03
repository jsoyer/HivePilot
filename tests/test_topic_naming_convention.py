"""Regression tests for forum-topic naming and duplication.

Production incident: three topics existed for one agent — the correct
`Gustave (Developer)` plus TWO bare `Gustave` topics — and a separate bare
lowercase `pentest` topic. Two distinct defects produced names that break the
"Firstname (Role)" convention, and a third let a registered topic be forgotten
so it got recreated:

1. `_resolve_agent_key` minted a slug from the actor's first word whenever
   ROLES failed to match. Roles are reloaded continuously, so during that
   window a real role resolved to a SECOND key (`gustave`, not `developer`).
2. The topic title came from the raw actor string (or, on the self-heal path,
   from the registry key itself) rather than from the role.
3. `_save_topics` wrote a stale snapshot wholesale, dropping entries another
   process had registered in between.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hivepilot.services import notification_service as ns


@pytest.fixture
def fake_roles(monkeypatch):
    """A deterministic roster — the real one depends on deployment config."""
    roles = {
        "developer": SimpleNamespace(display_name="Gustave", title="Developer"),
        "ciso": SimpleNamespace(display_name="Hugo", title="CISO"),
    }
    import hivepilot.roles

    monkeypatch.setattr(hivepilot.roles, "ROLES", roles)
    return roles


@pytest.fixture
def empty_roles(monkeypatch):
    """The mid-reload window that created the duplicate topics."""
    import hivepilot.roles

    monkeypatch.setattr(hivepilot.roles, "ROLES", {})


# ---------------------------------------------------------------------------
# _resolve_agent_key — never mint a key while the roster is unavailable
# ---------------------------------------------------------------------------


def test_a_real_role_resolves_to_its_role_key(fake_roles):
    assert ns._resolve_agent_key("Gustave (Developer)") == "developer"
    assert ns._resolve_agent_key("Gustave") == "developer"


def test_no_key_is_invented_while_roles_are_reloading(empty_roles):
    """The exact window that produced a second, wrongly-named Gustave topic."""
    assert ns._resolve_agent_key("Gustave") is None
    assert ns._resolve_agent_key("Gustave (Developer)") is None


def test_non_role_streams_still_get_their_own_key(fake_roles):
    """`hivepilot` / `pentest` legitimately stream to their own topic."""
    assert ns._resolve_agent_key("pentest") == "pentest"


# ---------------------------------------------------------------------------
# _canonical_topic_title — the convention cannot drift between call sites
# ---------------------------------------------------------------------------


def test_title_is_derived_from_the_role_not_from_the_caller(fake_roles):
    """Whatever the call site passed, the topic is named "Firstname (Role)"."""
    assert ns._canonical_topic_title("developer", "Gustave") == "Gustave (Developer)"
    assert ns._canonical_topic_title("developer", None) == "Gustave (Developer)"
    assert ns._canonical_topic_title("developer", "something else") == "Gustave (Developer)"


def test_a_non_role_key_keeps_its_supplied_title(fake_roles):
    assert ns._canonical_topic_title("pentest", "Pentest") == "Pentest"


def test_a_non_role_key_without_a_title_degrades_to_the_key(fake_roles):
    """Documents the `pentest` topic's origin — still the last resort."""
    assert ns._canonical_topic_title("pentest", None) == "pentest"


# ---------------------------------------------------------------------------
# _save_topics — a concurrent write must not drop a registered topic
# ---------------------------------------------------------------------------


def test_registering_a_topic_preserves_a_concurrent_registration(tmp_path, monkeypatch):
    registry = tmp_path / "stream_topics.json"
    monkeypatch.setattr(ns, "_topics_registry_path", lambda: registry)

    # Another process registered `ciso` after our snapshot was taken.
    registry.write_text(json.dumps({"developer": 330, "ciso": 284}), encoding="utf-8")

    ns._register_topic("pentest", 363)

    on_disk = json.loads(registry.read_text(encoding="utf-8"))
    assert on_disk["pentest"] == 363, "our new entry must land"
    assert on_disk["ciso"] == 284, (
        "a concurrently-registered topic must survive — losing it makes the "
        "next call create a duplicate"
    )
    assert on_disk["developer"] == 330


def test_saving_stays_authoritative_so_a_dead_topic_can_be_removed(tmp_path, monkeypatch):
    """Guard the fix from over-reaching: invalidation MUST still delete.

    Merging on every save would resurrect a topic that was deleted on the
    Telegram side, and the dead thread id would be reused forever.
    """
    registry = tmp_path / "stream_topics.json"
    monkeypatch.setattr(ns, "_topics_registry_path", lambda: registry)
    registry.write_text(json.dumps({"developer": 330, "ciso": 284}), encoding="utf-8")

    ns._save_topics({"developer": 330})  # ciso invalidated

    assert json.loads(registry.read_text(encoding="utf-8")) == {"developer": 330}


class TestRegistrySurvivesLosingItsFile:
    """Telegram cannot be asked whether a topic name already exists.

    The Bot API has no method to list a forum's topics, so the local registry
    is the ONLY guard against creating a duplicate. Losing the file would
    therefore give every agent a second topic with the same name — accepted
    silently by Telegram, and undetectable afterwards by any API call.
    """

    def test_an_emptied_file_is_rebuilt_from_the_mirror(self, tmp_path, monkeypatch):
        registry = tmp_path / "stream_topics.json"
        monkeypatch.setattr(ns, "_topics_registry_path", lambda: registry)

        ns._register_topic("developer", 330)
        ns._register_topic("ciso", 284)
        registry.write_text("{}", encoding="utf-8")  # file lost / reset

        assert ns._load_topics() == {"developer": 330, "ciso": 284}

    def test_the_rebuilt_registry_is_written_back(self, tmp_path, monkeypatch):
        registry = tmp_path / "stream_topics.json"
        monkeypatch.setattr(ns, "_topics_registry_path", lambda: registry)

        ns._register_topic("developer", 330)
        registry.unlink()

        ns._load_topics()
        assert json.loads(registry.read_text(encoding="utf-8")) == {"developer": 330}

    def test_a_genuinely_fresh_install_still_starts_empty(self, tmp_path, monkeypatch):
        """Restoration must not invent history where there is none."""
        registry = tmp_path / "stream_topics.json"
        monkeypatch.setattr(ns, "_topics_registry_path", lambda: registry)

        assert ns._load_topics() == {}

    def test_the_file_wins_while_it_has_content(self, tmp_path, monkeypatch):
        """The file is the hot path; the mirror is only a fallback."""
        registry = tmp_path / "stream_topics.json"
        monkeypatch.setattr(ns, "_topics_registry_path", lambda: registry)

        ns._register_topic("developer", 330)
        registry.write_text(json.dumps({"developer": 999}), encoding="utf-8")

        assert ns._load_topics() == {"developer": 999}

    def test_topics_registered_before_the_mirror_existed_are_backfilled(
        self, tmp_path, monkeypatch
    ):
        """Otherwise the restore path could only ever restore nothing.

        A deployment whose topics predate the mirror would carry something
        that looks like protection and restores an empty registry — worse
        than having no restore path at all.
        """
        registry = tmp_path / "stream_topics.json"
        monkeypatch.setattr(ns, "_topics_registry_path", lambda: registry)
        monkeypatch.setattr(ns, "_mirror_reconciled", False)
        registry.write_text(json.dumps({"developer": 330, "ciso": 284}), encoding="utf-8")

        ns._load_topics()

        assert ns._read_topic_mirror() == {"developer": 330, "ciso": 284}

    def test_reconciliation_happens_once_per_process(self, tmp_path, monkeypatch):
        """The registry is read on every send; a DB round-trip per send would
        be pure overhead for a mapping that changes a handful of times."""
        registry = tmp_path / "stream_topics.json"
        monkeypatch.setattr(ns, "_topics_registry_path", lambda: registry)
        monkeypatch.setattr(ns, "_mirror_reconciled", False)
        registry.write_text(json.dumps({"developer": 330}), encoding="utf-8")

        calls: list[int] = []
        real = ns._read_topic_mirror
        monkeypatch.setattr(ns, "_read_topic_mirror", lambda: (calls.append(1), real())[1])

        ns._load_topics()
        ns._load_topics()
        ns._load_topics()

        assert len(calls) == 1
