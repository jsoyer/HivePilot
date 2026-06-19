"""
Tests for hivepilot.services.interaction_service.

All tests use tmp_path (pytest) — NEVER write to the real vault.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.services.interaction_service import Interaction, InteractionService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HIVEPILOT_SUBTREE = "12 - HivePilot"
_INTERACTIONS_FOLDER = "Interactions"

FIXED_TIMESTAMP = "2026-06-19T10:00:00"
FIXED_DATE = "2026-06-19"


def _make_fake_vault(tmp_path: Path) -> Path:
    """Create a minimal fake vault with the required Interactions subfolder."""
    vault = tmp_path / "FakeVault"
    vault.mkdir()
    interactions_dir = vault / _HIVEPILOT_SUBTREE / _INTERACTIONS_FOLDER
    interactions_dir.mkdir(parents=True)
    return vault


def _make_interaction(
    actor: str = "architect",
    action: str = "reviews design",
    target: str | None = "developer",
    summary: str = "Reviewed the API design",
    timestamp: str = FIXED_TIMESTAMP,
    run_id: int | None = 42,
    metadata: dict | None = None,
) -> Interaction:
    return Interaction(
        actor=actor,
        action=action,
        target=target,
        summary=summary,
        timestamp=timestamp,
        run_id=run_id,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Interaction dataclass
# ---------------------------------------------------------------------------


class TestInteraction:
    def test_frozen_dataclass(self) -> None:
        """Interaction must be immutable (frozen=True)."""
        i = _make_interaction()
        with pytest.raises(Exception):
            i.actor = "other"  # type: ignore[misc]

    def test_optional_fields_default_to_none(self) -> None:
        i = Interaction(
            actor="a",
            action="does something",
            target=None,
            summary="summary text",
            timestamp=FIXED_TIMESTAMP,
        )
        assert i.run_id is None
        assert i.metadata is None

    def test_with_metadata(self) -> None:
        meta = {"key": "value", "count": 3}
        i = _make_interaction(metadata=meta)
        assert i.metadata == meta


# ---------------------------------------------------------------------------
# InteractionService — no vault (None)
# ---------------------------------------------------------------------------


class TestInteractionServiceNoVault:
    def test_log_interaction_returns_none_when_vault_is_none(self) -> None:
        svc = InteractionService(vault_path=None)
        result = svc.log_interaction(_make_interaction())
        assert result is None

    def test_write_timeline_note_returns_none_when_vault_is_none(self) -> None:
        svc = InteractionService(vault_path=None)
        interactions = [_make_interaction()]
        result = svc.write_timeline_note(interactions, timestamp=FIXED_TIMESTAMP)
        assert result is None


# ---------------------------------------------------------------------------
# InteractionService — dry_run=True (default)
# ---------------------------------------------------------------------------


class TestInteractionServiceDryRun:
    def test_log_interaction_dry_run_returns_dict(self, tmp_path: Path) -> None:
        vault = _make_fake_vault(tmp_path)
        svc = InteractionService(vault_path=vault, dry_run=True)
        result = svc.log_interaction(_make_interaction())
        assert result is not None
        assert isinstance(result, dict)
        assert result["dry_run"] is True

    def test_log_interaction_dry_run_does_not_create_file(self, tmp_path: Path) -> None:
        vault = _make_fake_vault(tmp_path)
        svc = InteractionService(vault_path=vault, dry_run=True)
        svc.log_interaction(_make_interaction())
        interactions_dir = vault / _HIVEPILOT_SUBTREE / _INTERACTIONS_FOLDER
        # No real file should have been written
        assert list(interactions_dir.iterdir()) == []

    def test_log_interaction_path_contains_date_actor_action_slug(self, tmp_path: Path) -> None:
        vault = _make_fake_vault(tmp_path)
        svc = InteractionService(vault_path=vault, dry_run=True)
        result = svc.log_interaction(_make_interaction())
        assert result is not None
        assert FIXED_DATE in result["path"]
        assert "architect" in result["path"]

    def test_write_timeline_note_dry_run_contains_mermaid(self, tmp_path: Path) -> None:
        vault = _make_fake_vault(tmp_path)
        svc = InteractionService(vault_path=vault, dry_run=True)
        interactions = [_make_interaction()]
        result = svc.write_timeline_note(interactions, timestamp=FIXED_TIMESTAMP)
        assert result is not None
        assert "mermaid" in result["content"]
        assert result["dry_run"] is True


# ---------------------------------------------------------------------------
# InteractionService — dry_run=False (real writes)
# ---------------------------------------------------------------------------


class TestInteractionServiceRealWrite:
    def test_log_interaction_creates_file_under_interactions(self, tmp_path: Path) -> None:
        vault = _make_fake_vault(tmp_path)
        svc = InteractionService(vault_path=vault, dry_run=False)
        result = svc.log_interaction(_make_interaction())
        assert result is not None
        assert result["dry_run"] is False
        note_path = Path(result["path"])
        assert note_path.exists()
        # Must be under 12 - HivePilot/Interactions/
        assert _HIVEPILOT_SUBTREE in str(note_path)
        assert _INTERACTIONS_FOLDER in str(note_path)

    def test_log_interaction_note_content_has_frontmatter_fields(self, tmp_path: Path) -> None:
        vault = _make_fake_vault(tmp_path)
        svc = InteractionService(vault_path=vault, dry_run=False)
        interaction = _make_interaction(
            actor="pm",
            action="assigns task",
            target="engineer",
            run_id=7,
        )
        result = svc.log_interaction(interaction)
        assert result is not None
        content = Path(result["path"]).read_text(encoding="utf-8")
        assert "pm" in content
        assert "assigns task" in content

    def test_write_timeline_note_creates_file(self, tmp_path: Path) -> None:
        vault = _make_fake_vault(tmp_path)
        svc = InteractionService(vault_path=vault, dry_run=False)
        interactions = [_make_interaction()]
        result = svc.write_timeline_note(interactions, timestamp=FIXED_TIMESTAMP)
        assert result is not None
        assert result["dry_run"] is False
        note_path = Path(result["path"])
        assert note_path.exists()


# ---------------------------------------------------------------------------
# render_timeline — pure function
# ---------------------------------------------------------------------------


class TestRenderTimeline:
    def test_returns_mermaid_fenced_block(self) -> None:
        svc = InteractionService(vault_path=None)
        interactions = [_make_interaction()]
        result = svc.render_timeline(interactions)
        assert "```mermaid" in result
        assert "```" in result

    def test_contains_each_actor_and_action(self) -> None:
        svc = InteractionService(vault_path=None)
        interactions = [
            _make_interaction(actor="cto", action="approves", target="cfo"),
            _make_interaction(actor="cfo", action="signs off", target="legal"),
        ]
        result = svc.render_timeline(interactions)
        assert "cto" in result
        assert "approves" in result
        assert "cfo" in result
        assert "signs off" in result
        assert "legal" in result

    def test_sequence_diagram_syntax(self) -> None:
        svc = InteractionService(vault_path=None)
        interactions = [_make_interaction(actor="alice", action="asks", target="bob")]
        result = svc.render_timeline(interactions)
        assert "sequenceDiagram" in result

    def test_empty_interactions_still_returns_valid_block(self) -> None:
        svc = InteractionService(vault_path=None)
        result = svc.render_timeline([])
        assert "```mermaid" in result
        assert "sequenceDiagram" in result

    def test_interaction_with_no_target(self) -> None:
        svc = InteractionService(vault_path=None)
        interactions = [_make_interaction(actor="ceo", action="broadcasts update", target=None)]
        result = svc.render_timeline(interactions)
        assert "ceo" in result
        assert "broadcasts update" in result
