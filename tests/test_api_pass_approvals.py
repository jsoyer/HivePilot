"""HP-102: Pollen pass-approvals API shares decide_approval() with Telegram."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hivepilot.pass_store import APPROVED, create_pending
from hivepilot.presenters import reset_pending
from hivepilot.services.token_service import add_token


@pytest.fixture(autouse=True)
def _reset_presenter() -> None:
    reset_pending()
    yield
    reset_pending()


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    import yaml

    from hivepilot.config import settings

    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _auth(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


def test_list_pass_approvals_requires_auth(api_client):
    assert api_client.get("/v1/pass-approvals").status_code == 401


def test_list_and_decide_share_approval_id(api_client, tmp_tokens_file):
    raw, _ = add_token("approve", note="jerome")
    proposal = create_pending(
        kind="partition",
        project="example-api",
        task="docs",
        payload={"path": "notes.md"},
    )
    listed = api_client.get("/v1/pass-approvals", headers=_auth(raw))
    assert listed.status_code == 200
    cards = listed.json()
    assert len(cards) == 1
    assert cards[0]["approval_id"] == proposal.id
    assert cards[0]["door"] == "approvals"
    assert cards[0]["surface"] == "pollen"
    assert "telegram" in cards[0]["surfaces"]

    decided = api_client.post(
        f"/v1/pass-approvals/{proposal.id}",
        headers=_auth(raw),
        json={"decision": "approve"},
    )
    assert decided.status_code == 200
    body = decided.json()
    assert body["approval_id"] == proposal.id
    assert body["surface"] == "pollen"
    assert body["proposal"]["status"] == APPROVED

    second = api_client.post(
        f"/v1/pass-approvals/{proposal.id}",
        headers=_auth(raw),
        json={"decision": "approve"},
    )
    assert second.status_code == 400
