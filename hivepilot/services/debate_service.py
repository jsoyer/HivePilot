"""
Debate engine — role-based position synthesis and ADR writing.

Roles state positions on a topic; the engine synthesizes a decision
deterministically and writes an ADR via ObsidianService.write_adr.

Design invariants:
- Positions are pure data inputs — the engine does NOT run CLIs or call LLMs.
- dry_run=True (default) — no real file written unless explicitly disabled.
- Synthesis is deterministic: majority-stance wins; ties go to first by input order.
- Immutable dataclasses throughout.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hivepilot.services.obsidian_service import ObsidianService

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Position:
    """A single role's stance on a debate topic.

    Parameters
    ----------
    role:
        Role identifier (e.g. ``"ceo"``, ``"cto"``).
    stance:
        One-line stance (e.g. ``"adopt"``, ``"reject"``, ``"defer"``).
    rationale:
        Short rationale for the stance.
    """

    role: str
    stance: str
    rationale: str


@dataclass(frozen=True)
class DebateResult:
    """Outcome of a debate synthesis.

    Parameters
    ----------
    topic:
        The debate topic.
    positions:
        All positions submitted to the debate.
    decision:
        The synthesized (or provided) decision.
    consequences:
        Positive/negative consequences, including dissent summary.
    confidence:
        Optional confidence score (``0.0``-``1.0``) attached to *decision*.
        ``None`` (the default) means no confidence was supplied — e.g. the
        templated/majority-stance decision path (Debate Judge & Consensus PRD,
        Sprint 1's opt-in judge is the only current producer of a real value).
    """

    topic: str
    positions: tuple[Position, ...]
    decision: str
    consequences: str
    confidence: float | None = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DebateService:
    """Debate engine: synthesize a decision from role positions and write an ADR.

    Parameters
    ----------
    vault_path:
        Path to the Obsidian vault root.  Accepts ``Path``, ``str``, or
        ``None``.  When ``None`` (or the path does not exist), ``to_adr``
        and ``run`` return ``None`` without writing anything.
    dry_run:
        When ``True`` (default), no files are written.
    """

    def __init__(
        self,
        vault_path: Path | str | None,
        dry_run: bool = True,
    ) -> None:
        resolved: Path | None = None
        if vault_path is not None:
            candidate = Path(vault_path).expanduser().resolve()
            resolved = candidate if candidate.exists() else None

        self._vault_path = resolved
        self._dry_run = dry_run
        self._obsidian: ObsidianService | None = (
            ObsidianService(vault_path=resolved, dry_run=dry_run) if resolved is not None else None
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def synthesize(
        self,
        topic: str,
        positions: list[Position],
        decision: str | None = None,
        consequences: str | None = None,
        confidence: float | None = None,
    ) -> DebateResult:
        """Synthesize a debate into a DebateResult.

        Parameters
        ----------
        topic:
            Short description of the decision being debated.
        positions:
            List of :class:`Position` objects — one per participating role.
        decision:
            Explicit decision text.  When ``None``, the majority-stance rule
            is applied deterministically: the stance shared by the most roles
            wins; ties go to the first position by input order.
        consequences:
            Explicit consequences text.  When ``None``, a short summary is
            derived by listing any dissenting positions.
        confidence:
            Optional confidence score attached to *decision* (opt-in judge
            path only). ``None`` leaves :attr:`DebateResult.confidence` unset —
            the majority-stance path (``decision=None``) is untouched.

        Returns
        -------
        DebateResult
        """
        if not positions:
            raise ValueError("positions must not be empty")

        resolved_decision = decision if decision is not None else self._majority_decision(positions)
        resolved_consequences = (
            consequences
            if consequences is not None
            else self._derive_consequences(positions, resolved_decision)
        )

        return DebateResult(
            topic=topic,
            positions=tuple(positions),
            decision=resolved_decision,
            consequences=resolved_consequences,
            confidence=confidence,
        )

    def to_adr(
        self,
        result: DebateResult,
        *,
        security_impact: str = "None identified",
        review_date: str = "",
    ) -> dict[str, Any] | None:
        """Write an ADR for the debate result via ObsidianService.

        Parameters
        ----------
        result:
            The :class:`DebateResult` to record.
        security_impact:
            Security implications of the decision.
        review_date:
            ISO date for the next scheduled review (``YYYY-MM-DD``).

        Returns
        -------
        dict or None
            The emit dict from :meth:`ObsidianService.write_adr`, plus a
            ``confidence`` key when *result.confidence* is not ``None`` (the
            emit shape is otherwise byte-identical to pre-Sprint-1 behaviour).
            ``None`` if no vault is configured.
        """
        if self._obsidian is None:
            return None

        context = (
            f"{result.topic}\n\n"
            f"This ADR records the outcome of a multi-role debate. "
            f"{len(result.positions)} position(s) were considered."
        )
        options = [f"{p.role}: {p.stance}" for p in result.positions]

        emit = self._obsidian.write_adr(
            title=result.topic,
            context=context,
            options=options,
            decision=result.decision,
            consequences=result.consequences,
            security_impact=security_impact,
            review_date=review_date,
        )
        if result.confidence is not None:
            emit = {**emit, "confidence": result.confidence}
        return emit

    def run(
        self,
        topic: str,
        positions: list[Position],
        decision: str | None = None,
        consequences: str | None = None,
        confidence: float | None = None,
        security_impact: str = "None identified",
        review_date: str = "",
    ) -> dict[str, Any] | None:
        """Convenience method: synthesize then write ADR.

        Returns ``None`` when no vault is configured (no-op).

        Parameters
        ----------
        topic:
            Short description of the decision being debated.
        positions:
            List of :class:`Position` objects.
        decision:
            Explicit decision (``None`` -> majority-stance rule).
        consequences:
            Explicit consequences (``None`` -> auto-derived from dissent).
        confidence:
            Optional confidence score attached to *decision* (opt-in judge
            path only).
        security_impact:
            Security implications of the decision.
        review_date:
            ISO date for the next scheduled review.

        Returns
        -------
        dict or None
        """
        result = self.synthesize(
            topic=topic,
            positions=positions,
            decision=decision,
            consequences=consequences,
            confidence=confidence,
        )
        return self.to_adr(result, security_impact=security_impact, review_date=review_date)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _majority_decision(positions: list[Position]) -> str:
        """Return a decision string based on the majority stance.

        Ties are broken by first appearance in *positions*.
        """
        counts: Counter[str] = Counter(p.stance for p in positions)
        max_count = max(counts.values())

        # Collect stances in first-seen order for deterministic tie-breaking
        seen: set[str] = set()
        ordered_stances: list[str] = []
        for p in positions:
            if p.stance not in seen:
                seen.add(p.stance)
                ordered_stances.append(p.stance)

        winning_stance = next(s for s in ordered_stances if counts[s] == max_count)
        supporters = [p.role for p in positions if p.stance == winning_stance]

        return (
            f"{winning_stance} "
            f"(supported by: {', '.join(supporters)}; "
            f"total votes: {max_count}/{len(positions)})"
        )

    @staticmethod
    def _derive_consequences(positions: list[Position], decision: str) -> str:
        """Derive a short consequences string by summarising dissenting positions."""
        counts: Counter[str] = Counter(p.stance for p in positions)
        max_count = max(counts.values())

        seen: set[str] = set()
        ordered_stances: list[str] = []
        for p in positions:
            if p.stance not in seen:
                seen.add(p.stance)
                ordered_stances.append(p.stance)

        winning_stance = next(s for s in ordered_stances if counts[s] == max_count)
        dissenters = [p for p in positions if p.stance != winning_stance]

        if not dissenters:
            return f"Unanimous agreement on '{winning_stance}'. No dissenting positions."

        dissent_lines = "; ".join(f"{p.role} ({p.stance}: {p.rationale})" for p in dissenters)
        return f"Decision '{winning_stance}' adopted. Dissenting position(s): {dissent_lines}."
