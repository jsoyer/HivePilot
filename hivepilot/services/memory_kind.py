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

    #: Appends to the CURRENT value; never overwrites. Composes with anything
    #: (obsidian: `f"{existing}\n\n{block}"`).
    ADDITIVE = "additive"
    #: Replaces the field wholesale. Composes with no other recalling backend.
    EXCLUSIVE = "exclusive"
    #: Appends to a SNAPSHOT taken before its own recall, not to the current
    #: value — so it silently discards anything written in between.
    #:
    #: mem0 is this, and reading its code says otherwise: line 546 is
    #: `f"{original_extra_prompt}\n\n{block}"`, which looks additive, while
    #: `original_extra_prompt` was captured at line 491. Declared ADDITIVE by a
    #: maintainer reading that line, mem0 + obsidian would be permitted and
    #: obsidian's block would vanish — the check missing the only real case it
    #: exists for. Additive in form, overwriting in effect.
    #:
    #: This state describes the ENGINE's current wiring, not mem0 forever. Once
    #: the snapshot is taken once, before the recall phase, and offered to every
    #: store, ordering stops mattering and such a backend is plainly additive.
    SNAPSHOT = "snapshot"


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
    snapshots = [x for x in (a, b) if x.semantics is RecallSemantics.SNAPSHOT]
    if snapshots:
        taker = snapshots[0]
        other = b if taker is a else a
        return (
            f"{taker.name} appends to a SNAPSHOT of `extra_prompt` taken before its own "
            f"recall, not to the current value, so it discards whatever {other.name} wrote "
            f"in between -- silently, and only when {taker.name} happens to run second. "
            f"Enable one of them. This lifts once the engine takes the snapshot itself, "
            f"once, before the recall phase."
        )
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
    by_name = sorted(backends, key=lambda b: b.name)

    # A snapshot backend cannot share the field with ANY other recaller,
    # additive included -- that is exactly the pairing a two-state check
    # wrongly permitted. Checked first because it is the strictest rule.
    snapshot = next((b for b in by_name if b.semantics is RecallSemantics.SNAPSHOT), None)
    if snapshot is not None:
        other = next((b for b in by_name if b is not snapshot), None)
        if other is not None:
            reason = describe_conflict(snapshot, other)
            return CompositionDecision(allowed=False, reason=reason or "")
        return CompositionDecision(allowed=True)

    exclusive = [b for b in by_name if b.semantics is RecallSemantics.EXCLUSIVE]
    if len(exclusive) < 2:
        return CompositionDecision(allowed=True)

    reason = describe_conflict(exclusive[0], exclusive[1])
    return CompositionDecision(allowed=False, reason=reason or "")


# The pre-recall value of `extra_prompt`, captured by the ENGINE once per task
# before any `before_step` hook fires.
#
# `plugins/mem0.py` used to capture this itself, at the top of its own recall,
# so its `store` could persist the task's real ask instead of re-persisting
# mem0's own recalled block. Right instinct, wrong moment: taken when MEM0
# runs, the snapshot already contains whatever another backend appended first,
# so mem0 would write `snapshot + its own block` and obsidian's recall would be
# gone. That is what made mem0 a SNAPSHOT backend, refused alongside every
# other recaller -- including obsidian, the combination this deployment runs.
#
# Captured up here instead, ordering stops being load-bearing and such a
# backend is plainly additive again.
#
# `_`-prefixed so it reads as private; `ClaudeRunner._build_prompt` reads only
# `extra_prompt`/`prior_context` and never iterates the dict, so it is never
# rendered into a prompt.
INPUT_SNAPSHOT_KEY = "_memory_input_snapshot"


def capture_input_snapshot(metadata: dict) -> None:
    """Record `extra_prompt` as it stood BEFORE any backend recalled.

    Idempotent per dict: `Orchestrator._execute_task` builds ONE metadata dict
    per task and reuses it for every step, so re-capturing on step two would
    snapshot step one's recalled memories -- the exact feedback loop this
    exists to close.

    An absent `extra_prompt` records `None`, not `""`. A store reads those
    differently: "there was no ask" versus "the ask was empty", and collapsing
    them would persist a blank as if it were content.
    """
    if INPUT_SNAPSHOT_KEY in metadata:
        return
    metadata[INPUT_SNAPSHOT_KEY] = metadata.get("extra_prompt")


def input_snapshot(metadata: dict) -> str | None:
    """The engine's pre-recall snapshot, or None if it was never captured."""
    return metadata.get(INPUT_SNAPSHOT_KEY)
