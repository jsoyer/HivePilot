"""Tests for `POST /v1/concierge` (+ unversioned twin) — the Pollen
natural-language chat surface (HP-22).

The endpoint is a thin, fail-closed wrapper over
`concierge_service.route`, so these tests mock `route` and assert the
request/response contract, auth floor, and that a proposed route is surfaced
(not executed). Mirrors the auth/fixture patterns in
`tests/test_whoami_endpoint.py`.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import concierge_service
from hivepilot.services.concierge_service import ConciergeDecision, DispatchOrder
from hivepilot.services.token_service import add_token


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _auth(raw_token: str) -> dict:
    return {"Authorization": f"Bearer {raw_token}"}


class TestConciergeEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.post("/v1/concierge", json={"text": "hi"})
        assert resp.status_code == 401

    def test_empty_text_is_400(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.post("/v1/concierge", json={"text": "   "}, headers=_auth(raw))
        assert resp.status_code == 400

    def test_answer_is_returned(self, api_client, tmp_tokens_file, monkeypatch):
        monkeypatch.setattr(
            concierge_service,
            "route",
            lambda text, **kw: ConciergeDecision(kind="answer", answer_text="Run 8 succeeded."),
        )
        raw, _ = add_token("read")
        resp = api_client.post(
            "/v1/concierge",
            json={"text": "how did the last run go?", "conversation_id": "web-1"},
            headers=_auth(raw),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "answer"
        assert body["answer_text"] == "Run 8 succeeded."
        assert body["destructive"] is False

    def test_route_proposal_is_surfaced_not_executed(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        """A `route` decision comes back with its role/target/order and the
        destructive flag — the endpoint returns a PROPOSAL, it never dispatches."""
        monkeypatch.setattr(
            concierge_service,
            "route",
            lambda text, **kw: ConciergeDecision(
                kind="route",
                role_key="developer",
                target="example-api",
                order="add a healthcheck",
                destructive=True,
            ),
        )
        raw, _ = add_token("read")
        resp = api_client.post(
            "/v1/concierge",
            json={"text": "ask the dev to add a healthcheck"},
            headers=_auth(raw),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "route"
        assert body["role_key"] == "developer"
        assert body["target"] == "example-api"
        assert body["order"] == "add a healthcheck"
        assert body["destructive"] is True

    def test_multi_route_dispatches_serialized(self, api_client, tmp_tokens_file, monkeypatch):
        monkeypatch.setattr(
            concierge_service,
            "route",
            lambda text, **kw: ConciergeDecision(
                kind="multi_route",
                dispatches=[
                    DispatchOrder(role_key="developer", target="example-api", order="do X"),
                    DispatchOrder(role_key="qa", target="example-api", order="test X"),
                ],
                destructive=True,
            ),
        )
        raw, _ = add_token("read")
        resp = api_client.post("/v1/concierge", json={"text": "dispatch dev and qa"}, headers=_auth(raw))
        assert resp.status_code == 200
        dispatches = resp.json()["dispatches"]
        assert [d["role_key"] for d in dispatches] == ["developer", "qa"]

    def test_owner_is_the_caller_token(self, api_client, tmp_tokens_file, monkeypatch):
        """The pending-offer owner passed to the concierge is the caller's
        token hash — so a bare 'yes' only resolves for the asker, mirroring
        Telegram's (chat_id, user_id) pairing."""
        seen: dict = {}

        def _capture(text, **kw):
            seen.update(kw)
            return ConciergeDecision(kind="answer", answer_text="ok")

        monkeypatch.setattr(concierge_service, "route", _capture)
        raw, entry = add_token("read")
        resp = api_client.post(
            "/v1/concierge",
            json={"text": "yes", "conversation_id": "web-42"},
            headers=_auth(raw),
        )
        assert resp.status_code == 200
        assert seen["user_id"] == entry.token
        assert seen["conversation_id"] == "web-42"
        assert seen["chat_id"] is not None

    def test_unversioned_route_also_registered(self, api_client, tmp_tokens_file, monkeypatch):
        monkeypatch.setattr(
            concierge_service,
            "route",
            lambda text, **kw: ConciergeDecision(kind="answer", answer_text="hi"),
        )
        raw, _ = add_token("read")
        resp = api_client.post("/concierge", json={"text": "hello"}, headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["answer_text"] == "hi"
