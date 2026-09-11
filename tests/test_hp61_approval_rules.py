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
            {
                "project": "example-api",
                "task": "docs",
                "action": "",
                "auto": "approve",
                "change_class": "mechanical",
            },
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


def test_replace_rules_rejects_bad_change_class():
    with pytest.raises(approval_rules_service.ApprovalRuleError, match="change_class"):
        approval_rules_service.replace_rules([{"auto": "approve", "change_class": "maybe"}])


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
        json={
            "rules": [
                {
                    "auto": "approve",
                    "project": "example-api",
                    "task": "docs",
                    "change_class": "mechanical",
                }
            ]
        },
    )
    assert ok.status_code == 200
    rules = ok.json()["rules"]
    assert len(rules) == 1
    assert rules[0]["auto"] == "approve"
    assert rules[0]["project"] == "example-api"
    assert rules[0]["change_class"] == "mechanical"


def test_api_rejects_unknown_change_class(tmp_tokens_file, api_client):
    admin, _ = add_token("admin", note="hp86")
    bad = api_client.put(
        "/v1/approval-rules",
        headers=_auth(admin),
        json={"rules": [{"auto": "approve", "change_class": "mecanical"}]},
    )
    assert bad.status_code == 400
    assert "change_class" in bad.json()["detail"]


def test_orchestrator_auto_approve_skips_pending(monkeypatch):
    from hivepilot.services.approval_rules_service import match_auto

    approval_rules_service.replace_rules(
        [
            {
                "project": "example-api",
                "task": "docs",
                "auto": "approve",
                "change_class": "mechanical",
            }
        ]
    )
    assert match_auto(project="example-api", task="docs", metadata={"task": "docs"}) == "approve"


def test_approve_without_class_is_pending():
    approval_rules_service.replace_rules(
        [{"project": "example-api", "task": "docs", "auto": "approve"}]
    )
    assert (
        approval_rules_service.match_auto(project="example-api", task="docs", metadata={}) is None
    )


def test_human_gate_classes_never_auto_approve():
    for change_class in ("product_fork", "security", "destructive", "unknown"):
        approval_rules_service.replace_rules(
            [
                {
                    "project": "example-api",
                    "task": "docs",
                    "auto": "approve",
                    "change_class": change_class,
                }
            ]
        )
        assert (
            approval_rules_service.match_auto(
                project="example-api",
                task="docs",
                metadata={"change_class": "mechanical"},
            )
            is None
        ), change_class


def test_watcher_wake_is_not_a_class():
    approval_rules_service.replace_rules(
        [{"project": "example-api", "task": "docs", "auto": "approve"}]
    )
    assert (
        approval_rules_service.match_auto(
            project="example-api",
            task="docs",
            metadata={
                "source": "watcher",
                "woke": True,
                "wake": "nudge.posted",
                "event": "approval.requested",
                "bus_kind": "nudge.posted",
                "kind": "pipeline_checkpoint",
            },
        )
        is None
    )
    assert approval_rules_service.claimed_change_class(
        {"source": "watcher", "woke": True, "kind": "pipeline_checkpoint"}
    ) == ""


def test_claimed_non_mechanical_vetoes_mechanical_rule():
    approval_rules_service.replace_rules(
        [
            {
                "project": "example-api",
                "task": "docs",
                "auto": "approve",
                "change_class": "mechanical",
            }
        ]
    )
    assert (
        approval_rules_service.match_auto(
            project="example-api",
            task="docs",
            metadata={"change_class": "product_fork"},
        )
        is None
    )
    assert (
        approval_rules_service.match_auto(
            project="example-api",
            task="docs",
            metadata={"contested": True},
        )
        is None
    )
    assert (
        approval_rules_service.match_auto(
            project="example-api",
            task="docs",
            metadata={"change_class": "mechanical"},
        )
        == "approve"
    )


def test_deny_does_not_need_a_class():
    approval_rules_service.replace_rules(
        [{"project": "example-api", "task": "docs", "auto": "deny"}]
    )
    assert (
        approval_rules_service.match_auto(project="example-api", task="docs", metadata={})
        == "deny"
    )


def test_class_veto_does_not_fall_through():
    approval_rules_service.replace_rules(
        [
            {"project": "", "task": "", "action": "", "auto": "deny"},
            {
                "project": "example-api",
                "task": "docs",
                "auto": "approve",
                "change_class": "product_fork",
            },
        ]
    )
    assert (
        approval_rules_service.match_auto(project="example-api", task="docs", metadata={})
        is None
    )


def test_product_fork_alias_and_empty_default():
    stored = approval_rules_service.replace_rules(
        [
            {"auto": "approve", "change_class": "product-fork"},
            {"id": "bare", "auto": "deny"},
        ]
    )
    by_id = {rule.id: rule for rule in stored}
    assert any(rule.change_class == "product_fork" for rule in stored)
    assert by_id["bare"].change_class == ""


def test_legacy_table_gains_change_class_column(tmp_path, monkeypatch):
    from hivepilot.services import db, state_service

    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(state_service, "DB_PATH", db_path)
    with db.connect() as conn:
        conn.execute(
            """
            CREATE TABLE approval_action_rules (
                id TEXT PRIMARY KEY,
                project TEXT NOT NULL DEFAULT '',
                task TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                auto TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO approval_action_rules (id, project, task, action, auto) "
            "VALUES ('old', 'example-api', 'docs', '', 'approve')"
        )
    rules = approval_rules_service.list_rules()
    assert rules[0].change_class == ""
    assert (
        approval_rules_service.match_auto(project="example-api", task="docs", metadata={})
        is None
    )
