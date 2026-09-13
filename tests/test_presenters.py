"""HP-102: Pollen ↔ Telegram presenter parity (Approvals door only)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from hivepilot.checkpoints import get_checkpoint_by_proposal, open_pending_tool
from hivepilot.memory_proposals import (
    JSON_MEMORY,
    MemoryCorpus,
    propose_model_write,
    recall,
)
from hivepilot.pass_store import APPROVED, PENDING, REJECTED, create_pending, get
from hivepilot.presenters import (
    APPROVAL_DOOR,
    POLLEN,
    SURFACES,
    TELEGRAM,
    TELEGRAM_DOORS,
    PresenterError,
    callback_data,
    decide_approval,
    parse_callback,
    pollen_card,
    present,
    present_pending,
    reset_pending,
    telegram_keyboard,
)
from hivepilot.services.telegram_doors import (
    ALERTS,
    APPROVALS,
    INBOX,
    PERSISTENT_DOORS,
    RUNS,
    approval_actions_allowed,
)
from hivepilot.tool_catalog import load_catalog
from hivepilot.workspace_text import IsolatedJsonMemory


@pytest.fixture(autouse=True)
def _reset_presenter() -> Iterator[None]:
    reset_pending()
    yield
    reset_pending()


def _corpus() -> MemoryCorpus:
    return MemoryCorpus(memory=IsolatedJsonMemory())


def _write_catalog(tmp_path: Path) -> Path:
    path = tmp_path / "tool_catalog.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tools": [
                    {
                        "token": "Bash",
                        "risk": "high",
                        "defaultPolicy": "require_approval",
                        "volatile": True,
                        "idempotency": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def catalog(tmp_path: Path):
    return load_catalog(_write_catalog(tmp_path), force=True)


def test_exactly_four_telegram_doors() -> None:
    assert TELEGRAM_DOORS == (INBOX, APPROVALS, RUNS, ALERTS)
    assert TELEGRAM_DOORS == PERSISTENT_DOORS
    assert len(TELEGRAM_DOORS) == 4
    assert "whatsapp" not in TELEGRAM_DOORS
    assert "general" not in TELEGRAM_DOORS
    assert "approvals-memory" not in TELEGRAM_DOORS


def test_approval_actions_only_on_approvals_door() -> None:
    assert approval_actions_allowed(APPROVALS)
    assert approval_actions_allowed(APPROVAL_DOOR)
    for door in (INBOX, RUNS, ALERTS, "general", "whatsapp"):
        assert not approval_actions_allowed(door)


def test_pollen_and_telegram_share_approval_id() -> None:
    proposal = create_pending(
        kind="memory",
        project="example-api",
        task="docs",
        payload={"target": JSON_MEMORY, "key": "prefs", "value": {"n": 1}},
    )
    card = present(proposal, owner_id="jerome")
    pollen = pollen_card(card)
    keyboard = telegram_keyboard(card, door=APPROVALS)
    assert keyboard is not None
    assert pollen["approval_id"] == keyboard.approval_id == proposal.id == card.approval_id
    assert pollen["surfaces"] == list(SURFACES)
    assert keyboard.door == APPROVAL_DOOR
    assert pollen["door"] == APPROVAL_DOOR
    assert parse_callback(keyboard.buttons[0][0]["callback_data"]) == ("approve", proposal.id)
    assert parse_callback(keyboard.buttons[0][1]["callback_data"]) == ("reject", proposal.id)


def test_telegram_keyboard_absent_off_approvals_door() -> None:
    proposal = create_pending(kind="tool", payload={"token": "Bash"})
    card = present(proposal, owner_id="jerome")
    for door in (INBOX, RUNS, ALERTS, "general"):
        assert telegram_keyboard(card, door=door) is None


def test_callback_data_is_pass_prefix_not_a_fifth_topic() -> None:
    assert callback_data("approve", "abc123") == "pass:approve:abc123"
    assert callback_data("deny", "abc123") == "pass:deny:abc123"
    assert parse_callback("approve:42") is None
    assert parse_callback("concierge:yes:tok") is None


def test_owner_mismatch_does_not_decide() -> None:
    corpus = _corpus()
    proposal = propose_model_write(key="prefs", expected_revision=0, value={"n": 1})
    present(proposal, owner_id="alice")
    with pytest.raises(PresenterError, match="not owned"):
        decide_approval(
            proposal.id,
            "approve",
            owner_id="bob",
            surface=POLLEN,
            corpus=corpus,
        )
    assert get(proposal.id).status == PENDING
    assert recall(corpus, "prefs") is None


def test_ttl_expiry_blocks_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = _corpus()
    proposal = propose_model_write(key="prefs", expected_revision=0, value={"n": 1})
    present(proposal, owner_id="alice")

    import hivepilot.services.pending_confirmation as pending_mod

    real_time = pending_mod.time.time
    monkeypatch.setattr(pending_mod.time, "time", lambda: real_time() + 16 * 60)
    with pytest.raises(PresenterError, match="expired"):
        decide_approval(
            proposal.id,
            "approve",
            owner_id="alice",
            surface=TELEGRAM,
            corpus=corpus,
        )
    assert get(proposal.id).status == PENDING


def test_same_approval_id_single_resume_across_surfaces(catalog) -> None:
    executed: list[str] = []

    def execute(proposal) -> str:
        executed.append(proposal.id)
        return "ran"

    checkpoint = open_pending_tool(
        idempotency_key="tool-once",
        token="Bash",
        project="example-api",
        task="docs",
        catalog=catalog,
    )
    proposal = get(checkpoint.proposal_id)
    assert proposal is not None
    assert proposal.status == PENDING
    card = present(proposal, owner_id="jerome")
    first = decide_approval(
        card.approval_id,
        "approve",
        owner_id="jerome",
        surface=POLLEN,
        execute=execute,
    )
    assert first.approval_id == card.approval_id
    assert first.proposal.status == APPROVED
    assert first.resumed is True
    assert executed == [proposal.id]
    assert get_checkpoint_by_proposal(proposal.id) is not None

    with pytest.raises(PresenterError, match="expired|missing|not owned"):
        decide_approval(
            card.approval_id,
            "approve",
            owner_id="jerome",
            surface=TELEGRAM,
            execute=execute,
        )
    assert executed == [proposal.id]


def test_telegram_decide_is_the_same_path_as_pollen() -> None:
    corpus = _corpus()
    proposal = propose_model_write(
        key="note",
        expected_revision=0,
        value={"body": "from-model"},
        project="example-api",
        task="docs",
    )
    card = present(proposal, owner_id="jerome")
    pollen = pollen_card(card)
    keyboard = telegram_keyboard(card, door=APPROVALS)
    assert keyboard is not None
    decision, approval_id = parse_callback(keyboard.buttons[0][0]["callback_data"])
    assert approval_id == pollen["approval_id"]
    result = decide_approval(
        approval_id,
        decision,
        owner_id="jerome",
        surface=TELEGRAM,
        corpus=corpus,
    )
    assert result.surface == TELEGRAM
    assert result.proposal.status == APPROVED
    assert result.resumed is True
    assert recall(corpus, "note").payload == {"body": "from-model"}


def test_reject_leaves_memory_corpus_unchanged() -> None:
    corpus = _corpus()
    proposal = propose_model_write(key="note", expected_revision=0, value={"body": "nope"})
    present(proposal, owner_id="jerome")
    result = decide_approval(
        proposal.id,
        "deny",
        owner_id="jerome",
        surface=POLLEN,
        corpus=corpus,
    )
    assert result.proposal.status == REJECTED
    assert result.resumed is False
    assert recall(corpus, "note") is None


def test_edit_then_approve_applies_user_text() -> None:
    corpus = _corpus()
    proposal = propose_model_write(key="note", expected_revision=0, value={"body": "model"})
    present(proposal, owner_id="jerome")
    staged = decide_approval(
        proposal.id,
        "edit",
        owner_id="jerome",
        surface=POLLEN,
        corpus=corpus,
        edited_payload={"value": {"body": "user"}},
    )
    assert staged.proposal.status == PENDING
    assert recall(corpus, "note") is None
    approved = decide_approval(
        proposal.id,
        "approve",
        owner_id="jerome",
        surface=TELEGRAM,
        corpus=corpus,
    )
    assert approved.proposal.status == APPROVED
    assert recall(corpus, "note").payload == {"body": "user"}


def test_present_pending_is_idempotent_for_owner() -> None:
    first = create_pending(kind="partition", project="example-api", task="docs")
    second = create_pending(kind="skill_evolution", project="example-api", task="docs")
    cards = present_pending(owner_id="jerome")
    ids = {card.approval_id for card in cards}
    assert first.id in ids
    assert second.id in ids
    again = present_pending(owner_id="jerome")
    assert {card.approval_id for card in again} == ids
    with pytest.raises(PresenterError, match="owned by someone else"):
        present(first, owner_id="bob")


def test_whatsapp_surface_is_refused() -> None:
    proposal = create_pending(kind="tool", payload={"token": "Bash"})
    present(proposal, owner_id="jerome")
    with pytest.raises(PresenterError, match="surface"):
        decide_approval(proposal.id, "approve", owner_id="jerome", surface="whatsapp")
