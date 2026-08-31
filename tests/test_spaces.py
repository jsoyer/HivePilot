"""Tests for Espaces — conversation rooms (HP-45, Cycle 1 · P2).

Covers the `state_service` persistence layer (rooms + transcript, incl.
agent<->agent rooms) and the `/v1/spaces` API (auth, tenant scoping, validation,
and realtime event emission on a posted message).

The autouse `_isolate_state_db` fixture (conftest.py) gives each test empty
`spaces`/`space_messages` tables.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import api_service, events, state_service
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


class TestSpacesStore:
    def test_create_and_get_round_trips_participants(self) -> None:
        sid = state_service.create_space(
            [{"type": "human", "id": None}, {"type": "role", "id": "ceo"}],
            kind="dm",
            title="Camille & CEO",
        )
        space = state_service.get_space(sid)
        assert space is not None
        assert space["kind"] == "dm"
        assert space["title"] == "Camille & CEO"
        assert space["participants"] == [
            {"type": "human", "id": None},
            {"type": "role", "id": "ceo"},
        ]

    def test_agent_to_agent_room_is_representable(self) -> None:
        sid = state_service.create_space(
            [{"type": "role", "id": "ceo"}, {"type": "role", "id": "cto"}],
            kind="room",
            title="Le Pont",
        )
        space = state_service.get_space(sid)
        assert space is not None
        assert all(p["type"] == "role" for p in space["participants"])

    def test_messages_append_and_bump_recency(self) -> None:
        a = state_service.create_space([{"type": "role", "id": "ceo"}])
        b = state_service.create_space([{"type": "role", "id": "cto"}])
        # b is newer, so it leads until a gets a message.
        assert [s["id"] for s in state_service.list_spaces()][:2] == [b, a]
        state_service.add_space_message(a, "human", "hello", sender_id=None)
        assert state_service.list_spaces()[0]["id"] == a  # a rose to the top

    def test_list_carries_count_and_last_message(self) -> None:
        sid = state_service.create_space([{"type": "role", "id": "ceo"}])
        state_service.add_space_message(sid, "human", "one")
        state_service.add_space_message(sid, "role", "two", sender_id="ceo")
        row = next(s for s in state_service.list_spaces() if s["id"] == sid)
        assert row["message_count"] == 2
        assert row["last_message_at"] is not None

    def test_list_messages_ordered_and_incremental(self) -> None:
        sid = state_service.create_space([{"type": "role", "id": "ceo"}])
        m1 = state_service.add_space_message(sid, "human", "one")
        m2 = state_service.add_space_message(sid, "role", "two", sender_id="ceo")
        msgs = state_service.list_space_messages(sid)
        assert [m["id"] for m in msgs] == [m1, m2]
        assert [m["sender_type"] for m in msgs] == ["human", "role"]
        assert [m["id"] for m in state_service.list_space_messages(sid, after_id=m1)] == [m2]

    def test_delete_cascades_messages(self) -> None:
        sid = state_service.create_space([{"type": "role", "id": "ceo"}])
        state_service.add_space_message(sid, "human", "hi")
        state_service.delete_space(sid)
        assert state_service.get_space(sid) is None
        assert state_service.list_space_messages(sid) == []

    def test_tenant_isolation(self) -> None:
        acme = state_service.create_space([{"type": "role", "id": "ceo"}], tenant="acme")
        state_service.create_space([{"type": "role", "id": "cto"}], tenant="beta")
        assert [s["id"] for s in state_service.list_spaces("acme")] == [acme]
        assert state_service.get_space(acme, tenant="beta") is None


class TestSpacesApiAuth:
    def test_list_requires_auth(self, api_client):
        assert api_client.get("/v1/spaces").status_code == 401

    def test_create_requires_run(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.post(
            "/v1/spaces", json={"participants": [{"type": "role", "id": "ceo"}]}, headers=_auth(raw)
        )
        assert resp.status_code == 403


class TestSpacesApi:
    def test_create_list_get_and_post_message_with_event(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run", tenant="acme")
        created = api_client.post(
            "/v1/spaces",
            json={
                "participants": [{"type": "human"}, {"type": "role", "id": "ceo"}],
                "kind": "dm",
                "title": "Camille & CEO",
            },
            headers=_auth(raw),
        )
        assert created.status_code == 200
        sid = created.json()["id"]

        listed = api_client.get("/v1/spaces", headers=_auth(raw))
        assert listed.status_code == 200
        assert any(s["id"] == sid for s in listed.json()["spaces"])

        got = api_client.get(f"/v1/spaces/{sid}", headers=_auth(raw))
        assert got.status_code == 200
        assert got.json()["title"] == "Camille & CEO"

        posted = api_client.post(
            f"/v1/spaces/{sid}/messages", json={"body": "on démarre ?"}, headers=_auth(raw)
        )
        assert posted.status_code == 200

        msgs = api_client.get(f"/v1/spaces/{sid}/messages", headers=_auth(raw))
        assert [m["body"] for m in msgs.json()["messages"]] == ["on démarre ?"]

        # the posted message was announced on the realtime bus (HP-40)
        space_events = [e for e in events.read_since(0) if e["kind"] == "space.message"]
        assert len(space_events) == 1
        assert space_events[0]["entity_type"] == "space"
        assert space_events[0]["tenant"] == "acme"

    def test_create_rejects_empty_participants(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        resp = api_client.post("/v1/spaces", json={"participants": []}, headers=_auth(raw))
        assert resp.status_code == 400

    def test_create_rejects_unknown_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        resp = api_client.post(
            "/v1/spaces",
            json={"participants": [{"type": "role", "id": "no_such_role"}]},
            headers=_auth(raw),
        )
        assert resp.status_code == 400

    def test_empty_message_rejected(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        sid = api_client.post(
            "/v1/spaces", json={"participants": [{"type": "role", "id": "ceo"}]}, headers=_auth(raw)
        ).json()["id"]
        resp = api_client.post(
            f"/v1/spaces/{sid}/messages", json={"body": "   "}, headers=_auth(raw)
        )
        assert resp.status_code == 400

    def test_other_tenant_cannot_see_space(self, api_client, tmp_tokens_file):
        raw_acme, _ = add_token("run", tenant="acme")
        sid = api_client.post(
            "/v1/spaces",
            json={"participants": [{"type": "role", "id": "ceo"}]},
            headers=_auth(raw_acme),
        ).json()["id"]

        raw_other, _ = add_token("read", tenant="other")
        assert api_client.get(f"/v1/spaces/{sid}", headers=_auth(raw_other)).status_code == 404
        assert api_client.get("/v1/spaces", headers=_auth(raw_other)).json()["spaces"] == []

    def test_delete_requires_admin(self, api_client, tmp_tokens_file):
        raw_run, _ = add_token("run")
        sid = api_client.post(
            "/v1/spaces",
            json={"participants": [{"type": "role", "id": "ceo"}]},
            headers=_auth(raw_run),
        ).json()["id"]
        assert api_client.delete(f"/v1/spaces/{sid}", headers=_auth(raw_run)).status_code == 403

        raw_admin, _ = add_token("admin")
        assert api_client.delete(f"/v1/spaces/{sid}", headers=_auth(raw_admin)).status_code == 200
