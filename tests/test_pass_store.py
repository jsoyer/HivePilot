"""HP-97: unified PASS store — decide + compose match_auto."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hivepilot.pass_store import (
    APPROVED,
    EDITED,
    EXPIRED,
    PENDING,
    REJECTED,
    PassStoreError,
    create_pending,
    decide,
    get,
    inbox,
    match_auto,
    merge_edit,
    partition_hp61_metadata,
    submit,
)
from hivepilot.services import approval_rules_service
from hivepilot.tool_catalog import APPROVAL_KINDS, load_catalog


def _write_catalog(tmp_path: Path, tools: list[dict]) -> Path:
    path = tmp_path / "tool_catalog.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "tools": tools}), encoding="utf-8")
    return path


@pytest.fixture()
def catalog(tmp_path: Path):
    return load_catalog(
        _write_catalog(
            tmp_path,
            [
                {
                    "token": "Read",
                    "risk": "low",
                    "defaultPolicy": "allow",
                    "volatile": False,
                    "idempotency": True,
                },
                {
                    "token": "Bash",
                    "risk": "high",
                    "defaultPolicy": "require_approval",
                    "volatile": True,
                    "idempotency": False,
                },
            ],
        ),
        force=True,
    )


def test_create_pending_persists_before_any_decide() -> None:
    proposal = create_pending(
        kind="memory",
        project="example-api",
        task="docs",
        payload={"path": "notes.md", "revision": 3, "text": "hello"},
    )
    assert proposal.status == PENDING
    assert proposal.kind == "memory"
    assert get(proposal.id) is not None
    assert get(proposal.id).status == PENDING
    assert inbox(kind="memory")[0].id == proposal.id


def test_decide_approve_reject_edit_expire() -> None:
    approved = create_pending(kind="tool", payload={"token": "Read"})
    assert decide(approved.id, "approve", actor="jerome").status == APPROVED

    rejected = create_pending(kind="tool", payload={"token": "Bash"})
    assert decide(rejected.id, "reject", actor="jerome").status == REJECTED

    edited = create_pending(
        kind="memory",
        payload={"path": "mem.json", "revision": 1, "text": "old"},
    )
    result = decide(
        edited.id,
        "edit",
        actor="jerome",
        edited_payload={"text": "new"},
    )
    assert result.status == EDITED
    assert result.payload["text"] == "new"
    assert result.payload["path"] == "mem.json"
    assert result.payload["revision"] == 1

    expired = create_pending(kind="skill_evolution", payload={"path": "skill.md"})
    assert decide(expired.id, "expire").status == EXPIRED


def test_decide_persists_before_side_effect() -> None:
    seen: list[str] = []

    def boom(proposal) -> None:
        seen.append(proposal.status)
        raise RuntimeError("side-effect failed")

    proposal = create_pending(kind="tool", payload={"token": "Read"})
    with pytest.raises(RuntimeError, match="side-effect failed"):
        decide(proposal.id, "approve", actor="t", side_effect=boom)

    stored = get(proposal.id)
    assert stored is not None
    assert stored.status == APPROVED
    assert seen == [APPROVED]


def test_edit_cannot_retarget_path_or_revision() -> None:
    proposal = create_pending(
        kind="memory",
        payload={"path": "a.md", "revision": 2, "expected_revision": 2, "text": "x"},
    )
    with pytest.raises(PassStoreError, match="retarget"):
        decide(proposal.id, "edit", edited_payload={"path": "b.md"})
    with pytest.raises(PassStoreError, match="retarget"):
        decide(proposal.id, "edit", edited_payload={"revision": 9})
    with pytest.raises(PassStoreError, match="retarget"):
        decide(proposal.id, "edit", edited_payload={"expected_revision": 9})
    still = get(proposal.id)
    assert still is not None
    assert still.status == PENDING
    assert still.payload["path"] == "a.md"
    assert still.payload["revision"] == 2


def test_merge_edit_refuses_new_target_keys() -> None:
    with pytest.raises(PassStoreError, match="retarget"):
        merge_edit({"text": "x"}, {"path": "/etc/passwd"})


def test_inbox_filters_by_kind() -> None:
    create_pending(kind="tool", payload={"token": "Read"})
    create_pending(kind="memory", payload={"path": "m.json", "revision": 0})
    create_pending(kind="skill_evolution", payload={"path": "s.md", "revision": 1})
    create_pending(kind="partition", payload={"path": "plan.json"})
    assert {row.kind for row in inbox()} == set(APPROVAL_KINDS)
    tools = inbox(kind="tool")
    assert all(row.kind == "tool" for row in tools)
    assert len(tools) == 1
    assert inbox(kind="memory")[0].kind == "memory"


def test_hp94_kind_is_partitioned_from_hp61_action() -> None:
    meta = partition_hp61_metadata("tool", {"kind": "tool", "token": "Read"})
    assert meta["pass_kind"] == "tool"
    assert meta["action"] == "Read"
    assert meta.get("kind") != "tool" or "kind" not in meta


def test_match_auto_unknown_tool_is_denied(catalog) -> None:
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
        match_auto(
            kind="tool",
            project="example-api",
            task="docs",
            metadata={"token": "not-a-real-tool", "change_class": "mechanical"},
            catalog=catalog,
        )
        == "deny"
    )


def test_match_auto_catalog_require_approval_stays_hitl(catalog) -> None:
    approval_rules_service.replace_rules(
        [
            {
                "project": "example-api",
                "task": "docs",
                "action": "Bash",
                "auto": "approve",
                "change_class": "mechanical",
            }
        ]
    )
    assert (
        match_auto(
            kind="tool",
            project="example-api",
            task="docs",
            metadata={"token": "Bash", "change_class": "mechanical"},
            catalog=catalog,
        )
        is None
    )


def test_match_auto_allow_plus_mechanical_rule_approves(catalog) -> None:
    approval_rules_service.replace_rules(
        [
            {
                "project": "example-api",
                "task": "docs",
                "action": "Read",
                "auto": "approve",
                "change_class": "mechanical",
            }
        ]
    )
    assert (
        match_auto(
            kind="tool",
            project="example-api",
            task="docs",
            metadata={"token": "Read", "change_class": "mechanical"},
            catalog=catalog,
        )
        == "approve"
    )


@pytest.mark.parametrize("change_class", ["product_fork", "security", "destructive", "unknown"])
def test_match_auto_non_mechanical_never_auto(catalog, change_class: str) -> None:
    approval_rules_service.replace_rules(
        [
            {
                "project": "example-api",
                "task": "docs",
                "action": "Read",
                "auto": "approve",
                "change_class": "mechanical",
            }
        ]
    )
    assert (
        match_auto(
            kind="tool",
            project="example-api",
            task="docs",
            metadata={"token": "Read", "change_class": change_class},
            catalog=catalog,
        )
        is None
    )


def test_match_auto_does_not_treat_hp94_kind_as_hp61_action(catalog) -> None:
    approval_rules_service.replace_rules(
        [
            {
                "project": "example-api",
                "task": "docs",
                "action": "tool",
                "auto": "approve",
                "change_class": "mechanical",
            }
        ]
    )
    assert (
        match_auto(
            kind="tool",
            project="example-api",
            task="docs",
            metadata={"token": "Read", "kind": "tool", "change_class": "mechanical"},
            catalog=catalog,
        )
        is None
    )


def test_match_auto_memory_ignores_unknown_tool_token(catalog) -> None:
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
        match_auto(
            kind="memory",
            project="example-api",
            task="docs",
            metadata={"token": "not-a-real-tool", "change_class": "mechanical"},
            catalog=catalog,
        )
        == "approve"
    )
    assert (
        match_auto(
            kind="memory",
            project="example-api",
            task="docs",
            metadata={"token": "not-a-real-tool", "change_class": "product_fork"},
            catalog=catalog,
        )
        is None
    )


def test_match_auto_rules_deny_still_fires(catalog) -> None:
    approval_rules_service.replace_rules(
        [{"project": "example-api", "task": "docs", "auto": "deny"}]
    )
    assert (
        match_auto(
            kind="tool",
            project="example-api",
            task="docs",
            metadata={"token": "Read"},
            catalog=catalog,
        )
        == "deny"
    )


def test_submit_auto_approve_persists_before_side_effect(catalog) -> None:
    approval_rules_service.replace_rules(
        [
            {
                "project": "example-api",
                "task": "docs",
                "action": "Read",
                "auto": "approve",
                "change_class": "mechanical",
            }
        ]
    )
    seen: list[str] = []

    def mark(proposal) -> None:
        seen.append(proposal.status)

    proposal = submit(
        kind="tool",
        project="example-api",
        task="docs",
        change_class="mechanical",
        payload={"token": "Read"},
        catalog=catalog,
        side_effect=mark,
    )
    assert proposal.status == APPROVED
    assert seen == [APPROVED]
    assert inbox(status=PENDING) == []


def test_submit_unknown_tool_auto_rejects(catalog) -> None:
    proposal = submit(
        kind="tool",
        project="example-api",
        task="docs",
        payload={"token": "totally-unknown"},
        catalog=catalog,
    )
    assert proposal.status == REJECTED
    assert proposal.reason == "match_auto deny"


def test_terminal_cannot_decide_again() -> None:
    proposal = create_pending(kind="tool", payload={"token": "Read"})
    decide(proposal.id, "approve")
    with pytest.raises(PassStoreError, match="already APPROVED"):
        decide(proposal.id, "reject")


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(PassStoreError, match="kind must be"):
        create_pending(kind="whatsapp")
