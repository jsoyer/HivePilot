"""HP-79 FastAPI workshop: propose is run-gated, accept/reject is approve-gated."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hivepilot.services.token_service import add_token
from hivepilot.skill_dirs import SKILL_MANIFEST


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


def test_list_skills_requires_auth(api_client):
    assert api_client.get("/v1/skills").status_code == 401


def test_list_skills_ok_for_read(api_client, tmp_tokens_file):
    raw, _ = add_token("read")
    resp = api_client.get("/v1/skills", headers=_auth(raw))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_propose_requires_run_and_accept_requires_approve(
    api_client, tmp_tokens_file, tmp_path, monkeypatch
):
    from hivepilot.services import skill_workshop_service as sws
    from hivepilot.services.api_service import _get_orchestrator

    root = tmp_path / "skills" / "workshop-demo"
    root.mkdir(parents=True)
    body = "# Workshop demo\n\nBe helpful.\n"
    (root / SKILL_MANIFEST).write_text(body, encoding="utf-8")
    spec = {
        "name": "workshop-demo",
        "description": "demo",
        "provider": f"directory:{root.resolve()}",
        "files": {SKILL_MANIFEST: body},
    }

    class _PM:
        def get_skill(self, name: str):
            return spec if name == "workshop-demo" else None

        def list_skills(self):
            return [spec]

    orch = _get_orchestrator()
    monkeypatch.setattr(orch, "plugins", _PM())
    monkeypatch.setattr(sws, "skill_scan_dirs", lambda: [tmp_path / "skills"])

    read_raw, _ = add_token("read")
    run_raw, _ = add_token("run")
    approve_raw, _ = add_token("approve")

    payload = {
        "skill_name": "workshop-demo",
        "files": {SKILL_MANIFEST: body + "\nCite sources.\n"},
        "rationale": "add a citation habit",
    }
    assert (
        api_client.post("/v1/skills/proposals", json=payload, headers=_auth(read_raw)).status_code
        == 403
    )
    created = api_client.post("/v1/skills/proposals", json=payload, headers=_auth(run_raw))
    assert created.status_code == 200
    proposal_id = created.json()["id"]
    assert created.json()["status"] == "proposed"
    assert "Cite sources" in created.json()["diff_text"]
    assert (root / SKILL_MANIFEST).read_text(encoding="utf-8") == body

    listed = api_client.get("/v1/skills/proposals?status=proposed", headers=_auth(read_raw))
    assert listed.status_code == 200
    assert any(row["id"] == proposal_id for row in listed.json())

    deny = api_client.post(
        f"/v1/skills/proposals/{proposal_id}/accept",
        json={},
        headers=_auth(run_raw),
    )
    assert deny.status_code == 403

    accepted = api_client.post(
        f"/v1/skills/proposals/{proposal_id}/accept",
        json={"actor": "reviewer"},
        headers=_auth(approve_raw),
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    assert "Cite sources" in (root / SKILL_MANIFEST).read_text(encoding="utf-8")
