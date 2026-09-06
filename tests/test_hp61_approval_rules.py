"""HP-61: per-action approval rules + API + policy hook."""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import approval_rules_service
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
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def test_match_auto_specificity_and_wildcard():
    approval_rules_service.replace_rules(
        [
            {"project": "", "task": "", "action": "", "auto": "deny"},
            {"project": "example-api", "task": "docs", "action": "", "auto": "approve"},
        ]
    )
    assert (
        approval_rules_service.match_auto(project="example-api", task="docs", metadata={})
        == "approve"
    )
    assert approval_rules_service.match_auto(project="other", task="docs", metadata={}) == "deny"


def test_match_auto_none_without_rules():
    approval_rules_service.replace_rules([])
    assert approval_rules_service.match_auto(project="x", task="y", metadata={}) is None


def test_replace_rules_rejects_bad_auto():
    with pytest.raises(approval_rules_service.ApprovalRuleError, match="approve or deny"):
        approval_rules_service.replace_rules([{"auto": "maybe"}])


def test_api_rules_role_gate(tmp_tokens_file, api_client):
    admin, _ = add_token("admin", note="hp61")
    reader, _ = add_token("read", note="hp61-read")

    listed = api_client.get("/v1/approval-rules", headers=_auth(reader))
    assert listed.status_code == 200
    assert listed.json() == {"rules": []}

    denied = api_client.put(
        "/v1/approval-rules",
        headers=_auth(reader),
        json={"rules": [{"auto": "approve", "project": "example-api"}]},
    )
    assert denied.status_code == 403

    ok = api_client.put(
        "/v1/approval-rules",
        headers=_auth(admin),
        json={"rules": [{"auto": "approve", "project": "example-api", "task": "docs"}]},
    )
    assert ok.status_code == 200
    rules = ok.json()["rules"]
    assert len(rules) == 1
    assert rules[0]["auto"] == "approve"
    assert rules[0]["project"] == "example-api"


def test_orchestrator_auto_approve_skips_pending(monkeypatch):
    from hivepilot.services.approval_rules_service import match_auto

    approval_rules_service.replace_rules(
        [{"project": "example-api", "task": "docs", "auto": "approve"}]
    )
    assert match_auto(project="example-api", task="docs", metadata={"task": "docs"}) == "approve"
