"""HP-116: schedules.create goes through PASS; class ≠ mechanical."""

from __future__ import annotations

from pathlib import Path

import yaml

from hivepilot.pass_store import APPROVED, PENDING, REJECTED, match_auto
from hivepilot.schedule_create import (
    SCHEDULES_CREATE_CLASS,
    SCHEDULES_CREATE_TOKEN,
    ScheduleCreateError,
    approve,
    request,
)
from hivepilot.services import approval_rules_service
from hivepilot.services.approval_rules_service import MECHANICAL, PRODUCT_FORK
from hivepilot.services.schedule_service import load_schedules
from hivepilot.tool_catalog import load_catalog
from hivepilot.workspace_paths import WorkspacePathError


def _catalog(tmp_path: Path, *, policy: str = "require_approval"):
    path = tmp_path / "tool_catalog.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tools": [
                    {
                        "token": SCHEDULES_CREATE_TOKEN,
                        "risk": "high",
                        "defaultPolicy": policy,
                        "volatile": False,
                        "idempotency": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return load_catalog(path, force=True)


def _request(tmp_path: Path, **kwargs):
    schedules = kwargs.pop("schedules_file", tmp_path / "schedules.yaml")
    catalog = kwargs.pop("catalog", None) or _catalog(tmp_path)
    return request(
        name=kwargs.pop("name", "nightly-docs"),
        task=kwargs.pop("task", "docs"),
        projects=kwargs.pop("projects", ["example-api"]),
        schedules_file=schedules,
        workspace_root=tmp_path,
        catalog=catalog,
        **kwargs,
    )


def test_request_is_pending_and_does_not_write(tmp_path: Path) -> None:
    schedules = tmp_path / "schedules.yaml"
    issued = _request(tmp_path, schedules_file=schedules)
    assert issued.pending is True
    assert issued.written is False
    assert issued.proposal.status == PENDING
    assert issued.proposal.action == SCHEDULES_CREATE_TOKEN
    assert issued.proposal.change_class == PRODUCT_FORK
    assert issued.proposal.change_class != MECHANICAL
    assert not schedules.exists()


def test_approve_writes_schedule(tmp_path: Path) -> None:
    schedules = tmp_path / "schedules.yaml"
    issued = _request(tmp_path, schedules_file=schedules)
    decided = approve(issued.proposal.id, actor="jerome")
    assert decided.proposal.status == APPROVED
    assert decided.written is True
    entries = load_schedules(schedules)
    assert "nightly-docs" in entries
    assert entries["nightly-docs"].task == "docs"
    assert entries["nightly-docs"].projects == ["example-api"]


def test_reject_does_not_write(tmp_path: Path) -> None:
    from hivepilot.pass_store import decide

    schedules = tmp_path / "schedules.yaml"
    issued = _request(tmp_path, schedules_file=schedules)
    stored = decide(issued.proposal.id, "reject", actor="jerome")
    assert stored.status == REJECTED
    assert not schedules.exists()


def test_mechanical_claim_and_allow_override_still_hitl(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, policy="allow")
    approval_rules_service.replace_rules(
        [
            {
                "project": "example-api",
                "task": "docs",
                "action": SCHEDULES_CREATE_TOKEN,
                "auto": "approve",
                "change_class": MECHANICAL,
            }
        ]
    )
    issued = _request(
        tmp_path,
        catalog=catalog,
        project="example-api",
        change_class=MECHANICAL,
    )
    assert issued.proposal.status == PENDING
    assert issued.written is False
    assert issued.proposal.change_class == SCHEDULES_CREATE_CLASS
    assert (
        match_auto(
            kind="tool",
            project="example-api",
            task="docs",
            metadata={
                "token": SCHEDULES_CREATE_TOKEN,
                "change_class": issued.proposal.change_class,
            },
            catalog=catalog,
        )
        is None
    )


def test_shipped_catalog_requires_approval() -> None:
    from hivepilot.tool_catalog import reload_catalog, resolve

    reload_catalog()
    decision = resolve(SCHEDULES_CREATE_TOKEN)
    assert decision.known is True
    assert decision.needs_approval is True
    assert decision.risk == "high"
    assert decision.default_policy == "require_approval"


def test_workspace_path_escape_refuses_before_pass(tmp_path: Path) -> None:
    try:
        _request(tmp_path, path="../../etc/passwd")
        raised = False
    except WorkspacePathError:
        raised = True
    assert raised
    from hivepilot.pass_store import inbox

    assert inbox(kind="tool") == []


def test_escaping_symlink_path_refuses_before_pass(tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "leak").symlink_to(outside)
    try:
        request(
            name="leaky",
            task="docs",
            projects=["example-api"],
            path="leak",
            workspace_root=workspace,
            schedules_file=workspace / "schedules.yaml",
            catalog=_catalog(tmp_path),
        )
        raised = False
    except WorkspacePathError:
        raised = True
    assert raised


def test_relative_workspace_path_is_stored_confined(tmp_path: Path) -> None:
    (tmp_path / "cron").mkdir()
    issued = _request(tmp_path, path="cron/job.sh")
    assert issued.proposal.payload["path"] == "cron/job.sh"
    assert issued.pending is True


def test_duplicate_name_refuses(tmp_path: Path) -> None:
    schedules = tmp_path / "schedules.yaml"
    first = _request(tmp_path, schedules_file=schedules)
    approve(first.proposal.id, actor="jerome")
    try:
        _request(tmp_path, schedules_file=schedules)
        raised = False
    except ScheduleCreateError as exc:
        raised = True
        assert "already exists" in str(exc)
    assert raised


def test_second_request_same_name_reuses_pending(tmp_path: Path) -> None:
    first = _request(tmp_path)
    second = _request(tmp_path)
    assert first.proposal.id == second.proposal.id
    assert second.pending is True
