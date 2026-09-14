"""HP-111 FastAPI: evolution list is read-gated; decide/accept need approve."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hivepilot.evidence import ingest_ref
from hivepilot.services.token_service import add_token
from hivepilot.skill_evolution import propose


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


def _draft() -> str:
    ingest_ref(ref_id="api-ev", preview="api-ev")
    draft = propose(
        evolution_type="fix",
        name="faulty",
        revision_id="rev-fix",
        causal_event_id="evt-1",
        representative_result="result-1",
        evidence_refs=["api-ev"],
        files={"SKILL.md": "# api\n", "extra.md": "x\n"},
        baseline_files={"SKILL.md": "# old\n"},
    )
    assert draft.proposal_id
    return draft.proposal_id


def test_list_requires_auth(api_client):
    assert api_client.get("/v1/skill-evolutions").status_code == 401


def test_hitl_then_accept(api_client, tmp_tokens_file, tmp_path: Path, monkeypatch):
    from hivepilot.config import settings
    from hivepilot.skill_dirs import skill_scan_dirs

    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setattr(settings, "plugins_enabled", True)
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(
        "hivepilot.services.api_service.skill_scan_dirs", lambda: skill_scan_dirs(tmp_path)
    )

    read_raw, _ = add_token("read")
    approve_raw, _ = add_token("approve")
    proposal_id = _draft()

    listed = api_client.get("/v1/skill-evolutions", headers=_auth(read_raw))
    assert listed.status_code == 200
    cards = listed.json()
    assert len(cards) == 1
    assert cards[0]["id"] == proposal_id
    assert {item["path"] for item in cards[0]["diffs"]} == {"SKILL.md", "extra.md"}
    assert cards[0]["status"] == "PENDING"

    denied = api_client.post(
        f"/v1/skill-evolutions/{proposal_id}/approve",
        headers=_auth(read_raw),
    )
    assert denied.status_code == 403

    premature = api_client.post(
        f"/v1/skill-evolutions/{proposal_id}/accept",
        json={"expected_digest": cards[0]["content_hash"]},
        headers=_auth(approve_raw),
    )
    assert premature.status_code == 403

    approved = api_client.post(
        f"/v1/skill-evolutions/{proposal_id}/approve",
        headers=_auth(approve_raw),
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"

    stale = api_client.post(
        f"/v1/skill-evolutions/{proposal_id}/accept",
        json={"expected_digest": "wrong"},
        headers=_auth(approve_raw),
    )
    assert stale.status_code == 409

    accepted = api_client.post(
        f"/v1/skill-evolutions/{proposal_id}/accept",
        json={"expected_digest": cards[0]["content_hash"]},
        headers=_auth(approve_raw),
    )
    assert accepted.status_code == 200
    assert accepted.json()["applied"] is True
    dest = skills / "faulty"
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "# api\n"
    assert (dest / "extra.md").read_text(encoding="utf-8") == "x\n"

    again = api_client.post(
        f"/v1/skill-evolutions/{proposal_id}/accept",
        json={"expected_digest": cards[0]["content_hash"]},
        headers=_auth(approve_raw),
    )
    assert again.status_code == 200
    assert again.json()["applied"] is True
