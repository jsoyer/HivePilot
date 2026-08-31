"""Tests for the Espaces async reply loop — dépose/relève (HP-46, P2).

Covers the background responder (typing battements + reply posting + events,
fail-safe per role) and the end-to-end deposit loop through the API (a human
message triggers a background agent reply; a role message never does).
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import (
    api_service,
    async_run_service,
    events,
    spaces_responder,
    state_service,
)
from hivepilot.services.token_service import add_token


@pytest.fixture(autouse=True)
def _clear_generator():
    spaces_responder.register_reply_generator(None)
    yield
    spaces_responder.register_reply_generator(None)


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


class TestResponder:
    def test_posts_a_reply_and_emits_typing_and_message(self) -> None:
        sid = state_service.create_space([{"type": "role", "id": "ceo"}])
        state_service.add_space_message(sid, "human", "on démarre ?")
        seen = {}
        spaces_responder.register_reply_generator(
            lambda space, role, thread: (
                seen.update(role=role, count=len(thread)) or f"{role}: c'est parti"
            )
        )

        spaces_responder.respond_in_space(sid)

        msgs = state_service.list_space_messages(sid)
        assert [m["sender_type"] for m in msgs] == ["human", "role"]
        assert msgs[-1]["sender_id"] == "ceo"
        assert msgs[-1]["body"] == "ceo: c'est parti"
        assert seen == {"role": "ceo", "count": 1}  # generator saw the human msg

        kinds = [e["kind"] for e in events.read_since(0)]
        assert "space.typing" in kinds
        assert "space.message" in kinds
        assert "space.typing_stop" in kinds

    def test_no_generator_is_a_graceful_noop(self) -> None:
        sid = state_service.create_space([{"type": "role", "id": "ceo"}])
        state_service.add_space_message(sid, "human", "hi")
        spaces_responder.respond_in_space(sid)  # no generator registered
        # only the human message; typing battements still emitted
        assert [m["sender_type"] for m in state_service.list_space_messages(sid)] == ["human"]
        assert any(e["kind"] == "space.typing" for e in events.read_since(0))

    def test_one_reply_per_role(self) -> None:
        sid = state_service.create_space(
            [{"type": "role", "id": "ceo"}, {"type": "role", "id": "cto"}]
        )
        spaces_responder.register_reply_generator(lambda space, role, thread: f"hi from {role}")
        spaces_responder.respond_in_space(sid)
        roles = [
            m["sender_id"]
            for m in state_service.list_space_messages(sid)
            if m["sender_type"] == "role"
        ]
        assert sorted(roles) == ["ceo", "cto"]

    def test_generator_failure_is_isolated(self) -> None:
        sid = state_service.create_space(
            [{"type": "role", "id": "ceo"}, {"type": "role", "id": "cto"}]
        )

        def _gen(space, role, thread):
            if role == "ceo":
                raise RuntimeError("boom")
            return "cto ok"

        spaces_responder.register_reply_generator(_gen)
        spaces_responder.respond_in_space(sid)  # must not raise

        roles = [
            m["sender_id"]
            for m in state_service.list_space_messages(sid)
            if m["sender_type"] == "role"
        ]
        assert roles == ["cto"]  # ceo failed, cto still replied

    def test_skips_empty_reply(self) -> None:
        sid = state_service.create_space([{"type": "role", "id": "ceo"}])
        spaces_responder.register_reply_generator(lambda space, role, thread: "   ")
        spaces_responder.respond_in_space(sid)
        assert state_service.list_space_messages(sid) == []


class TestDeposeReleveEndToEnd:
    def test_human_message_triggers_background_reply(self, api_client, tmp_tokens_file):
        spaces_responder.register_reply_generator(
            lambda space, role, thread: f"{role} relève et répond"
        )
        raw, _ = add_token("run")
        sid = api_client.post(
            "/v1/spaces",
            json={"participants": [{"type": "role", "id": "ceo"}]},
            headers=_auth(raw),
        ).json()["id"]

        posted = api_client.post(
            f"/v1/spaces/{sid}/messages", json={"body": "salut"}, headers=_auth(raw)
        )
        assert posted.status_code == 200  # returns immediately (dépose)

        assert async_run_service.wait_until_idle(5.0)  # drain the background relève

        bodies = [
            m["body"]
            for m in api_client.get(f"/v1/spaces/{sid}/messages", headers=_auth(raw)).json()[
                "messages"
            ]
        ]
        assert bodies == ["salut", "ceo relève et répond"]

    def test_role_message_does_not_trigger_a_reply(self, api_client, tmp_tokens_file):
        calls = []
        spaces_responder.register_reply_generator(
            lambda space, role, thread: calls.append(role) or "should not happen"
        )
        raw, _ = add_token("run")
        sid = api_client.post(
            "/v1/spaces",
            json={"participants": [{"type": "role", "id": "ceo"}]},
            headers=_auth(raw),
        ).json()["id"]

        # A role-authored message must NOT trigger the reply loop (no recursion).
        api_client.post(
            f"/v1/spaces/{sid}/messages",
            json={"body": "agent line", "sender_type": "role", "sender_id": "ceo"},
            headers=_auth(raw),
        )
        assert async_run_service.wait_until_idle(2.0)
        assert calls == []

    def test_auto_reply_off_disables_the_loop(self, api_client, tmp_tokens_file, monkeypatch):
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "spaces_auto_reply", False)
        spaces_responder.register_reply_generator(lambda space, role, thread: "nope")
        raw, _ = add_token("run")
        sid = api_client.post(
            "/v1/spaces",
            json={"participants": [{"type": "role", "id": "ceo"}]},
            headers=_auth(raw),
        ).json()["id"]
        api_client.post(f"/v1/spaces/{sid}/messages", json={"body": "salut"}, headers=_auth(raw))
        assert async_run_service.wait_until_idle(2.0)
        bodies = [
            m["body"]
            for m in api_client.get(f"/v1/spaces/{sid}/messages", headers=_auth(raw)).json()[
                "messages"
            ]
        ]
        assert bodies == ["salut"]
