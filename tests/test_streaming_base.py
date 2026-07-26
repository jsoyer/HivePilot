"""Tests for hivepilot.streaming.base: the channel-agnostic StreamChannel
protocol, ThreadRef, StreamChannelRegistry (mirrors NotifierRegistry) and the
shared `split_for` chunk splitter (generalized from
notification_service._split_for_telegram).
"""

from __future__ import annotations

import re

import pytest

from hivepilot.streaming.base import (
    STREAM_CHANNEL_MAP,
    StreamChannelKindCollisionError,
    StreamChannelRegistry,
    ThreadRef,
    split_for,
)


@pytest.fixture(autouse=True)
def _restore_stream_channel_map():
    """STREAM_CHANNEL_MAP is process-global mutable state -- snapshot/restore
    around every test so channel names registered here never leak into other
    test modules sharing the pytest session (mirrors
    test_notifier_registry.py's `_restore_notifier_map`)."""
    snapshot = dict(STREAM_CHANNEL_MAP)
    yield
    STREAM_CHANNEL_MAP.clear()
    STREAM_CHANNEL_MAP.update(snapshot)


class _FakeChannel:
    name = "fake"
    max_len = 100

    def enabled(self) -> bool:
        return True

    def ensure_agent_thread(self, agent_key: str, title: str) -> ThreadRef | None:
        return ThreadRef(channel=self.name, container="c1", thread_id="t1")

    def send(self, thread, text, *, rich: bool = False) -> None:
        pass

    def format(self, markdown: str) -> str:
        return markdown


class TestThreadRef:
    def test_is_frozen(self) -> None:
        ref = ThreadRef(channel="slack", container="C123", thread_id="1707.001")
        with pytest.raises(Exception):
            ref.container = "other"  # type: ignore[misc]

    def test_thread_id_defaults_to_none(self) -> None:
        ref = ThreadRef(channel="discord", container=123)
        assert ref.thread_id is None


class TestStreamChannelRegistry:
    def test_register_and_known_names(self) -> None:
        StreamChannelRegistry.register("fake", _FakeChannel())
        assert "fake" in StreamChannelRegistry.known_names()
        assert STREAM_CHANNEL_MAP["fake"].name == "fake"

    def test_collision_without_override_raises(self) -> None:
        StreamChannelRegistry.register("fake", _FakeChannel())
        with pytest.raises(StreamChannelKindCollisionError):
            StreamChannelRegistry.register("fake", _FakeChannel())

    def test_reregistering_same_instance_does_not_raise(self) -> None:
        chan = _FakeChannel()
        StreamChannelRegistry.register("fake", chan)
        StreamChannelRegistry.register("fake", chan)  # same object -> no-op
        assert STREAM_CHANNEL_MAP["fake"] is chan

    def test_override_replaces_without_raising(self) -> None:
        StreamChannelRegistry.register("fake", _FakeChannel())
        replacement = _FakeChannel()
        StreamChannelRegistry.register("fake", replacement, override=True)
        assert STREAM_CHANNEL_MAP["fake"] is replacement


class TestSplitForPlain:
    def test_short_text_returned_unchanged(self) -> None:
        assert split_for("hello", 100) == ["hello"]

    def test_splits_on_paragraph_boundary(self) -> None:
        text = ("a" * 40) + "\n\n" + ("b" * 40)
        chunks = split_for(text, 50, entity_aware=False)
        assert len(chunks) == 2
        assert chunks[0].startswith("a" * 40)
        assert chunks[1].startswith("b" * 40)

    def test_never_truncates_content(self) -> None:
        text = "word " * 500
        chunks = split_for(text, 100, max_chunks=50, entity_aware=False)
        joined = "".join(re.sub(r"\n\n\(\d+/\d+\)$", "", c) for c in chunks)
        assert "word" * 1 in joined
        assert len(chunks) > 1

    def test_respects_max_chunks_cap(self) -> None:
        text = "x" * 10000
        chunks = split_for(text, 10, max_chunks=3, entity_aware=False)
        assert len(chunks) == 3
        assert "truncated" in chunks[-1]

    def test_continuation_marker_added_when_split(self) -> None:
        text = "word " * 500
        chunks = split_for(text, 100, max_chunks=50, entity_aware=False)
        assert "(1/" in chunks[0]


class TestSplitForEntityAware:
    def test_balances_tags_across_chunk_boundary(self) -> None:
        html_text = "<b>" + ("x" * 300) + "</b>"
        chunks = split_for(html_text, 100, max_chunks=50, entity_aware=True)
        assert len(chunks) > 1
        for chunk in chunks:
            # each chunk-fragment (minus the trailing "(i/N)" marker) must be
            # independently well-formed: every opened <b> is closed.
            body = re.sub(r"\n\n\(\d+/\d+\)$", "", chunk)
            assert body.count("<b>") == body.count("</b>")


def test_stream_channel_map_has_builtin_names_after_import() -> None:
    """Importing hivepilot.streaming registers telegram/slack/discord."""
    import hivepilot.streaming  # noqa: F401

    for name in ("telegram", "slack", "discord"):
        assert name in STREAM_CHANNEL_MAP
