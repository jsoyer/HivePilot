"""
Interaction logging service — records inter-role interactions as Obsidian notes
and renders Mermaid sequence diagrams.

Safety invariants:
- dry_run=True (default) returns planned path + content WITHOUT writing.
- No real vault write unless dry_run=False AND vault_path exists on disk.
- All I/O is delegated to ObsidianService (which enforces subtree safety).
- render_timeline() is a pure function with no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hivepilot.services import state_service
from hivepilot.services.obsidian_service import ObsidianService
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Interaction:
    """Immutable record of a single inter-role interaction.

    Parameters
    ----------
    actor:
        Role name initiating the interaction (e.g. ``"architect"``).
    action:
        Short verb phrase describing what happened (e.g. ``"reviews design"``).
    target:
        Role name receiving the interaction, or ``None`` for broadcasts.
    summary:
        Human-readable description of what occurred.
    timestamp:
        ISO-8601 datetime string (passed in — do NOT call datetime.now here).
    run_id:
        Optional pipeline run identifier for traceability.
    metadata:
        Optional arbitrary key/value pairs for extensibility.
    """

    actor: str
    action: str
    target: str | None
    summary: str
    timestamp: str
    run_id: int | None = field(default=None)
    metadata: dict | None = field(default=None)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class InteractionService:
    """Records inter-role interactions as Obsidian notes and Mermaid timelines.

    Parameters
    ----------
    vault_path:
        Path to the Obsidian vault root.  When ``None`` or the path does not
        exist on disk, all write operations are no-ops that return ``None``.
    dry_run:
        When ``True`` (default) no files are written.  Mutating methods return
        a dict describing the planned operation.
    """

    def __init__(
        self,
        vault_path: Path | str | None,
        dry_run: bool = True,
    ) -> None:
        self._dry_run = dry_run
        self._obsidian: ObsidianService | None = None

        if vault_path is not None:
            resolved = Path(vault_path).expanduser().resolve()
            if resolved.exists():
                self._obsidian = ObsidianService(resolved, dry_run=dry_run)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_interaction(self, interaction: Interaction) -> dict[str, Any] | None:
        """Write a single interaction note to ``Interactions/`` in the vault.

        The note filename follows the pattern:
        ``{date}-{actor}-{slug(action)}.md``

        Parameters
        ----------
        interaction:
            The :class:`Interaction` to persist.

        Returns
        -------
        dict with keys ``path``, ``content``, ``dry_run`` — or ``None`` when
        no vault is available.
        """
        interaction_id = state_service.record_interaction(
            actor=interaction.actor,
            action=interaction.action,
            target=interaction.target,
            summary=interaction.summary,
            timestamp=interaction.timestamp,
            run_id=interaction.run_id,
            metadata=interaction.metadata,
        )

        if self._obsidian is None:
            return None

        date = _date_from_timestamp(interaction.timestamp)
        actor_slug = _slugify(interaction.actor)
        action_slug = _slugify(interaction.action)
        filename = f"{date}-{actor_slug}-{action_slug}.md"
        subpath = f"Interactions/{filename}"

        title = f"{interaction.actor} → {interaction.action}"
        body = f"{interaction.summary}\n"

        frontmatter_fields: dict[str, Any] = {
            "type": "interaction",
            "status": "logged",
            "created": date,
            "agent": "hivepilot",
            "actor": interaction.actor,
            "action": interaction.action,
            "target": interaction.target,
            "run_id": interaction.run_id,
            "timestamp": interaction.timestamp,
        }

        result = self._obsidian.write_note(
            subpath=subpath,
            title=title,
            body=body,
            frontmatter_fields=frontmatter_fields,
        )
        if isinstance(result, dict):
            result = {**result, "interaction_id": interaction_id}
        return result

    def render_timeline(self, interactions: list[Interaction]) -> str:
        """Render interactions as a Mermaid ``sequenceDiagram`` fenced block.

        Pure function — no I/O.

        Parameters
        ----------
        interactions:
            Ordered list of interactions to render.

        Returns
        -------
        str
            A fenced Mermaid code block.
        """
        lines: list[str] = ["sequenceDiagram"]
        for i in interactions:
            target = i.target if i.target else i.actor
            lines.append(f"    {i.actor}->>{target}: {i.action}")

        diagram_body = "\n".join(lines)
        return f"```mermaid\n{diagram_body}\n```"

    def write_timeline_note(
        self,
        interactions: list[Interaction],
        timestamp: str,
    ) -> dict[str, Any] | None:
        """Write a timeline note embedding the Mermaid diagram to the vault.

        The note filename follows the pattern: ``{date}-timeline.md``

        Parameters
        ----------
        interactions:
            All interactions to include in the timeline.
        timestamp:
            ISO-8601 datetime string used to derive the note date.

        Returns
        -------
        dict with keys ``path``, ``content``, ``dry_run`` — or ``None`` when
        no vault is available.
        """
        if self._obsidian is None:
            return None

        date = _date_from_timestamp(timestamp)
        subpath = f"Interactions/{date}-timeline.md"
        title = f"Interaction Timeline — {date}"
        body = self.render_timeline(interactions)

        frontmatter_fields: dict[str, Any] = {
            "type": "timeline",
            "status": "generated",
            "created": date,
            "agent": "hivepilot",
            "timestamp": timestamp,
        }

        return self._obsidian.write_note(
            subpath=subpath,
            title=title,
            body=body,
            frontmatter_fields=frontmatter_fields,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def log_challenge_interaction(actor: str, target: str, point: str) -> None:
    """Record a challenge interaction without requiring an InteractionService instance.

    Best-effort — logs a warning and returns if state_service raises.

    Parameters
    ----------
    actor:
        The agent raising the challenge (e.g. ``"CTO"``).
    target:
        The upstream agent being challenged (e.g. ``"Chief of Staff"``).
    point:
        One-line objection or concern.
    """
    try:
        timestamp = datetime.now(tz=timezone.utc).isoformat()
        state_service.record_interaction(
            actor=actor,
            action="challenge",
            target=target or None,
            summary=point,
            timestamp=timestamp,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "interaction.challenge_failed",
            actor=actor,
            target=target,
            error=str(exc),
        )


def log_request_interaction(actor: str, target: str, question: str) -> None:
    """Record a request or answer interaction without requiring an InteractionService instance.

    Best-effort — logs a warning and returns if state_service raises.

    Parameters
    ----------
    actor:
        The agent sending the request (or the answering agent for answers).
    target:
        The agent receiving the request (or the requester for answers).
    question:
        The question asked (or the answer text prefixed with ``[ANSWER]``).
    """
    try:
        timestamp = datetime.now(tz=timezone.utc).isoformat()
        action = "answer" if question.startswith("[ANSWER]") else "request"
        state_service.record_interaction(
            actor=actor,
            action=action,
            target=target or None,
            summary=question,
            timestamp=timestamp,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "interaction.request_failed",
            actor=actor,
            target=target,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Private utilities
# ---------------------------------------------------------------------------


def _date_from_timestamp(timestamp: str) -> str:
    """Extract the ``YYYY-MM-DD`` date portion from an ISO timestamp string."""
    return timestamp[:10]


def _slugify(text: str) -> str:
    """Convert a string to a lowercase-kebab-case filename slug."""
    slug = text.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug.strip("-")
    return slug
