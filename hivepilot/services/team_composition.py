"""Let the Chief of Staff propose a smaller team, without letting it excuse one.

The motivation is measured: **$5.94 for a 100-line stdlib script** -- ten
stages including a CISO and a QA pass on a CLI tool with no network, no
credentials and no dependencies. Running the full roster on every objective,
however small, is the single largest avoidable cost in a pipeline.

But "skip the security review" is an authorisation decision wearing a
cost-saving hat, and its failure mode is silent: a composition that quietly
drops the CISO produces a green pipeline, a merged pull request, and no
artefact anywhere saying a review was skipped. That is indistinguishable from a
review that passed, which is the worst property a security control can have.

Hence four guards, in this order:

1. **Select, never invent.** A name the agent made up is ignored and recorded.
   No fuzzy matching -- "review" is not "reviewer", and a near-miss resolved
   generously would silently run a role the agent never named, or drop one it
   meant to keep.
2. **Blocking roles are not removable.** Any stage whose role declares
   ``can_block``, plus the release gate, is restored if the selection omits it.
   ``can_block`` has been descriptive metadata read nowhere in the execution
   path; this is the first code that enforces it, and it enforces it *against*
   the agent that would skip it.
3. **Fail closed means the FULL team.** Absent, empty or unparseable output
   runs everything. An agent that emits nothing must not thereby empty the
   roster, and a broken parser must not silently become a cost optimiser.
4. **Recorded with a reason.** Every decision carries what was dropped, what
   was restored and what was invented. A decision nobody can audit afterwards
   is indistinguishable from no decision.

This module is pure: it takes facts about the stages and the selector's text,
and returns what should run. It reads no configuration and touches no
database, so the guards can be tested for what they are -- an authorisation
policy -- rather than through an orchestrator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)

#: The directive a selector stage emits. Deliberately its own line-oriented
#: form rather than a field of `parse_agent_report`: that helper's known-field
#: set is shared with the release gate, and adding a team roster to it would
#: put "who reviews" one typo away from "what the reviewer decided".
_DIRECTIVE_RE = re.compile(r"^\s*TEAM\s*:\s*(?P<names>.*)$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class StageFacts:
    """What the policy needs to know about one stage.

    Built by the caller from the resolved configuration so this module never
    reaches for global state -- and so a test can state a roster in six lines.
    """

    name: str
    role: str | None
    can_block: bool = False
    is_release_gate: bool = False

    @property
    def removable(self) -> bool:
        """False for anything the agent is not permitted to drop.

        A stage with no role is not removable either: the selector names
        roles, so a stage it cannot name is one it cannot have consented to
        dropping.
        """
        return bool(self.role) and not self.can_block and not self.is_release_gate


@dataclass(frozen=True)
class CompositionDecision:
    """What will run, what will not, and why -- all of it, for the record."""

    selected: tuple[str, ...]
    dropped: tuple[str, ...] = ()
    kept_despite: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    applied: bool = False
    reason: str = ""
    #: Roles the selector named that map to a stage. Kept for the record.
    requested: tuple[str, ...] = field(default=())


def parse_team_directive(text: str | None) -> list[str] | None:
    """Role names from a ``TEAM:`` line, or ``None`` if there is no directive.

    ``None`` and ``[]`` are deliberately different answers. ``None`` means the
    agent said nothing about composition, which runs everything. ``[]`` means
    it emitted the directive and named nobody, which is a malformed selection
    -- also everything, but for a reason worth recording separately.
    """
    if not text:
        return None
    match = _DIRECTIVE_RE.search(text)
    if match is None:
        return None
    raw = match.group("names")
    return [name.strip().lower() for name in raw.split(",") if name.strip()]


def _full_team(
    stages: list[StageFacts], reason: str, unknown: tuple[str, ...] = ()
) -> CompositionDecision:
    return CompositionDecision(
        selected=tuple(stage.name for stage in stages),
        applied=False,
        unknown=unknown,
        reason=reason,
    )


def decide(
    *,
    stages: list[StageFacts],
    selector_output: str | None,
    selector_stage: str | None = None,
) -> CompositionDecision:
    """Resolve the selector's proposal into a roster, guards applied.

    ``selector_stage`` names the stage whose output this is; it has already run
    by the time its own directive is read, so dropping it retroactively would
    make the recorded run incoherent. It is never dropped.
    """
    requested = parse_team_directive(selector_output)

    if requested is None:
        return _full_team(stages, "no TEAM directive was emitted; the full roster runs")
    if not requested:
        return _full_team(stages, "the TEAM directive named nobody; the full roster runs")

    known_roles = {stage.role for stage in stages if stage.role}
    unknown = tuple(name for name in requested if name not in known_roles)
    chosen = {name for name in requested if name in known_roles}

    if not chosen:
        return _full_team(
            stages,
            "the TEAM directive named only roles this pipeline does not have "
            f"({', '.join(unknown)}); the full roster runs",
            unknown=unknown,
        )

    selected: list[str] = []
    dropped: list[str] = []
    kept_despite: list[str] = []
    for stage in stages:
        if stage.role in chosen or stage.name == selector_stage:
            selected.append(stage.name)
        elif stage.removable:
            dropped.append(stage.name)
        else:
            selected.append(stage.name)
            kept_despite.append(stage.name)

    if not dropped:
        return CompositionDecision(
            selected=tuple(selected),
            kept_despite=tuple(kept_despite),
            unknown=unknown,
            requested=tuple(requested),
            applied=False,
            reason="the selection removed nothing; the full roster runs",
        )

    parts = [f"selected {', '.join(sorted(chosen))}", f"dropped {', '.join(dropped)}"]
    if kept_despite:
        parts.append(f"kept anyway (can_block or release gate): {', '.join(kept_despite)}")
    if unknown:
        parts.append(f"ignored names this pipeline does not have: {', '.join(unknown)}")

    decision = CompositionDecision(
        selected=tuple(selected),
        dropped=tuple(dropped),
        kept_despite=tuple(kept_despite),
        unknown=unknown,
        requested=tuple(requested),
        applied=True,
        reason="; ".join(parts),
    )
    logger.info(
        "composition.decided",
        selected=list(decision.selected),
        dropped=list(decision.dropped),
        kept_despite=list(decision.kept_despite),
        unknown=list(decision.unknown),
    )
    return decision
