"""Tests for the realtime SSE stream (HP-41, Cycle 1 · P1).

Covers the pure frame formatter + the `sse_stream` generator (tenant scoping,
ordering, heartbeat, reconnection cursor) at the unit level, and the
`GET /v1/events/stream` endpoint's auth + wiring at the HTTP level (with the
generator stubbed so the test never consumes an unbounded stream).
"""

from __future__ import annotations

import threading

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import api_service, events
from hivepilot.services.token_service import add_token


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    from hivepilot.config import settings

    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    return TestClient(api_service.app, raise_server_exceptions=True)


def _auth(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


def _parse_frames(chunks: list[str]) -> list[dict]:
    """Turn collected SSE text into structured frames (comments dropped)."""
    import json

    text = "".join(chunks)
    frames = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block or block.startswith(":"):
            continue
        frame: dict = {}
        for line in block.split("\n"):
            field, _, value = line.partition(": ")
            if field == "data":
                frame["data"] = json.loads(value)
            else:
                frame[field] = value
        frames.append(frame)
    return frames


class TestFormatSse:
    def test_frame_shape(self) -> None:
        row = {
            "id": 7,
            "kind": "run.completed",
            "entity_type": "run",
            "entity_id": "42",
            "tenant": "acme",
            "payload": {"status": "success"},
        }
        frame = api_service._format_sse(row)
        assert frame.startswith("id: 7\nevent: run.completed\ndata: ")
        assert frame.endswith("\n\n")
        parsed = _parse_frames([frame])[0]
        assert parsed["id"] == "7"
        assert parsed["event"] == "run.completed"
        assert parsed["data"]["payload"] == {"status": "success"}


class TestSseStream:
    def test_streams_from_zero_in_order(self) -> None:
        ids = [events.emit("run.started", "run", i, payload={"n": i}) for i in range(3)]
        chunks = list(api_service.sse_stream(0, poll_interval=0.01, idle_timeout=0.06))
        assert chunks[0] == ": connected\n\n"
        frames = _parse_frames(chunks)
        assert [int(f["id"]) for f in frames] == ids

    def test_tenant_scoping_hides_other_tenants(self) -> None:
        events.emit("run.started", "run", 1, tenant="acme")
        events.emit("run.started", "run", 2, tenant="other")
        events.emit("run.started", "run", 3, tenant="acme")
        chunks = list(
            api_service.sse_stream(0, tenant_scope="acme", poll_interval=0.01, idle_timeout=0.06)
        )
        frames = _parse_frames(chunks)
        assert [f["data"]["tenant"] for f in frames] == ["acme", "acme"]
        assert [f["data"]["entity_id"] for f in frames] == ["1", "3"]

    def test_admin_scope_sees_all(self) -> None:
        events.emit("run.started", "run", 1, tenant="acme")
        events.emit("run.started", "run", 2, tenant="other")
        chunks = list(
            api_service.sse_stream(0, tenant_scope=None, poll_interval=0.01, idle_timeout=0.06)
        )
        assert len(_parse_frames(chunks)) == 2

    def test_after_id_resumes_without_replay(self) -> None:
        a = events.emit("run.started", "run", 1)
        b = events.emit("run.started", "run", 2)
        chunks = list(api_service.sse_stream(a, poll_interval=0.01, idle_timeout=0.06))
        frames = _parse_frames(chunks)
        assert [int(f["id"]) for f in frames] == [b]

    def test_heartbeat_when_idle(self) -> None:
        chunks = list(
            api_service.sse_stream(
                None, poll_interval=0.01, heartbeat_interval=0.02, idle_timeout=0.08
            )
        )
        assert ": connected\n\n" in chunks
        assert any("keep-alive" in c for c in chunks)

    def test_stop_event_ends_stream(self) -> None:
        events.emit("run.started", "run", 1)
        stop = threading.Event()
        stop.set()
        chunks = list(api_service.sse_stream(0, stop=stop, poll_interval=0.01))
        # Only the preamble — the loop sees stop set and returns immediately.
        assert chunks == [": connected\n\n"]


class TestSseEndpoint:
    def test_requires_auth(self, api_client):
        assert api_client.get("/v1/events/stream").status_code == 401

    def test_admin_gets_full_scope(self, api_client, tmp_tokens_file, monkeypatch):
        calls = []

        def _fake(after_id, *, tenant_scope=None, **kw):
            calls.append({"after_id": after_id, "tenant_scope": tenant_scope})
            yield ": connected\n\n"

        monkeypatch.setattr(api_service, "sse_stream", _fake)
        raw, _ = add_token("admin")
        resp = api_client.get("/v1/events/stream?after=5", headers=_auth(raw))
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert calls == [{"after_id": 5, "tenant_scope": None}]

    def test_non_admin_scoped_to_its_tenant(self, api_client, tmp_tokens_file, monkeypatch):
        calls = []

        def _fake(after_id, *, tenant_scope=None, **kw):
            calls.append({"after_id": after_id, "tenant_scope": tenant_scope})
            yield ": connected\n\n"

        monkeypatch.setattr(api_service, "sse_stream", _fake)
        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/events/stream", headers=_auth(raw))
        assert resp.status_code == 200
        assert calls == [{"after_id": None, "tenant_scope": "acme"}]

    def test_last_event_id_header_sets_resume_point(self, api_client, tmp_tokens_file, monkeypatch):
        calls = []

        def _fake(after_id, *, tenant_scope=None, **kw):
            calls.append({"after_id": after_id, "tenant_scope": tenant_scope})
            yield ": connected\n\n"

        monkeypatch.setattr(api_service, "sse_stream", _fake)
        raw, _ = add_token("admin")
        resp = api_client.get("/v1/events/stream", headers={**_auth(raw), "Last-Event-ID": "12"})
        assert resp.status_code == 200
        assert calls == [{"after_id": 12, "tenant_scope": None}]
