"""What a human did with a pull request the gate had already judged.

The autonomy ladder has never had a measurable input: 10 approvals recorded, 0
of them paired with a verdict, because the destructive-step gate and the
adversarial review have never fired on the same run. The decision that DOES
pair with a gate outcome is what happens to the pull request afterwards, and
nothing recorded it.

FOUR cases, not the two the original task described:

    gate blocked  + merged  -> override   (the gate was too strict)
    gate blocked  + closed  -> agreed
    gate promoted + merged  -> agreed
    gate promoted + closed  -> override   (the gate was too LOOSE)

The last one is the expensive one and the one that was nearly left out.
Recording only blocked pull requests measures the gate in a single direction,
so a ladder built on it can only ever learn to loosen.

`decision` is deliberately not the whole answer: both disagreements read
"override" and they are opposite failures, so `gate_blocked` travels with it.
Collapsing the pair into agree/disagree would average two errors that point in
different directions.

Two refusals carry as much weight as the four records:

    an OPEN pull request returns None. Nobody has decided yet, and a pending
    review is not a weak agreement;

    an actor indistinguishable from HivePilot's own account returns None.
    `git.merge_pr` exists -- the engine can merge its own work -- and feeding
    that back as a human decision would close the ladder's loop on itself.
    Unknown returns None too: refusing to attribute is honest, defaulting to
    "human" silently counts every automated merge as a judgement.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Forge states that mean somebody acted. `OPEN` is deliberately absent, and so
#: is everything unrecognised -- a state this cannot read is not evidence.
_RESOLVED = {"MERGED", "CLOSED"}


def classify_pr_decision(
    *,
    gate_blocked: bool,
    pr_state: str | None,
    actor: str | None,
    engine_actor: str | None,
) -> dict[str, Any] | None:
    """The human decision this pull request represents, or None.

    None is the common answer and the safe one. It means "no decision to
    record" -- still open, unreadable, or taken by something that is not a
    person -- never "the human agreed".
    """
    state = (pr_state or "").strip().upper()
    if state not in _RESOLVED:
        return None

    who = (actor or "").strip()
    if not who:
        # An unnamed actor is fatal for MERGED and fine for CLOSED, and the
        # asymmetry is grounded rather than convenient: the engine can merge
        # (`git.merge_pr` -> `gh pr merge`) but has NO close path at all, so a
        # closed pull request is necessarily somebody's doing.
        #
        # `gh pr view` exposes `mergedBy` and nothing equivalent for a close,
        # so refusing unnamed closures would silently drop two of the four
        # cases -- including "the gate promoted it and a human threw it away",
        # the one this whole change exists to capture.
        #
        # The premise is pinned by a test: adding a close path to the engine
        # breaks it, rather than quietly poisoning the ladder.
        if state == "MERGED":
            return None
        who = "unknown"

    # Case- and whitespace-insensitive: a configured account name and what the
    # forge reports differ in shape often enough that an exact match would let
    # the engine's own merges through as human decisions.
    if engine_actor and who.casefold() == engine_actor.strip().casefold():
        return None

    merged = state == "MERGED"
    # Override is DISAGREEMENT with the gate, in either direction: shipping
    # what it blocked, or discarding what it passed.
    #
    # `!=` over `is not`, and `bool()` around the argument: identity comparison
    # only works for the True/False singletons, so a truthy non-bool from a
    # caller would silently invert the verdict rather than fail.
    decision = "agreed" if merged != bool(gate_blocked) else "override"

    return {
        "decision": decision,
        # Kept beside the verdict: "override" alone cannot tell "too strict"
        # from "too loose", and those need opposite corrections.
        "gate_blocked": bool(gate_blocked),
        "pr_state": state,
        "actor": who,
    }


def resolve_open_decisions(
    *,
    project_name: str | None,
    project: Any,
    forge: Any,
) -> int:
    """Observe every unresolved pull request for this project and record the
    human half. Returns how many were resolved.

    Runs at the START of a later run rather than from a daemon: the thing that
    opens these pull requests is best placed to notice they have been answered,
    and a background poller is a second moving part to keep alive.

    Never raises. It runs at the beginning of somebody else's run, and a
    measurement that will not write must never be the reason that run does not
    happen.
    """
    from hivepilot.services.state_service import (
        resolve_pr_gate_outcome,
        unresolved_pr_gate_outcomes,
    )

    # Asked FIRST, and a failure here abandons the whole sweep. `None` from
    # `viewer_login` means "could not determine", not "nothing to exclude" --
    # and passing it straight through would let the engine's own merges be
    # counted as human decisions. Rows are never rewritten once decided, so
    # deferring is free: a later run with a working forge resolves them.
    engine_actor = None
    try:
        engine_actor = (forge.viewer_login(project=project) or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("pr_decision.viewer_lookup_failed", error=str(exc))
    # Stripped before the check, not after: a whitespace-only login is truthy,
    # and would have walked past this guard into the classifier as a name that
    # matches nothing -- silently reinstating the fall-through this exists to
    # prevent. `forges.github` strips its own answer; a forge plugin need not.
    if not engine_actor:
        logger.info("pr_decision.deferred_unknown_engine_actor")
        return 0

    resolved = 0
    try:
        pending = unresolved_pr_gate_outcomes(project=project_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pr_decision.list_failed", error=str(exc))
        return 0

    for row in pending:
        branch = row.get("branch")
        if not branch:
            continue
        try:
            state, actor = forge.pr_outcome(project=project, branch=branch)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pr_decision.observe_failed", branch=branch, error=str(exc))
            continue

        verdict = classify_pr_decision(
            gate_blocked=bool(row.get("gate_blocked")),
            pr_state=state,
            actor=actor,
            engine_actor=engine_actor,
        )
        if verdict is None:
            # Still open, unreadable, or the engine's own merge. Left
            # unresolved on purpose -- the row stays an open question.
            continue

        try:
            resolve_pr_gate_outcome(
                branch=branch,
                decision=verdict["decision"],
                pr_state=verdict["pr_state"],
                actor=verdict["actor"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("pr_decision.record_failed", branch=branch, error=str(exc))
            continue

        logger.info(
            "pr_decision.resolved",
            branch=branch,
            decision=verdict["decision"],
            gate_blocked=verdict["gate_blocked"],
            actor=verdict["actor"],
        )
        resolved += 1

    return resolved
