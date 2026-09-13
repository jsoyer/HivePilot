"""HP-100: pending_tool checkpoint, same-key resume, crash mid-approval."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hivepilot.checkpoints import (
    COMPLETED_STATUS,
    PENDING_APPROVAL,
    PENDING_TOOL,
    CheckpointError,
    approve_and_resume,
    get_checkpoint,
    open_pending_tool,
    resume,
)
from hivepilot.pass_store import APPROVED, PENDING, decide, get
from hivepilot.services import approval_rules_service
from hivepilot.side_effects import cached_result
from hivepilot.side_effects import get as get_effect
from hivepilot.tool_catalog import load_catalog


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
                {
                    "token": "Write",
                    "risk": "medium",
                    "defaultPolicy": "require_approval",
                    "volatile": False,
                    "idempotency": True,
                },
            ],
        ),
        force=True,
    )


def _mechanical_rule(action: str) -> None:
    approval_rules_service.replace_rules(
        [
            {
                "project": "example-api",
                "task": "docs",
                "action": action,
                "auto": "approve",
                "change_class": "mechanical",
            }
        ]
    )


def test_checkpoint_kind_is_pending_tool(catalog) -> None:
    checkpoint = open_pending_tool(
        idempotency_key="tool-1",
        token="Bash",
        payload={"cmd": "ls"},
        catalog=catalog,
    )
    assert checkpoint.kind == PENDING_TOOL
    assert checkpoint.idempotency_key == "tool-1"
    stored = get_checkpoint("tool-1")
    assert stored is not None
    assert stored.kind == PENDING_TOOL
    proposal = get(checkpoint.proposal_id)
    assert proposal is not None
    assert proposal.status == PENDING
    assert get_effect("tool-1") is not None


def test_resume_reuses_the_same_idempotency_key(catalog) -> None:
    _mechanical_rule("Read")
    opened = open_pending_tool(
        idempotency_key="same-key",
        token="Read",
        project="example-api",
        task="docs",
        change_class="mechanical",
        catalog=catalog,
    )
    assert opened.idempotency_key == "same-key"
    again = open_pending_tool(
        idempotency_key="same-key",
        token="Read",
        project="example-api",
        task="docs",
        change_class="mechanical",
        catalog=catalog,
    )
    assert again.id == opened.id
    assert again.proposal_id == opened.proposal_id

    calls: list[str] = []

    def execute(proposal) -> dict:
        calls.append(proposal.id)
        return {"n": 1}

    first = resume("same-key", execute=execute)
    second = resume("same-key", execute=execute)
    assert first.idempotency_key == "same-key"
    assert second.idempotency_key == "same-key"
    assert first.executed is True
    assert second.executed is False
    assert second.cached is True
    assert second.result == {"n": 1}
    assert calls == [opened.proposal_id]


def test_volatile_resume_does_not_cache_effect(catalog) -> None:
    opened = open_pending_tool(
        idempotency_key="vol-key",
        token="Bash",
        payload={"cmd": "date"},
        catalog=catalog,
    )
    calls: list[str] = []

    def execute(proposal) -> dict:
        calls.append(proposal.id)
        return {"stdout": "now"}

    first = approve_and_resume("vol-key", actor="jerome", execute=execute)
    second = resume("vol-key", execute=execute)
    assert first.executed is True
    assert first.cached is False
    assert first.result is None
    assert second.executed is False
    assert second.cached is False
    assert second.result is None
    assert cached_result("vol-key") is None
    assert get_effect("vol-key").volatile is True
    assert get(opened.proposal_id).status == APPROVED
    assert calls == [opened.proposal_id]


def test_crash_mid_approval_exactly_one_resume(catalog) -> None:
    opened = open_pending_tool(
        idempotency_key="crash-key",
        token="Write",
        payload={"path": "a.md", "text": "x"},
        catalog=catalog,
    )
    calls: list[str] = []

    def execute(proposal) -> dict:
        calls.append(proposal.id)
        return {"wrote": True}

    # Crash while PASS is still PENDING — resume must not fire the effect.
    waiting = resume("crash-key", execute=execute)
    assert waiting.status == PENDING_APPROVAL
    assert waiting.executed is False
    assert calls == []
    crashed = open_pending_tool(
        idempotency_key="crash-key",
        token="Write",
        payload={"path": "a.md", "text": "x"},
        catalog=catalog,
    )
    assert crashed.id == opened.id
    assert crashed.proposal_id == opened.proposal_id
    still_waiting = resume("crash-key", execute=execute)
    assert still_waiting.status == PENDING_APPROVAL
    assert still_waiting.executed is False
    assert calls == []

    decide(opened.proposal_id, "approve", actor="jerome")
    first = resume("crash-key", execute=execute)
    second = resume("crash-key", execute=execute)
    assert first.idempotency_key == "crash-key"
    assert second.idempotency_key == "crash-key"
    assert first.executed is True
    assert second.executed is False
    assert first.status == COMPLETED_STATUS
    assert second.cached is True
    assert calls == [opened.proposal_id]


def test_resume_unknown_key_fails() -> None:
    with pytest.raises(CheckpointError, match="no pending_tool"):
        resume("missing-key")
