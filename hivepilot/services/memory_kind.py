"""Memory backends as peers, and the compositions the engine refuses.

mem0 and obsidian ship as plain `hook` plugins, and `MemoryBackendsView`
hardcodes "the two memory backends". So a third backend — honcho, Hindsight,
or one an operator writes — has no seat at the table. That contradicts the
engine's own rule: generic for ANY organisation, with noxys as one consumer.
Pinning mem0 as the only option is precisely the coupling that rule exists to
prevent.

This module owns the part that cannot live in a plugin: which *combinations*
are safe.

**Why semantics are declared and not inferred.** obsidian appends to
`extra_prompt` — `f"{existing}\\n\\n{block}"` in its own source — so it can
never destroy what another backend wrote, and composes with anything. A
backend that REPLACES the field composes with nothing. There is no way to read
that off a third party's implementation, so each backend declares it and the
engine composes on the declaration.

**Why a refusal and not a warning.** The failure being prevented is silent
corpus contamination: one backend storing another's recalled block, which
stays plausible while it compounds run after run. A warning an operator learns
to click through is worth nothing against that. Either a combination is safe
and this says nothing, or it is not and the caller refuses.

Deliberately NOT decided here: whether two backends are *redundant* (mem0 and
Hindsight both hold world-facts; honcho and Hindsight both model entities).
Redundancy is a deployment judgement about cost and duplication, and it
belongs to the config repo. This module only rules on what would corrupt.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RecallSemantics(str, Enum):
    """What a backend's `recall` does to a field another backend may own."""

    #: Appends; never overwrites. Composes with anything (obsidian).
    ADDITIVE = "additive"
    #: Replaces the field wholesale. Composes with no other exclusive backend.
    EXCLUSIVE = "exclusive"


@dataclass(frozen=True)
class MemoryBackend:
    """One registered memory backend.

    `semantics` has no default on purpose. Defaulting to ADDITIVE would be the
    dangerous guess — a replacing backend silently wiping another's recall is
    the contamination this module exists to stop — and defaulting to EXCLUSIVE
    would refuse combinations that are in fact fine. An author who has not
    thought about it must be made to.
    """

    name: str
    semantics: RecallSemantics

    def __post_init__(self) -> None:
        # `str`-valued Enum accepts its own members and their values; anything
        # else is a typo or an invented mode, and both must fail loudly at
        # registration rather than at recall time on a live run.
        RecallSemantics(self.semantics)


@dataclass(frozen=True)
class CompositionDecision:
    allowed: bool
    reason: str = ""


def describe_conflict(a: MemoryBackend, b: MemoryBackend) -> str | None:
    """Explain why *a* and *b* cannot both recall, or None if they can.

    Returns None for a safe pair so callers stay silent when nothing is wrong:
    a notice printed on every enable teaches people to ignore notices.
    """
    if a.semantics is RecallSemantics.EXCLUSIVE and b.semantics is RecallSemantics.EXCLUSIVE:
        return (
            f"{a.name} and {b.name} both replace `extra_prompt` on recall, so whichever "
            f"runs second discards the other's memories -- and each would then store the "
            f"other's recalled block as if it were the task's own input. Enable one, or "
            f"ask its author to make its recall additive."
        )
    return None


def resolve_composition(backends: list[MemoryBackend]) -> CompositionDecision:
    """Rule on a set of simultaneously-enabled memory backends.

    Order-independent by construction: a config accepted or refused depending
    on the order it happened to be written in is a trap, so the check sorts
    the exclusive backends by name before naming a pair.

    With three or more offenders it names ONE concrete pair rather than every
    combination — an unreadable list is not actionable, and resolving the named
    pair re-runs the check.
    """
    exclusive = sorted(
        (b for b in backends if b.semantics is RecallSemantics.EXCLUSIVE),
        key=lambda b: b.name,
    )
    if len(exclusive) < 2:
        return CompositionDecision(allowed=True)

    reason = describe_conflict(exclusive[0], exclusive[1])
    return CompositionDecision(allowed=False, reason=reason or "")
