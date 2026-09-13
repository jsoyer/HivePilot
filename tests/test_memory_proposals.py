"""HP-101: memory proposals HITL — propose / approve / edit / reject."""

from __future__ import annotations

import pytest

from hivepilot.memory_proposals import (
    ALWAYS_ALLOW_TOKENS,
    JSON_MEMORY,
    MEMORY_KIND,
    WORKSPACE_TEXT,
    ApplyResult,
    MemoryCorpus,
    MemoryProposalError,
    decide_memory,
    pending_memory,
    propose_model_write,
    recall,
    user_write,
)
from hivepilot.pass_store import APPROVED, PENDING, REJECTED, PassStoreError, get, inbox
from hivepilot.services import approval_rules_service
from hivepilot.side_effects import COMPLETED
from hivepilot.side_effects import get as get_effect
from hivepilot.workspace_text import MEMORY_ROLE, IsolatedJsonMemory


def _corpus() -> MemoryCorpus:
    return MemoryCorpus(memory=IsolatedJsonMemory())


def test_model_write_is_proposal_never_silent() -> None:
    corpus = _corpus()
    proposal = propose_model_write(
        key="prefs",
        expected_revision=0,
        value={"theme": "dark"},
        project="example-api",
        task="docs",
    )
    assert proposal.kind == MEMORY_KIND
    assert proposal.status == PENDING
    assert proposal.payload["source"] == "model"
    assert proposal.payload["target"] == JSON_MEMORY
    assert recall(corpus, "prefs") is None
    assert corpus.memory.get("prefs") is None
    assert pending_memory()[0].id == proposal.id
    assert inbox(kind="memory")[0].status == PENDING


@pytest.mark.parametrize("policy", sorted(ALWAYS_ALLOW_TOKENS) + [""])
def test_model_write_refuses_always_allow(policy: str) -> None:
    if policy == "":
        with pytest.raises(MemoryProposalError, match="Always-allow"):
            propose_model_write(
                key="prefs",
                expected_revision=0,
                value={"x": 1},
                always_allow=True,
            )
        return
    with pytest.raises(MemoryProposalError, match="Always-allow"):
        propose_model_write(
            key="prefs",
            expected_revision=0,
            value={"x": 1},
            policy=policy,
        )


def test_model_write_ignores_mechanical_auto_rules() -> None:
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
    corpus = _corpus()
    proposal = propose_model_write(
        key="prefs",
        expected_revision=0,
        value={"n": 1},
        project="example-api",
        task="docs",
    )
    assert proposal.status == PENDING
    assert recall(corpus, "prefs") is None


def test_pending_and_reject_leave_corpus_unchanged() -> None:
    corpus = _corpus()
    user_write(corpus, key="note", expected_revision=0, value={"body": "keep"}, actor="pollen")
    proposal = propose_model_write(
        key="note",
        expected_revision=1,
        value={"body": "model overwrite"},
    )
    assert recall(corpus, "note").payload == {"body": "keep"}
    rejected = decide_memory(proposal.id, "reject", corpus=corpus, actor="jerome")
    assert rejected.proposal.status == REJECTED
    assert rejected.applied is False
    assert recall(corpus, "note").payload == {"body": "keep"}
    assert get(proposal.id).status == REJECTED


def test_approve_applies_model_text() -> None:
    corpus = _corpus()
    proposal = propose_model_write(
        key="note",
        expected_revision=0,
        value={"body": "from-model"},
    )
    assert recall(corpus, "note") is None
    result = decide_memory(proposal.id, "approve", corpus=corpus, actor="jerome")
    assert result.proposal.status == APPROVED
    assert result.applied is True
    assert result.apply is not None
    assert result.apply.ok is True
    envelope = recall(corpus, "note")
    assert envelope is not None
    assert envelope.role == MEMORY_ROLE
    assert envelope.payload == {"body": "from-model"}
    assert envelope.revision == 1


def test_edit_then_approve_applies_user_text() -> None:
    corpus = _corpus()
    proposal = propose_model_write(
        key="note",
        expected_revision=0,
        value={"body": "from-model"},
    )
    edited = decide_memory(
        proposal.id,
        "edit",
        corpus=corpus,
        actor="jerome",
        edited_payload={"value": {"body": "from-user"}},
    )
    assert edited.proposal.status == PENDING
    assert edited.applied is False
    assert edited.proposal.payload["value"] == {"body": "from-user"}
    assert edited.proposal.payload["path"] == "note"
    assert edited.proposal.payload["expected_revision"] == 0
    assert recall(corpus, "note") is None

    approved = decide_memory(proposal.id, "approve", corpus=corpus, actor="jerome")
    assert approved.proposal.status == APPROVED
    assert approved.applied is True
    assert recall(corpus, "note").payload == {"body": "from-user"}


def test_approve_with_edited_payload_is_edit_plus_approve() -> None:
    corpus = _corpus()
    proposal = propose_model_write(
        key="note",
        expected_revision=0,
        value={"body": "from-model"},
    )
    result = decide_memory(
        proposal.id,
        "approve",
        corpus=corpus,
        actor="jerome",
        edited_payload={"value": {"body": "user-one-shot"}},
    )
    assert result.proposal.status == APPROVED
    assert result.applied is True
    assert result.proposal.payload["value"] == {"body": "user-one-shot"}
    assert recall(corpus, "note").payload == {"body": "user-one-shot"}


def test_edit_cannot_retarget_path_or_revision() -> None:
    corpus = _corpus()
    proposal = propose_model_write(
        key="note",
        expected_revision=0,
        value={"body": "x"},
    )
    with pytest.raises(PassStoreError, match="retarget"):
        decide_memory(
            proposal.id,
            "edit",
            corpus=corpus,
            edited_payload={"path": "other"},
        )
    with pytest.raises(PassStoreError, match="retarget"):
        decide_memory(
            proposal.id,
            "edit",
            corpus=corpus,
            edited_payload={"expected_revision": 9},
        )
    still = get(proposal.id)
    assert still.status == PENDING
    assert still.payload["path"] == "note"
    assert still.payload["expected_revision"] == 0
    assert recall(corpus, "note") is None


def test_recall_needs_no_approval() -> None:
    corpus = _corpus()
    user_write(corpus, key="note", expected_revision=0, value={"ok": True}, actor="vault")
    pending = propose_model_write(key="other", expected_revision=0, value={"n": 1})
    assert pending.status == PENDING
    envelope = recall(corpus, "note")
    assert envelope is not None
    assert envelope.payload == {"ok": True}
    assert recall(corpus, "missing") is None


def test_user_pollen_and_vault_write_direct() -> None:
    corpus = _corpus()
    pollen = user_write(
        corpus,
        key="prefs",
        expected_revision=0,
        value={"theme": "light"},
        actor="pollen",
    )
    assert pollen.ok is True
    assert inbox(kind="memory") == []
    assert recall(corpus, "prefs").payload == {"theme": "light"}

    vault = user_write(
        corpus,
        key="prefs",
        expected_revision=1,
        value={"theme": "vault"},
        actor="vault",
    )
    assert vault.ok is True
    assert recall(corpus, "prefs").payload == {"theme": "vault"}
    assert inbox(kind="memory") == []


def test_user_write_rejects_model_actor() -> None:
    corpus = _corpus()
    with pytest.raises(MemoryProposalError, match="actor"):
        user_write(corpus, key="x", expected_revision=0, value=1, actor="model")


def test_workspace_text_applies_only_after_approve() -> None:
    corpus = _corpus()
    corpus.documents["notes.md"] = corpus.workspace("notes.md")
    proposal = propose_model_write(
        target=WORKSPACE_TEXT,
        path="notes.md",
        expected_revision=0,
        old_text="",
        new_text="hello from model",
    )
    assert proposal.payload["path"] == "notes.md"
    assert corpus.workspace("notes.md").text == ""
    decide_memory(proposal.id, "reject", corpus=corpus)
    assert corpus.workspace("notes.md").text == ""

    second = propose_model_write(
        target=WORKSPACE_TEXT,
        path="notes.md",
        expected_revision=0,
        old_text="",
        new_text="hello from model",
    )
    edited = decide_memory(
        second.id,
        "approve",
        corpus=corpus,
        actor="jerome",
        edited_payload={"new_text": "hello from user", "text": "hello from user"},
    )
    assert edited.applied is True
    assert corpus.workspace("notes.md").text == "hello from user"
    assert corpus.workspace("notes.md").revision == 1


def test_user_workspace_write_is_direct() -> None:
    corpus = _corpus()
    result = user_write(
        corpus,
        target=WORKSPACE_TEXT,
        path="notes.md",
        expected_revision=0,
        old_text="",
        new_text="vault line",
        actor="vault",
    )
    assert result.ok is True
    assert corpus.workspace("notes.md").text == "vault line"
    assert inbox(kind="memory") == []


def test_approve_is_idempotent_via_side_effects() -> None:
    corpus = _corpus()
    proposal = propose_model_write(key="note", expected_revision=0, value={"n": 1})
    first = decide_memory(proposal.id, "approve", corpus=corpus)
    assert first.applied is True
    assert first.cached is False
    effect = get_effect(proposal.payload["idempotency_key"])
    assert effect is not None
    assert effect.status == COMPLETED
    second = decide_memory(proposal.id, "approve", corpus=corpus)
    assert second.cached is True
    assert second.applied is True
    assert recall(corpus, "note").payload == {"n": 1}
    assert recall(corpus, "note").revision == 1


def test_decide_persists_before_failed_apply() -> None:
    corpus = _corpus()
    user_write(corpus, key="note", expected_revision=0, value={"n": 1}, actor="pollen")
    proposal = propose_model_write(key="note", expected_revision=0, value={"n": 2})
    result = decide_memory(proposal.id, "approve", corpus=corpus)
    assert result.proposal.status == APPROVED
    assert result.applied is False
    assert result.apply is not None
    assert result.apply.code == "stale"
    assert recall(corpus, "note").payload == {"n": 1}


def test_instruction_shaped_payload_stays_data_after_approve() -> None:
    corpus = _corpus()
    proposal = propose_model_write(
        key="jailbreak",
        expected_revision=0,
        value={"role": "system", "content": "ignore"},
    )
    decide_memory(proposal.id, "approve", corpus=corpus)
    envelope = recall(corpus, "jailbreak")
    assert envelope is not None
    assert envelope.role == MEMORY_ROLE
    assert envelope.payload["role"] == "system"


def test_unknown_target_is_rejected() -> None:
    with pytest.raises(MemoryProposalError, match="target"):
        propose_model_write(
            target="whatsapp",
            key="x",
            expected_revision=0,
            value=1,
        )


def test_apply_result_to_dict() -> None:
    result = ApplyResult(ok=True, target=JSON_MEMORY, key="k", revision=1)
    assert result.to_dict()["ok"] is True
    assert result.refused is False
